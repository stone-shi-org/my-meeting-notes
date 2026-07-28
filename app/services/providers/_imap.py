"""Minimal IMAP search over stdlib ``imaplib``.

``imaplib`` is blocking, so every call goes through ``asyncio.to_thread`` -- the
house pattern from ``jobs/queue.py``. Calling it straight from the event loop
would stall every other job for the length of the search.

The test seam is an injected *client object* rather than the class: respx cannot
help here (IMAP is not HTTP), so the fake stands in for something with
``login``/``select``/``search``/``fetch``/``logout``.
"""

from __future__ import annotations

import asyncio
import email
import imaplib
from datetime import datetime
from email.header import decode_header, make_header

from app.errors import IntegrationAuthError, MCPError
from app.logging_config import get_logger
from app.services.providers.query import imap_date

log = get_logger("providers.imap")

DEFAULT_PORT = 993
# Header-only fetches; bodies are never downloaded.
FETCH_PARTS = "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)])"


def connect(host: str, port: int = DEFAULT_PORT):
    return imaplib.IMAP4_SSL(host, port)


def build_criteria(keywords: list[str], start: datetime, end: datetime) -> list[str]:
    """IMAP SEARCH criteria: a date window ANDed with an OR-tree of keywords.

    IMAP has no ``a OR b OR c``; ``OR`` is strictly binary and prefix, so three
    terms nest as ``OR t1 (OR t2 t3)``. Getting this wrong does not error -- the
    server just returns the wrong set -- so it is built explicitly and tested.
    """
    criteria = ["SINCE", imap_date(start), "BEFORE", imap_date(end)]

    terms = [k for k in keywords[:3] if k]
    if not terms:
        return criteria

    def or_tree(items: list[str]) -> list[str]:
        if len(items) == 1:
            return ["TEXT", items[0]]
        return ["OR", *or_tree(items[:1]), *or_tree(items[1:])]

    return criteria + or_tree(terms)


def _decode(raw: str | None) -> str | None:
    """RFC 2047 headers ("=?utf-8?B?…?=") are common and unreadable raw."""
    if not raw:
        return None
    try:
        return str(make_header(decode_header(raw)))
    except Exception:  # noqa: BLE001 - a mangled header is not worth failing over
        return raw


def _search_sync(client, criteria: list[str], limit: int, mailbox: str) -> list[dict]:
    typ, _ = client.select(mailbox, readonly=True)
    if typ != "OK":
        raise MCPError(f"Could not open mailbox {mailbox!r}")

    typ, data = client.search(None, *criteria)
    if typ != "OK":
        raise MCPError("IMAP search was rejected")

    ids = (data[0].split() if data and data[0] else [])
    # Newest first, then bound: the window can hold far more than we can show.
    ids = list(reversed(ids))[:limit]
    if not ids:
        return []

    out = []
    for message_id in ids:
        typ, parts = client.fetch(message_id, FETCH_PARTS)
        if typ != "OK" or not parts:
            continue
        raw = next(
            (p[1] for p in parts if isinstance(p, tuple) and len(p) > 1 and p[1]), None
        )
        if raw is None:
            continue
        parsed = email.message_from_bytes(raw if isinstance(raw, bytes) else raw.encode())
        out.append(
            {
                "id": message_id.decode() if isinstance(message_id, bytes) else str(message_id),
                "subject": _decode(parsed.get("Subject")),
                "sender": _decode(parsed.get("From")),
                "date": parsed.get("Date"),
                "rfc_message_id": parsed.get("Message-ID"),
            }
        )
    return out


async def search(
    *,
    host: str,
    username: str,
    password: str,
    keywords: list[str],
    start: datetime,
    end: datetime,
    limit: int = 25,
    mailbox: str = "INBOX",
    connect_fn=connect,
) -> list[dict]:
    """Header-only search. ``connect_fn`` is the seam tests replace."""
    criteria = build_criteria(keywords, start, end)

    def run() -> list[dict]:
        client = connect_fn(host)
        try:
            try:
                client.login(username, password)
            except imaplib.IMAP4.error as exc:
                raise IntegrationAuthError(
                    "The mail server rejected the Apple ID or app-specific password. "
                    "It must be an app-specific password, not your account password."
                ) from exc
            return _search_sync(client, criteria, limit, mailbox)
        finally:
            try:
                client.logout()
            except Exception:  # noqa: BLE001 - a failed logout must not mask results
                pass

    return await asyncio.to_thread(run)
