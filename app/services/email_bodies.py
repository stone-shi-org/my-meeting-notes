"""Lazily fetch attached emails' full bodies, and summarise the long ones.

Attaching an email stores a ~200 character snippet, because no provider returns
a body at search time. This module is the second half: the first time somebody
looks at a thread, it fetches the real bodies through each provider's
``get_email_message``/``get_email_body`` and writes them onto the rows.

**Not a queued job**, and not for the reason it looks like. The two arguments
CLAUDE.md gives for the sweep avoiding ``jobs/queue.py`` both apply harder here:

- Hydration fires on *every thread open*, not on a timer, so the progress dock
  would fill with one-second jobs while a 40-minute diarization scrolls out of
  sight. The dock's job is the one slow thing a person is waiting on.
- Restart survival, the other thing the queue buys, is free here.
  ``body IS NULL AND body_fetched_at IS NULL`` *is* the resume predicate, and the
  next page view re-issues it. Hydration is checkpointed by construction.

The one honest argument for the queue is bounding LLM spend -- and the queue
does not provide that either: it is an unbounded FIFO with no per-user limit. The
guards that do work are the ones ``next_step`` already uses, and are reused here:
a per-event-loop semaphore, and a "we already tried" stamp.
"""

from __future__ import annotations

import asyncio
import sqlite3
import weakref
from pathlib import Path

from app.db import get_conn, utcnow
from app.errors import AppError
from app.logging_config import get_logger
from app.services import html_text
from app.services import llm as llm_svc
from app.services import prompts as prompts_svc
from app.services.providers import loader as providers_svc

log = get_logger("email_bodies")

# One screenful. The rest arrive on the next view rather than making the first
# one wait -- the same reasoning as followups.MAX_ATTACH_PER_SWEEP.
HYDRATE_MAX_PER_CALL = 12

# A body this long is already far past anything a person reads in a thread view,
# and the column is on a row that broad reads touch.
MAX_BODY_CHARS = 32_000

# Below this the body *is* the summary: paying for an LLM call to compress four
# lines into one costs money, adds latency and loses information.
AI_SUMMARY_MIN_CHARS = 900

# Summaries are opt-in, one button press per conversation, so this bound is about
# what a person is willing to wait for rather than about a screenful. Lower than
# HYDRATE_MAX_PER_CALL because each item here is an LLM round trip.
SUMMARISE_MAX_PER_CALL = 8

# What gets sent to the summariser, so one enormous newsletter cannot dominate a
# request. The head is kept: an email says what it wants in its first screen.
SUMMARY_INPUT_CHARS = 6_000

# Provider fetches. Google's per-mailbox N+1 ceiling -- see google.GMAIL_CONCURRENCY.
FETCH_CONCURRENCY = 8

# Summaries. Deliberately NOT 8: that number is about Gmail's rate limits, this
# one is about LLM round trips a person is actively waiting on, which is exactly
# what next_step.LIST_REFRESH_CONCURRENCY is set for.
SUMMARY_CONCURRENCY = 4

_SUMMARY_LIMITERS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

# Columns every read path wants. `body` and `raw_json` are deliberately absent:
# SQLite reads whole rows, and a 32KB body pulled off disk by a timeline load
# that never displays one is pure cost.
ROW_COLUMNS = (
    "id, thread_id, meeting_id, mcp_id, message_id, sender, subject, date, "
    "snippet, account, triage_level, tag, summary, score, relevance_score, "
    "relevance_reason, attached_at, url, rfc_message_id, provider, "
    "auto_attached, seen_at, folder_id, conversation_id, in_reply_to, "
    "references_json, to_recipients, cc_recipients, direction, "
    "body_fetched_at, ai_summary, ai_summary_model, integration_id, "
    "(body IS NOT NULL) AS has_body"
)


def _summary_limiter() -> asyncio.Semaphore:
    """One limiter per event loop, shared across concurrent requests on it.

    A semaphore created per call would cap one request and let N concurrent
    thread opens multiply the limit by N -- the same trap documented on
    next_step._list_refresh_limiter.
    """
    loop = asyncio.get_running_loop()
    limiter = _SUMMARY_LIMITERS.get(loop)
    if limiter is None:
        limiter = asyncio.Semaphore(SUMMARY_CONCURRENCY)
        _SUMMARY_LIMITERS[loop] = limiter
    return limiter


