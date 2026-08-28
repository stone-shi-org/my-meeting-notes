"""Thread and meeting persistence."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

from app.db import utcnow
from app.errors import ConflictError, NotFoundError, ValidationError

# Thread list cards show these counts, so they're computed in the list query
# rather than N+1 round trips from the SPA.
THREAD_COUNTS_SQL = """
    (SELECT COUNT(*) FROM meetings m WHERE m.thread_id = t.id)                AS meeting_count,
    (SELECT MAX(m.meeting_at) FROM meetings m WHERE m.thread_id = t.id)       AS last_meeting_at,
    (SELECT COUNT(*) FROM thread_emails e WHERE e.thread_id = t.id)           AS email_count,
    (SELECT COUNT(*) FROM thread_calendar_events c WHERE c.thread_id = t.id)  AS event_count,
    (SELECT COUNT(*) FROM thread_notes n WHERE n.thread_id = t.id)            AS note_count,
    (
      (SELECT COUNT(*) FROM thread_emails e
        WHERE e.thread_id = t.id AND e.auto_attached = 1 AND e.seen_at IS NULL)
      +
      (SELECT COUNT(*) FROM thread_calendar_events c
        WHERE c.thread_id = t.id AND c.auto_attached = 1 AND c.seen_at IS NULL)
    )                                                                         AS unread_count
"""

# Attachment tables that carry the unread mark, keyed by the URL segment the
# thread routes already use. A map rather than an f-string: the segment comes
# from the request path, and it is about to be interpolated into SQL.
UNREAD_TABLES: dict[str, str] = {
    "emails": "thread_emails",
    "calendar-events": "thread_calendar_events",
}

SORTABLE = {
    "updated_at": "t.updated_at",
    "created_at": "t.created_at",
    "title": "t.title COLLATE NOCASE",
}

# The `group` query value that means "threads in no group at all". A sentinel
# string rather than a second boolean parameter, because the home screen sends
# one value per section and Ungrouped is just another section.
UNGROUPED = "none"


def _group_id(group: str) -> int:
    if not group.isdigit():
        raise ValidationError(f"group must be a group id or '{UNGROUPED}'")
    return int(group)


def touch_thread(conn: sqlite3.Connection, thread_id: int) -> None:
    """Bump updated_at so the thread rises in the list after any child write."""
    conn.execute(
        "UPDATE threads SET updated_at = ? WHERE id = ?", (utcnow(), thread_id)
    )


def get_thread(conn: sqlite3.Connection, thread_id: int) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT t.*, {THREAD_COUNTS_SQL} FROM threads t WHERE t.id = ?", (thread_id,)
    ).fetchone()


def require_thread(conn: sqlite3.Connection, thread_id: int) -> sqlite3.Row:
    row = get_thread(conn, thread_id)
    if row is None:
        raise NotFoundError("Thread not found")
    return row


def create_thread(
    conn: sqlite3.Connection,
    *,
    owner_id: int,
    title: str,
    description: str | None = None,
) -> sqlite3.Row:
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO threads (owner_id, title, description, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (owner_id, title, description, now, now),
    )
    return require_thread(conn, cur.lastrowid)  # type: ignore[arg-type]


def list_threads(
    conn: sqlite3.Connection,
    *,
    scope_sql: str,
    scope_params: list,
    q: str | None,
    archived: bool | None,
    sort: str,
    order: str,
    limit: int,
    offset: int,
    group: str | None = None,
) -> tuple[list[sqlite3.Row], int]:
    where = [scope_sql.replace("owner_id", "t.owner_id")]
    params: list = list(scope_params)

    if group is not None:
        # UNGROUPED is a filter, not an id: the home screen pages each group
        # separately and "no group" is one of the sections it pages.
        if group == UNGROUPED:
            where.append("t.group_id IS NULL")
        else:
            where.append("t.group_id = ?")
            params.append(_group_id(group))

    if q:
        where.append("(t.title LIKE ? OR t.description LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like])

    if archived is not None:
        where.append("t.archived = ?")
        params.append(int(archived))

    where_sql = " AND ".join(where)
    order_col = SORTABLE.get(sort, SORTABLE["updated_at"])
    direction = "ASC" if order.lower() == "asc" else "DESC"

    total = conn.execute(
        f"SELECT COUNT(*) FROM threads t WHERE {where_sql}", params
    ).fetchone()[0]

    rows = conn.execute(
        f"""
        SELECT t.*, {THREAD_COUNTS_SQL}
          FROM threads t
         WHERE {where_sql}
         ORDER BY {order_col} {direction}, t.id DESC
         LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()

    return rows, total


