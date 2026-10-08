"""Best-of-2 chat: throwing away the answer the user did not keep.

Best of 2 sends one prompt as two ordinary chat requests, one per model, so
each of them persists its own user message and its own answer. When the user
closes one panel the other answer is the one that stays, and this removes the
loser's pair so the conversation is linear again.

A pair is two adjacent rows: ``_produce`` inserts the user message and the
answer in one synchronous block with no ``await`` between them, so a second
request finishing at the same moment cannot land between them. The user
message is therefore "the row immediately before the answer, if it is a user
row" -- no id has to be threaded through the SSE payload for this.

A reply the client abandoned *before* it finished is never persisted at all
(`stream_chat_response` cancels the producer on disconnect), so this is only
called for an answer that had already arrived.
"""

from __future__ import annotations

import sqlite3

from app.errors import NotFoundError

# Table names are interpolated, never user input: callers pass a literal.
_TABLES = {"home_chat_messages", "chat_messages", "meeting_chat_messages"}


def discard_reply(
    conn: sqlite3.Connection,
    table: str,
    scope_sql: str,
    scope_params: tuple,
    message_id: int,
) -> int:
    """Delete one assistant message and the user message it answered.

    ``scope_sql`` is the table's own ownership predicate (``owner_id = ?``,
    ``thread_id = ?``, ``meeting_id = ?``). Only an *assistant* row can be
    discarded, so this can never be pointed at a user's own words.
    Returns how many rows were removed; a miss is a 404, like any other id.
    """
    if table not in _TABLES:
        raise ValueError(f"unknown chat table {table!r}")

    row = conn.execute(
        f"SELECT id, created_at FROM {table} WHERE id = ? AND role = 'assistant' AND {scope_sql}",
        (message_id, *scope_params),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"No reply {message_id}")

    prev = conn.execute(
        f"SELECT id, role FROM {table} WHERE {scope_sql} "
        "AND (created_at < ? OR (created_at = ? AND id < ?)) "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (*scope_params, row["created_at"], row["created_at"], row["id"]),
    ).fetchone()

    ids = [row["id"]]
    if prev is not None and prev["role"] == "user":
        ids.append(prev["id"])
    return conn.execute(
        f"DELETE FROM {table} WHERE id IN ({','.join('?' * len(ids))})", ids
    ).rowcount