def derive_snippet(body: str, limit: int = 200) -> str:
    """A one-line preview from a body, for a provider that supplied none.

    Apple/IMAP sets ``snippet=None`` by construction because its search is a
    header-only fetch, so every iCloud row renders blank in a snippet-based UI.
    Only ever used where the column is still NULL -- overwriting a provider's own
    snippet with our guess at one would be a regression, not an improvement.
    """
    flat = " ".join((body or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


def pending(
    conn: sqlite3.Connection,
    thread_id: int,
    *,
    limit: int = HYDRATE_MAX_PER_CALL,
    email_id: int | None = None,
    force: bool = False,
) -> list[sqlite3.Row]:
    """Rows still wanting a body, newest first.

    ``body_fetched_at IS NULL`` rather than ``body IS NULL`` is the whole point of
    the second column: a provider with no fetch-by-id tool returns None forever,
    and without the stamp every page view would ask it again.
    """
    where = ["thread_id = ?", "body IS NULL"]
    params: list = [thread_id]
    if email_id is not None:
        where.append("id = ?")
        params.append(email_id)
    if not force:
        where.append("body_fetched_at IS NULL")

    params.append(limit)
    return conn.execute(
        f"SELECT id, message_id, mcp_id, folder_id, integration_id, provider, "
        f"snippet, subject, sender FROM thread_emails "
        f"WHERE {' AND '.join(where)} ORDER BY date DESC LIMIT ?",
        params,
    ).fetchall()


def pending_summaries(
    conn: sqlite3.Connection,
    thread_id: int,
    *,
    limit: int = SUMMARISE_MAX_PER_CALL,
    email_ids: list[int] | None = None,
) -> list[sqlite3.Row]:
    """Rows that have a body worth summarising but no summary, newest first.

    A **separate** predicate from :func:`pending`, and that separation is the
    whole point. ``pending`` requires ``body IS NULL``, so once a body is stored
    the row is never selected again -- which silently made a failed or skipped
    summary permanent, since even ``force=True`` only relaxes the
    ``body_fetched_at`` half. Asking "which rows want a summary?" is a different
    question from "which rows want a body?" and needs its own query.

    No "we already tried" stamp here, unlike ``body_fetched_at``: summarising is
    an explicit button press, so pressing it again *is* the retry.
    """
    where = [
        "thread_id = ?",
        "body IS NOT NULL",
        "ai_summary IS NULL",
        "length(body) >= ?",
    ]
    params: list = [thread_id, AI_SUMMARY_MIN_CHARS]
    if email_ids:
        where.append(f"id IN ({','.join('?' * len(email_ids))})")
        params.extend(email_ids)

    params.append(limit)
    return conn.execute(
        f"SELECT id, subject, sender, body FROM thread_emails "
        f"WHERE {' AND '.join(where)} ORDER BY date DESC LIMIT ?",
        params,
    ).fetchall()


def _count_pending_bodies(conn: sqlite3.Connection, thread_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM thread_emails WHERE thread_id = ? "
        "AND body IS NULL AND body_fetched_at IS NULL",
        (thread_id,),
    ).fetchone()[0]


def _count_pending_summaries(conn: sqlite3.Connection, thread_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM thread_emails WHERE thread_id = ? "
        "AND body IS NOT NULL AND ai_summary IS NULL AND length(body) >= ?",
        (thread_id, AI_SUMMARY_MIN_CHARS),
    ).fetchone()[0]


def _native_id(row: sqlite3.Row) -> str | None:
    return row["mcp_id"] or None


def _integration_id(row: sqlite3.Row) -> int | None:
    """Prefer the column; fall back to parsing the composite message_id.

    The fallback is only for rows attached before ``integration_id`` existed. It
    is also why MCP rows were previously unfetchable: MCP deliberately emits bare
    ids, so there is no ``{provider}:{integration_id}:{native}`` to parse.
    """
    if row["integration_id"] is not None:
        return int(row["integration_id"])
    parts = str(row["message_id"] or "").split(":", 2)
    if len(parts) > 1 and parts[1].isdigit():
        return int(parts[1])
    return None


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #


def _store(
    db_path: Path | str | None,
    row_id: int,
    *,
    body: str | None,
    headers: dict | None = None,
    fill_snippet: bool,
) -> None:
    """Write one fetch result. Always stamps ``body_fetched_at``.

    Stamped on failure too, which is what makes "asked, and this account cannot
    supply one" a representable state rather than an infinite retry.

    Threading columns use COALESCE for the same reason ``attach_email``'s conflict
    clause does: hydration must be able to *fill* them, never to blank them.
    """
    sets = ["body_fetched_at = ?"]
    params: list = [utcnow()]

    if body is not None:
        sets.append("body = ?")
        params.append(body)
        if fill_snippet:
            sets.append("snippet = ?")
            params.append(derive_snippet(body))

    for column in (
        "conversation_id",
        "rfc_message_id",
        "in_reply_to",
        "to_recipients",
        "cc_recipients",
        "direction",
    ):
        value = (headers or {}).get(column)
        if value:
            sets.append(f"{column} = COALESCE({column}, ?)")
            params.append(value)

    references = (headers or {}).get("references")
    if references:
        import json

        sets.append("references_json = COALESCE(references_json, ?)")
        params.append(json.dumps(list(references)))

    params.append(row_id)
    with get_conn(db_path) as conn:
        conn.execute(
            f"UPDATE thread_emails SET {', '.join(sets)} WHERE id = ?", params
        )
    # Deliberately no touch_thread() and no seen_at write. Hydrating is the app
    # fetching, not a person reading: bumping updated_at would send a thread to
    # the top of the home list just for being opened, and writing seen_at would
    # clear the unread mark the sweep set. `mark_seen` owns that column.


def _store_summary(
    db_path: Path | str | None, row_id: int, summary: str, model: str
) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE thread_emails SET ai_summary = ?, ai_summary_model = ? WHERE id = ?",
            (summary[:1000], model, row_id),
        )


# --------------------------------------------------------------------------- #
# Summarising
# --------------------------------------------------------------------------- #


def summarise_sync(
    db_path: Path | str | None,
    *,
    body: str,
    subject: str | None,
    sender: str | None,
    model: str | None = None,
) -> tuple[str | None, str | None]:
    """One LLM call. Returns ``(summary, model)``, or ``(None, None)`` on failure.

    Blocking, so callers run it through ``to_thread`` -- the same shape as
    ``notes.generate_title`` and ``next_step.generate_sync``.

    Failure is never an error here: the body is already stored by the time this
    runs, and both summary columns staying NULL is exactly how "nothing generated
    one" is recorded. Losing the body because nothing could summarise it would be
    the worst possible outcome of opening a thread.
    """
    try:
        with get_conn(db_path) as conn:
            config = llm_svc.LLMConfig.from_db(conn, model_override=model)

        prompt = prompts_svc.load("email_summary_prompt")
        if prompt.temperature is not None:
            config.temperature = prompt.temperature

        system, user = prompt.render(
            {
                "email_body": body[:SUMMARY_INPUT_CHARS],
                "subject": subject or "(no subject)",
                "sender": sender or "(unknown sender)",
            }
        )
        parsed, _, _ = llm_svc.chat_json(config, system, user)
        summary = (parsed.get("summary") or "").strip()
        if not summary:
            raise llm_svc.LLMError("Model returned an empty summary")
    except Exception as exc:  # noqa: BLE001 - see docstring
        log.warning("email summary generation failed: %s", exc)
        return None, None

    return summary, config.model


# --------------------------------------------------------------------------- #
# Bodies
# --------------------------------------------------------------------------- #


async def hydrate_thread_emails(
    db_path: Path | str | None,
    *,
    thread_id: int,
    user_id: int,
    limit: int = HYDRATE_MAX_PER_CALL,
    email_id: int | None = None,
    force: bool = False,
) -> dict:
    """Fetch and store bodies for a thread's un-hydrated emails.

    Bodies only. Summaries are a separate, explicitly-requested pass -- see
    :func:`summarise_thread_emails`. Splitting them keeps the automatic path free
    of LLM spend and latency: fetching a body is a cheap provider read that
    should just happen, while summarising is a per-message model call somebody
    should be asking for.

    ``remaining`` says how many rows are still un-hydrated after this call, so
    the caller can keep going rather than leaving a long thread half-filled.

    Connections are opened per step rather than held across the provider round
    trips -- the same rule as the interactive match route: a request connection
    parked inside an HTTP call to Google is a write lock held for seconds.
    """
    with get_conn(db_path) as conn:
        rows = pending(conn, thread_id, limit=limit, email_id=email_id, force=force)
        if not rows:
            return {
                "requested": 0,
                "fetched": 0,
                "unavailable": 0,
                "remaining": _count_pending_bodies(conn, thread_id),
            }

        # Owner-scoped, so someone else's integration id is simply not found --
        # this is what keeps hydration from becoming a way to probe another
        # user's connected accounts.
        wanted = {i for i in (_integration_id(r) for r in rows) if i is not None}
        integrations = {
            row["id"]: row
            for row in conn.execute(
                "SELECT * FROM integrations WHERE user_id = ? AND id IN "
                f"({','.join('?' * len(wanted))})",
                [user_id, *sorted(wanted)],
            )
        } if wanted else {}
        providers = {
            iid: providers_svc.build_provider(conn, row)
            for iid, row in integrations.items()
        }

    gate = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def one(row: sqlite3.Row) -> bool:
        provider = providers.get(_integration_id(row) or -1)
        native = _native_id(row)
        fetched: object = None

        if provider is not None and native:
            async with gate:
                try:
                    fetched = await provider.get_email_message(
                        native_id=native, folder_id=row["folder_id"]
                    )
                except AppError as exc:
                    log.info("email %s body unavailable: %s", row["id"], exc.message)
                except Exception as exc:  # noqa: BLE001
                    log.warning("email %s body fetch failed: %s", row["id"], exc)

        body = html_text.to_plain_text(getattr(fetched, "body", None))[:MAX_BODY_CHARS]
        headers = fetched.header_updates() if fetched is not None else {}

        await asyncio.to_thread(
            _store,
            db_path,
            row["id"],
            body=body or None,
            headers=headers,
            # Only where the provider gave us none. Apple/IMAP is the case this
            # exists for; overwriting Gmail's own snippet would be a regression.
            fill_snippet=not (row["snippet"] or "").strip(),
        )
        return bool(body)

    results = await asyncio.gather(*(one(r) for r in rows), return_exceptions=True)

    fetched = 0
    for result in results:
        if isinstance(result, BaseException):
            log.warning("hydration task failed: %s", result)
            continue
        fetched += int(result)

    with get_conn(db_path) as conn:
        remaining = _count_pending_bodies(conn, thread_id)

    return {
        "requested": len(rows),
        "fetched": fetched,
        "unavailable": len(rows) - fetched,
        "remaining": remaining,
    }


# --------------------------------------------------------------------------- #
# Summaries -- opt in, never automatic
# --------------------------------------------------------------------------- #


async def summarise_thread_emails(
    db_path: Path | str | None,
    *,
    thread_id: int,
    limit: int = SUMMARISE_MAX_PER_CALL,
    email_ids: list[int] | None = None,
) -> dict:
    """Summarise stored bodies that have no summary yet.

    Deliberately not part of hydration. One LLM call per message is real money
    and real latency, and most messages are read rather than skimmed -- so this
    runs when somebody asks for it, on the conversation they are looking at.

    Selection is `pending_summaries`, which asks "which rows want a summary?"
    rather than reusing the body predicate. That distinction is what makes a
    failed summary retryable: a row with a stored body is invisible to
    ``pending`` forever, whatever ``force`` is set to.
    """
    with get_conn(db_path) as conn:
        rows = pending_summaries(conn, thread_id, limit=limit, email_ids=email_ids)
        if not rows:
            return {
                "requested": 0,
                "summarised": 0,
                "failed": 0,
                "remaining": _count_pending_summaries(conn, thread_id),
            }

    gate = _summary_limiter()

    async def one(row: sqlite3.Row) -> bool:
        async with gate:
            summary, model = await asyncio.to_thread(
                summarise_sync,
                db_path,
                body=row["body"],
                subject=row["subject"],
                sender=row["sender"],
            )
        if not (summary and model):
            # Both columns stay NULL, which is what leaves the row selectable
            # next time. The body is already stored either way.
            return False
        await asyncio.to_thread(_store_summary, db_path, row["id"], summary, model)
        return True

    results = await asyncio.gather(*(one(r) for r in rows), return_exceptions=True)

    summarised = 0
    for result in results:
        if isinstance(result, BaseException):
            log.warning("summary task failed: %s", result)
            continue
        summarised += int(result)

    with get_conn(db_path) as conn:
        remaining = _count_pending_summaries(conn, thread_id)

    return {
        "requested": len(rows),
        "summarised": summarised,
        "failed": len(rows) - summarised,
        "remaining": remaining,
    }


def body_of(conn: sqlite3.Connection, thread_id: int, email_id: int) -> sqlite3.Row | None:
    """One email's stored body. The list routes never carry it."""
    return conn.execute(
        "SELECT id, body, body_fetched_at, ai_summary, ai_summary_model, "
        "(body IS NOT NULL) AS has_body FROM thread_emails "
        "WHERE id = ? AND thread_id = ?",
        (email_id, thread_id),
    ).fetchone()
