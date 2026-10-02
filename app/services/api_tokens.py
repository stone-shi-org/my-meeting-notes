"""Personal API tokens for the MCP server (MMN-14).

A token is what an agent -- Claude Code, Claude Desktop through mcp-remote,
Pocket Agent -- presents to ``/mcp`` as ``Authorization: Bearer mmn_...``.
Sessions are the wrong credential for that: they expire after
``session_ttl_hours`` and can only be minted by a password login, so an agent
configured once would silently break two weeks later.

Stored exactly like ``sessions``: the database holds ``sha256(token)`` and the
raw value exists only in the create response, shown once. ``prefix`` keeps the
first characters so the Settings list can tell tokens apart without holding
anything replayable.

Tokens are accepted by ``/mcp`` only, never by the REST API. The REST routes
know nothing about ``scope``, so a read-only token presented there would act
as a full-access one.
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from app.db import utcnow
from app.errors import NotFoundError, ValidationError
from app.security import hash_token

TOKEN_PREFIX = "mmn_"
TOKEN_BYTES = 32
# Characters of the raw token kept for display: "mmn_" plus four of the secret.
# Enough to tell two tokens apart in a list, far too few to guess the rest.
DISPLAY_PREFIX_LEN = len(TOKEN_PREFIX) + 4
SCOPES = ("read", "read_write")
NAME_MAX = 100
MAX_EXPIRY_DAYS = 3650
# last_used_at is written at most this often, so a chatty agent does not turn
# every tool call into a write.
LAST_USED_RESOLUTION_SECONDS = 60


def new_raw_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)


def looks_like_api_token(raw: str) -> bool:
    return raw.startswith(TOKEN_PREFIX)


def _parse(stamp: str) -> datetime:
    parsed = datetime.fromisoformat(stamp)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def row_to_token(row: sqlite3.Row) -> dict:
    """The listable shape. Never includes the hash, and never could include
    the raw token -- it is not stored."""
    now = datetime.now(timezone.utc)
    expired = bool(row["expires_at"]) and _parse(row["expires_at"]) <= now
    if row["revoked_at"]:
        state = "revoked"
    elif expired:
        state = "expired"
    else:
        state = "active"
    return {
        "id": row["id"],
        "name": row["name"],
        "prefix": row["prefix"],
        "scope": row["scope"],
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
        "expires_at": row["expires_at"],
        "revoked_at": row["revoked_at"],
        "state": state,
    }


def create_token(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    name: str,
    scope: str = "read",
    expires_in_days: int | None = None,
) -> tuple[str, dict]:
    """Mint a token. Returns ``(raw_token, row)``; the raw value is never
    recoverable after this call returns."""
    name = (name or "").strip()
    if not name:
        raise ValidationError("A token needs a name, so you can tell it apart later")
    if len(name) > NAME_MAX:
        raise ValidationError(f"Token name must be at most {NAME_MAX} characters")
    if scope not in SCOPES:
        raise ValidationError(f"scope must be one of: {', '.join(SCOPES)}")
    expires_at = None
    if expires_in_days is not None:
        if not 1 <= expires_in_days <= MAX_EXPIRY_DAYS:
            raise ValidationError(f"expires_in_days must be between 1 and {MAX_EXPIRY_DAYS}")
        expires_at = (datetime.now(timezone.utc) + timedelta(days=expires_in_days)).isoformat()

    raw = new_raw_token()
    cur = conn.execute(
        """
        INSERT INTO api_tokens (user_id, name, token_hash, prefix, scope, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, name, hash_token(raw), raw[:DISPLAY_PREFIX_LEN], scope, utcnow(), expires_at),
    )
    row = conn.execute("SELECT * FROM api_tokens WHERE id = ?", (cur.lastrowid,)).fetchone()
    return raw, row_to_token(row)


def list_tokens(conn: sqlite3.Connection, *, user_id: int) -> list[dict]:
    """Only the caller's own -- admins included. A token is a credential, and
    nobody else needs to see that one exists."""
    rows = conn.execute(
        "SELECT * FROM api_tokens WHERE user_id = ? ORDER BY created_at DESC, id DESC",
        (user_id,),
    ).fetchall()
    return [row_to_token(r) for r in rows]


def revoke_token(conn: sqlite3.Connection, *, user_id: int, token_id: int) -> dict:
    """Revoked rather than deleted, so the list still shows what was cut off
    and when. Someone else's id is a 404, not a 403."""
    row = conn.execute(
        "SELECT * FROM api_tokens WHERE id = ? AND user_id = ?", (token_id, user_id)
    ).fetchone()
    if row is None:
        raise NotFoundError("Token not found")
    if not row["revoked_at"]:
        conn.execute("UPDATE api_tokens SET revoked_at = ? WHERE id = ?", (utcnow(), token_id))
        row = conn.execute("SELECT * FROM api_tokens WHERE id = ?", (token_id,)).fetchone()
    return row_to_token(row)


def resolve_token(
    conn: sqlite3.Connection, raw: str
) -> tuple[sqlite3.Row, sqlite3.Row] | None:
    """``(token, user)`` for a usable raw token, else None.

    Unusable means: unknown, revoked, expired, or belonging to a deactivated
    user. All four look identical to the caller -- a 401 that says which one
    would confirm a token exists.
    """
    token = conn.execute(
        "SELECT * FROM api_tokens WHERE token_hash = ?", (hash_token(raw),)
    ).fetchone()
    if token is None or token["revoked_at"]:
        return None
    if token["expires_at"] and _parse(token["expires_at"]) <= datetime.now(timezone.utc):
        return None
    user = conn.execute("SELECT * FROM users WHERE id = ?", (token["user_id"],)).fetchone()
    if user is None or not user["is_active"]:
        return None
    return token, user


def touch_token(conn: sqlite3.Connection, token: sqlite3.Row) -> None:
    last = token["last_used_at"]
    now = datetime.now(timezone.utc)
    if last and (now - _parse(last)).total_seconds() < LAST_USED_RESOLUTION_SECONDS:
        return
    conn.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (utcnow(), token["id"]))