def row_to_thread(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "owner_id": row["owner_id"],
        "title": row["title"],
        "description": row["description"],
        "archived": bool(row["archived"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "meeting_count": row["meeting_count"] if "meeting_count" in row.keys() else 0,
        "last_meeting_at": row["last_meeting_at"] if "last_meeting_at" in row.keys() else None,
        "email_count": row["email_count"] if "email_count" in row.keys() else 0,
        "event_count": row["event_count"] if "event_count" in row.keys() else 0,
        "note_count": row["note_count"] if "note_count" in row.keys() else 0,
        # Drives the blue dot on the thread card. Zero for every thread until
        # the sweep attaches something nobody has opened yet.
        "unread_count": row["unread_count"] if "unread_count" in row.keys() else 0,
        "auto_match_at": row["auto_match_at"] if "auto_match_at" in row.keys() else None,
        "auto_match_error": (
            row["auto_match_error"] if "auto_match_error" in row.keys() else None
        ),
        "next_step": row["next_step"] if "next_step" in row.keys() else None,
        "next_step_generated_at": (
            row["next_step_generated_at"] if "next_step_generated_at" in row.keys() else None
        ),
        # NULL is "Ungrouped", which is where every thread starts.
        "group_id": row["group_id"] if "group_id" in row.keys() else None,
        # Only the single-thread GET computes this (it costs an extra query);
        # the list endpoint doesn't show the suggestion, so this default stands.
        "next_step_stale": False,
    }


def compute_next_step_fingerprint(conn: sqlite3.Connection, thread_id: int) -> str:
    """A cheap fingerprint of everything a "next step" suggestion depends on.

    Changes whenever a meeting, email, calendar event or note is attached to the
    thread, or a meeting gets a new current summary -- exactly the events that
    should make a cached suggestion stale. Compared against
    ``threads.next_step_fingerprint`` on read rather than invalidated at every
    attach/create call site, the same way ``summaries.stale`` is derived from a
    transcript hash rather than cleared on write.

    A calendar event carries only its id: it is an immutable snapshot of
    something fetched. A note carries ``updated_at`` because it is edited in
    place, and rewriting one is exactly the kind of change that should move the
    suggestion.

    An email carries ``body_fetched_at``, which is the one that is easy to get
    wrong. An email's *content* is immutable, so the row looks like an event --
    but the row is filled in lazily: hydration adds the full body and its AI
    summary the first time someone opens the thread, and those are what the
    suggestion is actually reading. Keyed on the id alone, a thread's first
    hydration would silently fail to refresh anything and the cached suggestion
    would keep describing a thread it could only see the snippets of.

    Deliberately not the ``ai_summary`` text itself: it is derived from the body,
    so it adds no information the stamp does not already carry, and hashing
    multi-KB strings once per row on every home-page poll is pure waste. Nor
    does this call ``email_chains.build_chains`` -- grouping is a whole-thread
    computation and this runs per row on every list load.
    """
    rows = conn.execute(
        """
        SELECT 'm' || id || ':' || COALESCE(active_summary_id, 0) || ':' || updated_at AS token
          FROM meetings WHERE thread_id = ?
        UNION ALL
        SELECT 'e' || id || ':' || COALESCE(body_fetched_at, '')
          FROM thread_emails WHERE thread_id = ?
        UNION ALL
        SELECT 'c' || id FROM thread_calendar_events WHERE thread_id = ?
        UNION ALL
        SELECT 'n' || id || ':' || updated_at FROM thread_notes WHERE thread_id = ?
        ORDER BY 1
        """,
        (thread_id, thread_id, thread_id, thread_id),
    ).fetchall()
    raw = "|".join(r[0] for r in rows)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_next_step_stale(
    conn: sqlite3.Connection, thread_id: int, stored_fingerprint: str | None
) -> bool:
    """No suggestion yet is stale too -- that's what makes the box auto-generate."""
    if not stored_fingerprint:
        return True
    return compute_next_step_fingerprint(conn, thread_id) != stored_fingerprint


# A suggestion this old is refreshed even if nothing on the thread changed --
# the world outside it moves (a due date passes, "next week" becomes last
# week) in a way compute_next_step_fingerprint cannot see.
NEXT_STEP_MAX_AGE_DAYS = 14

# After a failed attempt, wait this long before trying again. Without this, a
# thread whose generation keeps failing (LLM misconfigured, provider down)
# would retry on every home-page poll -- the same request-storm shape
# followups.py's auto_match_at stamp exists to prevent.
NEXT_STEP_RETRY_COOLDOWN_MINUTES = 30


def _minutes_since(timestamp: str) -> float:
    """Minutes elapsed since an ISO-8601 timestamp from ``db.utcnow()``.

    Same parsing as ``matching.date_window``: ``fromisoformat`` matches
    ``utcnow()``'s own output exactly, with a naive-tzinfo guard for rows
    written before this field existed.
    """
    parsed = datetime.fromisoformat(timestamp)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).total_seconds() / 60


def next_step_needs_refresh(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """Whether the thread list should regenerate this thread's next step now.

    Three independent reasons, checked cheapest first: no suggestion yet,
    a recent failed attempt still in its cooldown window, the suggestion
    aged past ``NEXT_STEP_MAX_AGE_DAYS``, or its fingerprint no longer
    matches (``is_next_step_stale``, the only one costing an extra query).
    """
    keys = row.keys()
    checked_at = row["next_step_checked_at"] if "next_step_checked_at" in keys else None
    generated_at = row["next_step_generated_at"] if "next_step_generated_at" in keys else None
    # A successful generation stamps both columns with the exact same value.
    # Cool down only a later failed check; otherwise content attached just
    # after a successful generation would leave a known-stale suggestion on
    # the home page for thirty minutes.
    last_check_failed = bool(checked_at and checked_at != generated_at)
    if last_check_failed and _minutes_since(checked_at) < NEXT_STEP_RETRY_COOLDOWN_MINUTES:
        return False

    if not generated_at:
        return True
    if _minutes_since(generated_at) > NEXT_STEP_MAX_AGE_DAYS * 24 * 60:
        return True

    stored_fingerprint = row["next_step_fingerprint"] if "next_step_fingerprint" in keys else None
    return is_next_step_stale(conn, row["id"], stored_fingerprint)


def mark_seen(
    conn: sqlite3.Connection, *, thread_id: int, kind: str, item_id: int | None = None
) -> int:
    """Clear the unread mark on one attachment, or on all of a thread's.

    Returns how many rows changed, which is what lets the caller tell "already
    read" from "no such row" without a second query. ``seen_at`` is only ever
    written once: re-reading an item must not move the timestamp, or "attached
    while you were away, first opened at 09:14" stops being true.
    """
    table = UNREAD_TABLES[kind]
    sql = f"UPDATE {table} SET seen_at = ? WHERE thread_id = ? AND seen_at IS NULL"
    params: list = [utcnow(), thread_id]
    if item_id is not None:
        sql += " AND id = ?"
        params.append(item_id)
    return conn.execute(sql, params).rowcount


def move_item(
    conn: sqlite3.Connection, *, kind: str, thread_id: int, item_id: int, target_thread_id: int
) -> None:
    """Move one attached email or calendar event to another thread.

    Clears ``meeting_id``: ``matching.attached_context`` is scoped by meeting
    regardless of thread, so leaving it set would keep feeding a summary on a
    meeting this item no longer sits under. Reset to "seen" too, on the same
    reasoning as ``attach_event``/``attach_email`` -- a move is a person acting
    on the item, not the sweep finding it while nobody was looking.

    The unique index on (thread_id, uid) / (thread_id, message_id) means this
    can collide with something already attached to the destination -- reported
    as a conflict rather than merged, since picking whose score and reason wins
    is a call the pipeline shouldn't make silently.
    """
    table = UNREAD_TABLES[kind]
    try:
        cur = conn.execute(
            f"""
            UPDATE {table}
               SET thread_id = ?, meeting_id = NULL, auto_attached = 0, seen_at = ?
             WHERE id = ? AND thread_id = ?
            """,
            (target_thread_id, utcnow(), item_id, thread_id),
        )
    except sqlite3.IntegrityError:
        raise ConflictError("Already attached to the destination thread") from None
    if cur.rowcount == 0:
        raise NotFoundError("Not attached to this thread")


# --------------------------------------------------------------------------- #
# Meetings
# --------------------------------------------------------------------------- #

MEETING_EXTRAS_SQL = """
    (SELECT s.tldr FROM summaries s
      WHERE s.meeting_id = m.id AND s.is_current = 1 LIMIT 1)     AS summary_tldr,
    (SELECT COUNT(*) FROM action_items a
      WHERE a.meeting_id = m.id AND a.status = 'open')            AS open_action_items,
    (SELECT COUNT(*) FROM speaker_map sp WHERE sp.meeting_id = m.id) AS speaker_count
"""


def get_meeting(conn: sqlite3.Connection, meeting_id: int) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT m.*, {MEETING_EXTRAS_SQL} FROM meetings m WHERE m.id = ?",
        (meeting_id,),
    ).fetchone()


def require_meeting(conn: sqlite3.Connection, meeting_id: int) -> sqlite3.Row:
    row = get_meeting(conn, meeting_id)
    if row is None:
        raise NotFoundError("Meeting not found")
    return row


def create_meeting(
    conn: sqlite3.Connection,
    *,
    thread_id: int,
    owner_id: int,
    title: str,
    meeting_at: str | None = None,
    notes: str | None = None,
) -> sqlite3.Row:
    now = utcnow()
    cur = conn.execute(
        """
        INSERT INTO meetings (thread_id, owner_id, title, meeting_at, status,
                              notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'new', ?, ?, ?)
        """,
        (thread_id, owner_id, title, meeting_at or now, notes, now, now),
    )
    touch_thread(conn, thread_id)
    return require_meeting(conn, cur.lastrowid)  # type: ignore[arg-type]


def seed_speaker_names(conn: sqlite3.Connection, meeting_id: int, names: list[str]) -> int:
    """Park names the user gave before the recording was processed.

    They land on placeholder ids because the real SPEAKER_nn labels don't exist
    until diarization returns; the UI maps them afterwards by talk time. Shared
    by the upload form and by "create a meeting from a calendar event", which
    seeds them from the event's attendees.
    """
    parsed = [n.strip() for n in names if n and n.strip()]
    for i, name in enumerate(parsed):
        conn.execute(
            """
            INSERT INTO speaker_map (meeting_id, speaker_id, display_name, sort_order,
                                     source, updated_at)
            VALUES (?, ?, ?, ?, 'user_hint', ?)
            ON CONFLICT(meeting_id, speaker_id) DO UPDATE SET display_name = excluded.display_name
            """,
            (meeting_id, f"HINT_{i:02d}", name, i, utcnow()),
        )
    return len(parsed)


def list_meetings(
    conn: sqlite3.Connection,
    *,
    thread_id: int | None,
    scope_sql: str,
    scope_params: list,
    limit: int,
    offset: int,
) -> tuple[list[sqlite3.Row], int]:
    where = [scope_sql.replace("owner_id", "m.owner_id")]
    params: list = list(scope_params)

    if thread_id is not None:
        where.append("m.thread_id = ?")
        params.append(thread_id)

    where_sql = " AND ".join(where)

    total = conn.execute(
        f"SELECT COUNT(*) FROM meetings m WHERE {where_sql}", params
    ).fetchone()[0]

    rows = conn.execute(
        f"""
        SELECT m.*, {MEETING_EXTRAS_SQL}
          FROM meetings m
         WHERE {where_sql}
         ORDER BY m.meeting_at DESC, m.id DESC
         LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()

    return rows, total


def row_to_meeting(row: sqlite3.Row) -> dict:
    keys = row.keys()
    return {
        "id": row["id"],
        "thread_id": row["thread_id"],
        "owner_id": row["owner_id"],
        "title": row["title"],
        "meeting_at": row["meeting_at"],
        "status": row["status"],
        "original_filename": row["original_filename"],
        "original_bytes": row["original_bytes"],
        "audio_duration_sec": row["audio_duration_sec"],
        "audio_sample_rate": row["audio_sample_rate"],
        "audio_channels": row["audio_channels"],
        "audio_converted": bool(row["audio_converted"]),
        "has_audio": bool(row["audio_path"]),
        "has_transcript": row["active_diarization_id"] is not None,
        "has_summary": row["active_summary_id"] is not None,
        "notes": row["notes"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "summary_tldr": row["summary_tldr"] if "summary_tldr" in keys else None,
        "open_action_items": row["open_action_items"] if "open_action_items" in keys else 0,
        "speaker_count": row["speaker_count"] if "speaker_count" in keys else 0,
    }


# Tables scoped to one meeting that also carry their own thread_id, kept in
# lockstep with the meeting's by every attach path (see matching.attach_event /
# attach_email). Moving the meeting alone would strand them -- still on the old
# thread, naming a meeting that no longer lives there -- so a meeting move
# cascades to all four. ``jobs`` and ``match_runs`` matter even though nothing
# reads their thread_id today: both have ``thread_id ... ON DELETE CASCADE``,
# so leaving it stale means deleting the *old* thread later silently deletes a
# still-live meeting's job history and match runs out from under it.
_MEETING_SCOPED_UNIQUE_TABLES = {
    "calendar-events": "thread_calendar_events",
    "emails": "thread_emails",
}
_MEETING_SCOPED_TABLES = ("thread_notes", "match_runs", "jobs")


def move_meeting(
    conn: sqlite3.Connection, *, meeting_id: int, thread_id: int, target_thread_id: int
) -> sqlite3.Row:
    """Move a meeting -- and everything scoped to it -- to another thread.

    Same conflict rule as :func:`move_item`: a uid/message_id collision with
    something already attached to the destination thread raises rather than
    merging silently, which rolls the whole move back (the caller's
    connection commits or rolls back the request as one unit, so a conflict
    partway through a cascade never leaves the meeting moved but an
    attachment stranded behind).
    """
    cur = conn.execute(
        "UPDATE meetings SET thread_id = ?, updated_at = ? WHERE id = ? AND thread_id = ?",
        (target_thread_id, utcnow(), meeting_id, thread_id),
    )
    if cur.rowcount == 0:
        raise NotFoundError("Meeting not found on this thread")

    for kind, table in _MEETING_SCOPED_UNIQUE_TABLES.items():
        try:
            conn.execute(
                f"UPDATE {table} SET thread_id = ? WHERE meeting_id = ?",
                (target_thread_id, meeting_id),
            )
        except sqlite3.IntegrityError:
            noun = "event" if kind == "calendar-events" else "email"
            raise ConflictError(
                f"An attached {noun} on this meeting already exists on the destination thread"
            ) from None

    for table in _MEETING_SCOPED_TABLES:
        conn.execute(
            f"UPDATE {table} SET thread_id = ? WHERE meeting_id = ?",
            (target_thread_id, meeting_id),
        )

    return require_meeting(conn, meeting_id)
