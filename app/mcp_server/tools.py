"""What the MCP server can do (MMN-14): one plain function per tool.

Every tool here is an ordinary function -- ``(conn, principal, **args) -> dict``,
or ``async (principal, **args) -> dict`` for the one that has to talk to a
provider -- and :data:`TOOL_SPECS` is the registry ``server.py`` turns into MCP
tools. Keeping them free of any MCP type is what lets the tests call them
directly and the Settings page list them without importing the SDK.

Rules every tool follows, and that the tests hold them to:

* **The token owner's data only.** Admins included -- there is no ``all``
  flag here, because an agent should never be browsing other people's
  meetings. Someone else's id behaves exactly like a missing one
  (``not_found``), the same 404-not-403 rule as the REST API.
* **Read tools never write.** No ``touch_thread``, no ``seen_at``, no email
  hydration, no LLM call. Reading through an agent is not activity -- the same
  rule hydration follows. The single network call any read tool makes is
  ``get_upcoming_events`` (a live read of the user's calendars) and ``search``'s
  query embedding.
* **Bounded output.** Lists are paged; long text (transcripts, note and email
  bodies) is cut with an explicit ``next_offset``/``truncated`` so a client can
  ask for more rather than silently getting less.
* **Unknown is unknown.** An email with a NULL ``direction`` is reported as
  ``"unknown"``, never guessed -- see the Email conversations notes in CLAUDE.md.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from app.deps import CurrentUser
from app.errors import NotFoundError, ValidationError
from app.services import email_bodies as email_bodies_svc
from app.services import notes as notes_svc
from app.services import search as search_svc
from app.services import summarize as summarize_svc
from app.services import threads as threads_svc
from app.services import transcript as transcript_svc
from app.services import upcoming as upcoming_svc

LIST_LIMIT_MAX = 100
TRANSCRIPT_DEFAULT_CHARS = 40_000
TRANSCRIPT_MAX_CHARS = 200_000
# How much of a note/email body a *list* carries; the get_* tool has the rest.
BODY_PREVIEW_CHARS = 1_500
# A single get_note/get_email body is cut here, with `truncated` saying so.
BODY_MAX_CHARS = 100_000
SNIPPET_MARK = "**"
TIMELINE_DEFAULT_LIMIT = 50


@dataclass(frozen=True)
class Principal:
    """Who is calling, and with what. ``scope`` is ``read`` or ``read_write``;
    a session bearer (rather than an API token) is treated as ``read_write``,
    since it can already do everything through the REST API."""

    user: CurrentUser
    scope: str
    token_id: int | None = None

    @property
    def can_write(self) -> bool:
        return self.scope == "read_write"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    title: str
    fn: Callable[..., Any]
    write: bool = False
    #: Async tools take ``(principal, **args)`` and manage their own
    #: connections; sync ones take ``(conn, principal, **args)`` and run in a
    #: worker thread on a connection opened for the call.
    is_async: bool = False


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def server_time() -> str:
    return datetime.now(timezone.utc).isoformat()


def _limit(value: int | None, default: int = 20) -> int:
    if value is None:
        return default
    return max(1, min(int(value), LIST_LIMIT_MAX))


def _offset(value: int | None) -> int:
    return max(0, int(value or 0))


def _owned_thread(conn: sqlite3.Connection, p: Principal, thread_id: int) -> sqlite3.Row:
    row = threads_svc.get_thread(conn, int(thread_id))
    if row is None or row["owner_id"] != p.user.id:
        raise NotFoundError(f"Thread {thread_id} not found")
    return row


def _owned_meeting(conn: sqlite3.Connection, p: Principal, meeting_id: int) -> sqlite3.Row:
    row = threads_svc.get_meeting(conn, int(meeting_id))
    if row is None or row["owner_id"] != p.user.id:
        raise NotFoundError(f"Meeting {meeting_id} not found")
    return row


def _cut(text: str | None, limit: int) -> tuple[str | None, bool]:
    if text is None:
        return None, False
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _direction(value: str | None) -> str:
    return value if value in ("outbound", "inbound") else "unknown"


def _group_names(conn: sqlite3.Connection, owner_id: int) -> dict[int, str]:
    return {
        r["id"]: r["name"]
        for r in conn.execute(
            "SELECT id, name FROM thread_groups WHERE owner_id = ?", (owner_id,)
        ).fetchall()
    }


def _resolve_group(conn: sqlite3.Connection, p: Principal, group: str | None) -> str | None:
    """A group id, a group name (case-insensitive), or ``none`` for Ungrouped
    -> the value ``threads.list_threads`` takes."""
    if group is None or not str(group).strip():
        return None
    raw = str(group).strip()
    if raw.lower() in (threads_svc.UNGROUPED, "ungrouped"):
        return threads_svc.UNGROUPED
    names = _group_names(conn, p.user.id)
    if raw.isdigit() and int(raw) in names:
        return raw
    for gid, name in names.items():
        if name.casefold() == raw.casefold():
            return str(gid)
    raise NotFoundError(f"Group {raw!r} not found")


def _meeting_brief(row: sqlite3.Row, thread_title: str | None = None) -> dict:
    keys = row.keys()
    out = {
        "id": row["id"],
        "title": row["title"],
        "meeting_at": row["meeting_at"],
        "thread_id": row["thread_id"],
        "status": row["status"],
        "duration_sec": row["audio_duration_sec"],
        "has_transcript": row["active_diarization_id"] is not None,
        "has_summary": row["active_summary_id"] is not None,
        "summary_tldr": row["summary_tldr"] if "summary_tldr" in keys else None,
        "open_action_items": row["open_action_items"] if "open_action_items" in keys else 0,
    }
    if thread_title is not None:
        out["thread_title"] = thread_title
    return out


def _thread_brief(row: sqlite3.Row, groups: dict[int, str]) -> dict:
    data = threads_svc.row_to_thread(row)
    return {
        "id": data["id"],
        "title": data["title"],
        "description": data["description"],
        "archived": data["archived"],
        "group_id": data["group_id"],
        "group_name": groups.get(data["group_id"]) if data["group_id"] else None,
        "meeting_count": data["meeting_count"],
        "last_meeting_at": data["last_meeting_at"],
        "note_count": data["note_count"],
        "email_count": data["email_count"],
        "event_count": data["event_count"],
        "unread_count": data["unread_count"],
        "updated_at": data["updated_at"],
        "created_at": data["created_at"],
    }


def _note_out(note: dict, *, preview: bool) -> dict:
    body, truncated = _cut(note["body"], BODY_PREVIEW_CHARS if preview else BODY_MAX_CHARS)
    return {
        "id": note["id"],
        "thread_id": note["thread_id"],
        "meeting_id": note["meeting_id"],
        "title": note["title"],
        "body": body,
        "truncated": truncated,
        # Whose words these are: "manual" (the user), "ai_chat" (an AI answer
        # the user saved) or "mcp" (written by an agent through this server).
        "source": note["source"],
        "created_at": note["created_at"],
        "updated_at": note["updated_at"],
    }


def _email_out(row: dict) -> dict:
    return {
        "id": row["id"],
        "meeting_id": row.get("meeting_id"),
        "subject": row.get("subject"),
        "sender": row.get("sender"),
        "to": row.get("to_recipients"),
        "cc": row.get("cc_recipients"),
        "date": row.get("date"),
        "direction": _direction(row.get("direction")),
        "snippet": row.get("snippet"),
        "ai_summary": row.get("ai_summary"),
        "has_body": bool(row.get("has_body")),
        "account": row.get("account"),
        "url": row.get("url"),
    }


def _event_out(row: sqlite3.Row | dict) -> dict:
    get = row.get if isinstance(row, dict) else (lambda k: row[k] if k in row.keys() else None)
    return {
        "id": get("id"),
        "meeting_id": get("meeting_id"),
        "summary": get("summary"),
        "description": get("description"),
        "location": get("location"),
        # An all-day event keeps its bare date -- it is not midnight UTC.
        "start": get("start_at"),
        "end": get("end_at"),
        "calendar_name": get("calendar_name"),
        "account": get("account"),
        "url": get("url"),
    }


# --------------------------------------------------------------------------- #
# Threads
# --------------------------------------------------------------------------- #


def list_threads(
    conn: sqlite3.Connection,
    p: Principal,
    query: str | None = None,
    group: str | None = None,
    include_archived: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """List your threads (projects / recurring meeting series), most recently
    active first.

    `query` filters on thread title and description with the same rules as the
    home-screen search box (whole words, "quoted phrases", prefix*). `group` is
    a group id, a group name, or "none" for ungrouped threads. Use `search` to
    look inside meetings, transcripts, notes and emails instead.
    """
    size, start = _limit(limit), _offset(offset)
    rows, total = threads_svc.list_threads(
        conn,
        scope_sql="owner_id = ?",
        scope_params=[p.user.id],
        q=(query or None),
        archived=None if include_archived else False,
        sort="updated_at",
        order="desc",
        limit=size,
        offset=start,
        group=_resolve_group(conn, p, group),
    )
    groups = _group_names(conn, p.user.id)
    return {
        "server_time": server_time(),
        "total": total,
        "offset": start,
        "has_more": start + len(rows) < total,
        "threads": [_thread_brief(r, groups) for r in rows],
    }


def list_groups(conn: sqlite3.Connection, p: Principal) -> dict:
    """List your thread groups (the folders on the home screen) with how many
    threads each holds. Pass a group's name or id to `list_threads(group=...)`."""
    rows = conn.execute(
        """
        SELECT g.id, g.name,
               (SELECT COUNT(*) FROM threads t WHERE t.group_id = g.id) AS thread_count
          FROM thread_groups g
         WHERE g.owner_id = ?
         ORDER BY g.name COLLATE NOCASE, g.id
        """,
        (p.user.id,),
    ).fetchall()
    ungrouped = conn.execute(
        "SELECT COUNT(*) FROM threads WHERE owner_id = ? AND group_id IS NULL", (p.user.id,)
    ).fetchone()[0]
    return {
        "groups": [dict(r) for r in rows],
        "ungrouped_thread_count": ungrouped,
    }


def get_thread(conn: sqlite3.Connection, p: Principal, thread_id: int) -> dict:
    """One thread: its description, group, every meeting on it (newest first,
    with summary TL;DRs), how many notes/emails/calendar events are attached,
    and the cached "suggested next step" if one has been generated."""
    row = _owned_thread(conn, p, thread_id)
    groups = _group_names(conn, p.user.id)
    meetings = conn.execute(
        f"""
        SELECT m.*, {threads_svc.MEETING_EXTRAS_SQL}
          FROM meetings m
         WHERE m.thread_id = ?
         ORDER BY COALESCE(m.meeting_at, m.created_at) DESC, m.id DESC
        """,
        (row["id"],),
    ).fetchall()
    data = threads_svc.row_to_thread(row)
    stale = threads_svc.is_next_step_stale(
        conn,
        row["id"],
        row["next_step_fingerprint"] if "next_step_fingerprint" in row.keys() else None,
    )
    return {
        **_thread_brief(row, groups),
        "next_step": data["next_step"],
        "next_step_generated_at": data["next_step_generated_at"],
        "next_step_stale": stale if data["next_step"] else None,
        "meetings": [_meeting_brief(m) for m in meetings],
    }


def _timeline_entry(item) -> dict:
    payload = item.payload or {}
    base = {"kind": item.kind, "at": item.at, "id": item.id}
    if item.kind == "meeting":
        return {
            **base,
            "title": payload.get("title"),
            "status": payload.get("status"),
            "has_transcript": payload.get("has_transcript"),
            "summary_tldr": payload.get("summary_tldr"),
        }
    if item.kind == "event":
        return {**base, **{k: v for k, v in _event_out(payload).items() if k != "id"}}
    if item.kind == "email_chain":
        latest = (payload.get("messages") or [{}])[-1]
        return {
            **base,
            "subject": payload.get("subject"),
            "message_count": payload.get("message_count"),
            "participants": payload.get("participants"),
            "first_message_at": payload.get("first_message_at"),
            "last_message_at": payload.get("last_message_at"),
            # "you" / "them" / None (unknown) -- see email_chains._assemble.
            "last_message_from": payload.get("last_message_from"),
            "awaiting": payload.get("awaiting"),
            "latest_snippet": latest.get("snippet"),
            "latest_ai_summary": latest.get("ai_summary"),
            "email_ids": [m.get("id") for m in payload.get("messages") or []],
        }
    if item.kind == "note":
        body, truncated = _cut(payload.get("body"), 300)
        return {
            **base,
            "title": payload.get("title"),
            "source": payload.get("source"),
            "preview": body,
            "truncated": truncated,
        }
    return base


def get_thread_timeline(
    conn: sqlite3.Connection, p: Principal, thread_id: int, limit: int = TIMELINE_DEFAULT_LIMIT
) -> dict:
    """Everything on a thread in one date-sorted list (newest first): meetings,
    calendar events, email conversations (grouped) and notes -- the same
    timeline the thread page shows. Follow up with get_meeting / get_email /
    get_note for detail."""
    from app.routers import threads as threads_router

    _owned_thread(conn, p, thread_id)
    items = threads_router.thread_timeline(int(thread_id), user=p.user, conn=conn)
    size = _limit(limit, TIMELINE_DEFAULT_LIMIT)
    return {
        "thread_id": int(thread_id),
        "total": len(items),
        "has_more": len(items) > size,
        "items": [_timeline_entry(i) for i in items[:size]],
    }


# --------------------------------------------------------------------------- #
# Meetings
# --------------------------------------------------------------------------- #


def list_meetings(
    conn: sqlite3.Connection,
    p: Principal,
    thread_id: int | None = None,
    since: str | None = None,
    until: str | None = None,
    query: str | None = None,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """List meetings newest first, optionally within a date range.

    `since` / `until` take an ISO date (2026-09-21) or datetime; a bare `until`
    date includes that whole day. Work out "last week" from `server_time` in the
    response. `query` is a case-insensitive substring of the meeting title *or*
    its thread's title, so "the XX meeting" finds meetings filed under an "XX"
    thread too. `status` filters on the processing state (e.g. "done").
    """
    size, start = _limit(limit), _offset(offset)
    where = ["m.owner_id = ?"]
    params: list[Any] = [p.user.id]
    if thread_id is not None:
        _owned_thread(conn, p, thread_id)
        where.append("m.thread_id = ?")
        params.append(int(thread_id))
    lo = search_svc.parse_bound(since, name="since", upper=False)
    hi = search_svc.parse_bound(until, name="until", upper=True)
    if lo:
        where.append("COALESCE(m.meeting_at, m.created_at) >= ?")
        params.append(lo)
    if hi:
        where.append("COALESCE(m.meeting_at, m.created_at) < ?")
        params.append(hi)
    if query and query.strip():
        where.append("(m.title LIKE ? OR t.title LIKE ?)")
        like = f"%{query.strip()}%"
        params.extend([like, like])
    if status:
        where.append("m.status = ?")
        params.append(status)
    where_sql = " AND ".join(where)
    total = conn.execute(
        f"SELECT COUNT(*) FROM meetings m JOIN threads t ON t.id = m.thread_id WHERE {where_sql}",
        params,
    ).fetchone()[0]
    rows = conn.execute(
        f"""
        SELECT m.*, t.title AS thread_title, {threads_svc.MEETING_EXTRAS_SQL}
          FROM meetings m JOIN threads t ON t.id = m.thread_id
         WHERE {where_sql}
         ORDER BY COALESCE(m.meeting_at, m.created_at) DESC, m.id DESC
         LIMIT ? OFFSET ?
        """,
        [*params, size, start],
    ).fetchall()
    return {
        "server_time": server_time(),
        "total": total,
        "offset": start,
        "has_more": start + len(rows) < total,
        "meetings": [_meeting_brief(r, r["thread_title"]) for r in rows],
    }


def get_meeting(conn: sqlite3.Connection, p: Principal, meeting_id: int) -> dict:
    """One meeting's details: when, which thread, processing status, audio
    length, who spoke (display names, talk time, which one is "me"), whether a
    transcript and summary exist, and what notes/emails/calendar events are
    attached to it. Use get_meeting_transcript / get_meeting_summary next."""
    row = _owned_meeting(conn, p, meeting_id)
    thread = threads_svc.get_thread(conn, row["thread_id"])
    speakers: list[dict] = []
    if row["active_diarization_id"] is not None:
        try:
            transcript = transcript_svc.get_transcript(conn, row["id"])
            for sp in transcript_svc.speaker_stats(transcript):
                if sp.get("merged_into") or sp.get("hidden"):
                    continue
                speakers.append(
                    {
                        "id": sp["id"],
                        "name": sp.get("display_name") or sp.get("label") or sp["id"],
                        "is_me": bool(sp.get("is_me")),
                        "talk_time_sec": sp.get("total_speech_duration"),
                        "share": round(sp.get("share") or 0.0, 3),
                    }
                )
        except NotFoundError:
            pass

    def count(table: str) -> int:
        return conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE meeting_id = ?", (row["id"],)
        ).fetchone()[0]

    summary = conn.execute(
        "SELECT version, status, model, created_at FROM summaries "
        "WHERE meeting_id = ? AND is_current = 1",
        (row["id"],),
    ).fetchone()
    return {
        **_meeting_brief(row, thread["title"] if thread else None),
        "created_at": row["created_at"],
        "original_filename": row["original_filename"],
        "meeting_notes_field": row["notes"],
        "speakers": speakers,
        "summary": dict(summary) if summary else None,
        "attached": {
            "notes": count("thread_notes"),
            "emails": count("thread_emails"),
            "calendar_events": count("thread_calendar_events"),
        },
    }


def get_meeting_transcript(
    conn: sqlite3.Connection,
    p: Principal,
    meeting_id: int,
    format: Literal["markdown", "text", "vtt"] = "markdown",
    start_sec: float | None = None,
    end_sec: float | None = None,
    speaker: str | None = None,
    include_nonspeech: bool = False,
    offset: int = 0,
    max_chars: int = TRANSCRIPT_DEFAULT_CHARS,
) -> dict:
    """A meeting's transcript with speaker names applied.

    Long transcripts are paged: if `next_offset` is not null, call again with
    `offset=next_offset` for the rest. Narrow it with `start_sec`/`end_sec`
    (e.g. to jump to a `search` hit) or `speaker` (a display name or speaker
    id, case-insensitive). `format` is markdown (grouped by speaker turn),
    text (one "[mm:ss] Name: line" per segment) or vtt.
    """
    row = _owned_meeting(conn, p, meeting_id)
    if format not in ("markdown", "text", "vtt"):
        raise ValidationError("format must be one of: markdown, text, vtt")
    try:
        transcript = transcript_svc.get_transcript(
            conn, row["id"], include_nonspeech=include_nonspeech
        )
    except NotFoundError:
        return {
            "meeting_id": row["id"],
            "available": False,
            "status": row["status"],
            "message": f"No transcript yet (meeting status: {row['status']}).",
        }

    segments = transcript["segments"]
    # A segment is in the window if it overlaps it, strictly -- one that merely
    # ends where the window starts is the line *before* a search hit.
    if start_sec is not None:
        segments = [s for s in segments if (s.get("end") or s.get("start") or 0) > start_sec]
    if end_sec is not None:
        segments = [s for s in segments if (s.get("start") or 0) < end_sec]
    if speaker and speaker.strip():
        wanted = speaker.strip().casefold()
        segments = [
            s
            for s in segments
            if wanted in (str(s.get("speaker_name") or "").casefold(), str(s.get("speaker")).casefold())
        ]
    rendered = transcript_svc.render(
        {**transcript, "segments": segments}, "md" if format == "markdown" else format
    )
    budget = max(1_000, min(int(max_chars or TRANSCRIPT_DEFAULT_CHARS), TRANSCRIPT_MAX_CHARS))
    start = _offset(offset)
    text = rendered[start : start + budget]
    end = start + len(text)
    return {
        "meeting_id": row["id"],
        "title": row["title"],
        "meeting_at": row["meeting_at"],
        "available": True,
        "format": format,
        "segment_count": len(segments),
        "duration_sec": transcript.get("duration"),
        "speakers": sorted({s["speaker_name"] for s in segments if s.get("speaker_name")}),
        "total_chars": len(rendered),
        "offset": start,
        "next_offset": end if end < len(rendered) else None,
        "text": text,
    }


def _summary_out(summary: dict) -> dict:
    keep = (
        "version", "is_current", "status", "model", "created_at", "tldr", "summary_md",
        "title_suggestion", "key_decisions", "topics", "open_questions", "participants",
        "error", "stale",
    )
    out = {k: summary.get(k) for k in keep if k in summary}
    out["action_items"] = [
        {k: a.get(k) for k in ("id", "text", "owner_label", "due_text", "due_date", "priority", "status", "done_at")}
        for a in summary.get("action_items") or []
    ]
    return out


def get_meeting_summary(
    conn: sqlite3.Connection, p: Principal, meeting_id: int, version: int | None = None
) -> dict:
    """A meeting's AI summary: TL;DR, full markdown summary, key decisions,
    topics, open questions, participants and action items. Defaults to the
    current version; pass `version` for an older one. Says plainly when there
    is no summary (it never generates one). `stale: true` means speaker names
    or the transcript changed after it was written."""
    row = _owned_meeting(conn, p, meeting_id)
    base = {"meeting_id": row["id"], "title": row["title"], "meeting_at": row["meeting_at"]}
    if version is None:
        try:
            summary = summarize_svc.get_current_summary(conn, row["id"])
        except NotFoundError:
            return {
                **base,
                "available": False,
                "status": row["status"],
                "message": "This meeting has no summary yet.",
            }
    else:
        srow = conn.execute(
            "SELECT * FROM summaries WHERE meeting_id = ? AND version = ?", (row["id"], int(version))
        ).fetchone()
        if srow is None:
            raise NotFoundError(f"Summary version {version} not found")
        items = conn.execute(
            "SELECT * FROM action_items WHERE summary_id = ? ORDER BY idx", (srow["id"],)
        ).fetchall()
        summary = summarize_svc.row_to_summary(srow, items)
    available = summary.get("status") != "error"
    out = {**base, "available": available, **_summary_out(summary)}
    if not available:
        out["message"] = f"The latest summary attempt failed: {summary.get('error') or 'unknown error'}"
    return out


def list_action_items(
    conn: sqlite3.Connection,
    p: Principal,
    status: Literal["open", "done", "dropped", "all"] = "open",
    thread_id: int | None = None,
    meeting_id: int | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Action items from meetings' current summaries -- "what did I commit to
    last week?". Filter by status, thread, meeting, or the meeting's date
    (`since`/`until`, ISO dates). Newest meetings first."""
    if status not in ("open", "done", "dropped", "all"):
        raise ValidationError("status must be one of: open, done, dropped, all")
    size, start = _limit(limit, 50), _offset(offset)
    where = ["m.owner_id = ?", "s.is_current = 1"]
    params: list[Any] = [p.user.id]
    if status != "all":
        where.append("a.status = ?")
        params.append(status)
    if thread_id is not None:
        _owned_thread(conn, p, thread_id)
        where.append("m.thread_id = ?")
        params.append(int(thread_id))
    if meeting_id is not None:
        _owned_meeting(conn, p, meeting_id)
        where.append("m.id = ?")
        params.append(int(meeting_id))
    lo = search_svc.parse_bound(since, name="since", upper=False)
    hi = search_svc.parse_bound(until, name="until", upper=True)
    if lo:
        where.append("COALESCE(m.meeting_at, m.created_at) >= ?")
        params.append(lo)
    if hi:
        where.append("COALESCE(m.meeting_at, m.created_at) < ?")
        params.append(hi)
    where_sql = " AND ".join(where)
    joins = """
          FROM action_items a
          JOIN summaries s ON s.id = a.summary_id
          JOIN meetings m ON m.id = a.meeting_id
          JOIN threads t ON t.id = m.thread_id
    """
    total = conn.execute(f"SELECT COUNT(*) {joins} WHERE {where_sql}", params).fetchone()[0]
    rows = conn.execute(
        f"""
        SELECT a.*, m.title AS meeting_title, m.meeting_at, m.thread_id, t.title AS thread_title
        {joins}
         WHERE {where_sql}
         ORDER BY COALESCE(m.meeting_at, m.created_at) DESC, a.idx
         LIMIT ? OFFSET ?
        """,
        [*params, size, start],
    ).fetchall()
    return {
        "server_time": server_time(),
        "total": total,
        "offset": start,
        "has_more": start + len(rows) < total,
        "action_items": [
            {
                "id": r["id"],
                "text": r["text"],
                "owner": r["owner_label"],
                "due_text": r["due_text"],
                "due_date": r["due_date"],
                "priority": r["priority"],
                "status": r["status"],
                "done_at": r["done_at"],
                "meeting_id": r["meeting_id"],
                "meeting_title": r["meeting_title"],
                "meeting_at": r["meeting_at"],
                "thread_id": r["thread_id"],
                "thread_title": r["thread_title"],
            }
            for r in rows
        ],
    }


# --------------------------------------------------------------------------- #
# Notes
# --------------------------------------------------------------------------- #


def list_notes(
    conn: sqlite3.Connection,
    p: Principal,
    thread_id: int | None = None,
    meeting_id: int | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """Notes on a thread, on one meeting, or (with neither) your most recent
    notes everywhere. Newest first; bodies are previews -- use get_note for the
    full text. `source` says whose words a note is: "manual" (the user),
    "ai_chat" (a saved AI answer) or "mcp" (added by an agent)."""
    size, start = _limit(limit), _offset(offset)
    if meeting_id is not None:
        meeting = _owned_meeting(conn, p, meeting_id)
        if thread_id is not None and int(thread_id) != meeting["thread_id"]:
            raise NotFoundError(f"Meeting {meeting_id} is not on thread {thread_id}")
        notes = notes_svc.list_notes(conn, thread_id=meeting["thread_id"], meeting_id=meeting["id"])
    elif thread_id is not None:
        _owned_thread(conn, p, thread_id)
        notes = notes_svc.list_notes(conn, thread_id=int(thread_id))
    else:
        rows = conn.execute(
            """
            SELECT n.* FROM thread_notes n JOIN threads t ON t.id = n.thread_id
             WHERE t.owner_id = ?
             ORDER BY n.created_at DESC, n.id DESC
            """,
            (p.user.id,),
        ).fetchall()
        notes = [notes_svc.row_to_note(r) for r in rows]
    page = notes[start : start + size]
    return {
        "total": len(notes),
        "offset": start,
        "has_more": start + len(page) < len(notes),
        "notes": [_note_out(n, preview=True) for n in page],
    }


def _owned_note(conn: sqlite3.Connection, p: Principal, note_id: int) -> dict:
    row = conn.execute(
        "SELECT n.* FROM thread_notes n JOIN threads t ON t.id = n.thread_id "
        "WHERE n.id = ? AND t.owner_id = ?",
        (int(note_id), p.user.id),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"Note {note_id} not found")
    return notes_svc.row_to_note(row)


def get_note(conn: sqlite3.Connection, p: Principal, note_id: int) -> dict:
    """One note in full (markdown)."""
    return _note_out(_owned_note(conn, p, note_id), preview=False)


# --------------------------------------------------------------------------- #
# Emails and calendar events
# --------------------------------------------------------------------------- #


def list_emails(
    conn: sqlite3.Connection,
    p: Principal,
    thread_id: int,
    meeting_id: int | None = None,
    group_by_conversation: bool = True,
) -> dict:
    """Emails attached to a thread. By default grouped into conversations
    (oldest message first within each), with who sent the last message and
    who is waiting on whom. `direction` is "outbound" (you sent it), "inbound",
    or "unknown" -- never guessed. Passing `meeting_id` returns only the emails
    filed under that meeting, ungrouped (a per-meeting slice of a conversation
    is not the whole conversation). Bodies are not included: use get_email."""
    from app.routers import threads as threads_router

    _owned_thread(conn, p, thread_id)
    if meeting_id is not None:
        meeting = _owned_meeting(conn, p, meeting_id)
        if meeting["thread_id"] != int(thread_id):
            raise NotFoundError(f"Meeting {meeting_id} is not on thread {thread_id}")
    if meeting_id is not None or not group_by_conversation:
        sql = f"SELECT {email_bodies_svc.ROW_COLUMNS} FROM thread_emails WHERE thread_id = ?"
        params: list[Any] = [int(thread_id)]
        if meeting_id is not None:
            sql += " AND meeting_id = ?"
            params.append(int(meeting_id))
        rows = conn.execute(sql + " ORDER BY date DESC", params).fetchall()
        emails = [_email_out(threads_router._row_to_email(r)) for r in rows]
        return {"thread_id": int(thread_id), "grouped": False, "total": len(emails), "emails": emails}

    chains = threads_router._email_chains(conn, int(thread_id), p.user)
    chains.sort(key=lambda c: c.get("last_message_at") or "", reverse=True)
    return {
        "thread_id": int(thread_id),
        "grouped": True,
        "total": sum(c["message_count"] for c in chains),
        "conversations": [
            {
                "subject": c.get("subject"),
                "participants": c.get("participants"),
                "message_count": c.get("message_count"),
                "first_message_at": c.get("first_message_at"),
                "last_message_at": c.get("last_message_at"),
                "last_message_from": c.get("last_message_from"),
                "awaiting": c.get("awaiting"),
                "messages": [_email_out(m) for m in c.get("messages") or []],
            }
            for c in chains
        ],
    }


def get_email(conn: sqlite3.Connection, p: Principal, email_id: int) -> dict:
    """One attached email, with its body if the app has already fetched it.
    `body_status` is "stored", "not_fetched" (open the thread in the app to
    fetch it) or "unavailable" (this account cannot supply bodies). Never
    fetches anything itself."""
    from app.routers import threads as threads_router

    row = conn.execute(
        f"""
        SELECT {email_bodies_svc.ROW_COLUMNS}, body
          FROM thread_emails
         WHERE id = ? AND thread_id IN (SELECT id FROM threads WHERE owner_id = ?)
        """,
        (int(email_id), p.user.id),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"Email {email_id} not found")
    body, truncated = _cut(row["body"], BODY_MAX_CHARS)
    if row["body"] is not None:
        body_status = "stored"
    elif row["body_fetched_at"]:
        body_status = "unavailable"
    else:
        body_status = "not_fetched"
    return {
        **_email_out(threads_router._row_to_email(row)),
        "thread_id": row["thread_id"],
        "body": body,
        "body_truncated": truncated,
        "body_status": body_status,
    }


def list_calendar_events(
    conn: sqlite3.Connection,
    p: Principal,
    thread_id: int | None = None,
    meeting_id: int | None = None,
) -> dict:
    """Calendar events attached to a thread (or to one meeting on it), in
    start order. For your live calendar going forward, use get_upcoming_events."""
    if meeting_id is not None:
        meeting = _owned_meeting(conn, p, meeting_id)
        if thread_id is not None and int(thread_id) != meeting["thread_id"]:
            raise NotFoundError(f"Meeting {meeting_id} is not on thread {thread_id}")
        rows = conn.execute(
            "SELECT * FROM thread_calendar_events WHERE meeting_id = ? ORDER BY start_at",
            (meeting["id"],),
        ).fetchall()
    elif thread_id is not None:
        _owned_thread(conn, p, thread_id)
        rows = conn.execute(
            "SELECT * FROM thread_calendar_events WHERE thread_id = ? ORDER BY start_at",
            (int(thread_id),),
        ).fetchall()
    else:
        raise ValidationError("Pass thread_id or meeting_id")
    return {"total": len(rows), "events": [_event_out(r) for r in rows]}


async def get_upcoming_events(p: Principal, days: int = 7) -> dict:
    """Your upcoming calendar events across every connected calendar, from
    midnight today through the next `days` days (max 30). Each event says
    whether it is already attached to a thread. This reads your calendars live."""
    from app.db import get_conn

    result = await upcoming_svc.collect(lambda: get_conn(), user_id=p.user.id, days=int(days or 7))
    return {
        "server_time": server_time(),
        "days": max(1, min(int(days or 7), upcoming_svc.MAX_DAYS)),
        "connected_calendars": result.get("connected", 0),
        "error": result.get("error"),
        "events": [
            {
                "summary": e.get("summary"),
                "start": e.get("start"),
                "end": e.get("end"),
                "location": e.get("location"),
                "calendar_name": e.get("calendar_name"),
                "attendees": e.get("attendees"),
                "url": e.get("url"),
                "attached_to_thread": (e.get("attached") or {}).get("thread_id"),
            }
            for e in result.get("events") or []
        ],
    }


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #


def search(
    conn: sqlite3.Connection,
    p: Principal,
    query: str,
    kinds: list[str] | None = None,
    mode: Literal["hybrid", "keyword", "semantic"] = "hybrid",
    since: str | None = None,
    until: str | None = None,
    thread_id: int | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """Search everything you own: threads, meetings, transcript lines,
    summaries, action items, notes, emails and calendar events.

    `mode` "hybrid" (default) combines keyword matching with semantic
    (meaning-based) search when it is configured; "keyword" supports
    "quoted phrases" and prefix*. `kinds` narrows to any of: thread, meeting,
    segment (a transcript line), summary, action_item, note, email, event.
    A segment hit carries `meeting_id` and `start_sec` -- pass them to
    get_meeting_transcript(start_sec=...) to read around it. Matches in
    `snippet` are wrapped in **double asterisks**.
    """
    if not query or not query.strip():
        raise ValidationError("query must not be empty")
    if thread_id is not None:
        _owned_thread(conn, p, thread_id)
    result = search_svc.search(
        conn,
        owner_id=p.user.id,
        q=query.strip()[:500],
        kinds=kinds or None,
        since=since,
        until=until,
        thread_id=int(thread_id) if thread_id is not None else None,
        mode=mode,
        limit=_limit(limit),
        offset=_offset(offset),
    )
    hits = []
    for h in result["hits"]:
        snippet = (h.get("snippet") or "").replace(search_svc.MARK_OPEN, SNIPPET_MARK).replace(
            search_svc.MARK_CLOSE, SNIPPET_MARK
        )
        hits.append({**{k: v for k, v in h.items() if k not in ("url", "ref_id")}, "snippet": snippet})
    return {
        "server_time": server_time(),
        "query": result["query"],
        "mode_used": result["mode_used"],
        "semantic": result["semantic"],
        "offset": result["offset"],
        "has_more": result["has_more"],
        "hits": hits,
    }


# --------------------------------------------------------------------------- #
# Write tools (read_write tokens only)
# --------------------------------------------------------------------------- #


def create_note(
    conn: sqlite3.Connection,
    p: Principal,
    body: str,
    thread_id: int | None = None,
    meeting_id: int | None = None,
    title: str | None = None,
) -> dict:
    """Add a markdown note to a thread, or to one meeting on it (pass
    `meeting_id`; its thread is worked out for you). Leave `title` empty to have
    one generated. The note is labelled as written by an agent (source "mcp").
    Requires a read_write token."""
    from app.config import get_settings
    from app.schemas import NOTE_BODY_MAX

    body = (body or "").strip()
    if not body:
        raise ValidationError("body must not be empty")
    if len(body) > NOTE_BODY_MAX:
        raise ValidationError(f"body must be at most {NOTE_BODY_MAX} characters")
    if meeting_id is not None:
        meeting = _owned_meeting(conn, p, meeting_id)
        if thread_id is not None and int(thread_id) != meeting["thread_id"]:
            raise NotFoundError(f"Meeting {meeting_id} is not on thread {thread_id}")
        thread = _owned_thread(conn, p, meeting["thread_id"])
    elif thread_id is not None:
        meeting = None
        thread = _owned_thread(conn, p, thread_id)
    else:
        raise ValidationError("Pass thread_id or meeting_id")

    title = (title or "").strip()[: notes_svc.TITLE_MAX]
    title_model = None
    if not title:
        label = thread["title"] or ""
        if meeting is not None and meeting["title"]:
            label = f"{label} — {meeting['title']}" if label else meeting["title"]
        # Same "cannot fail the save" path as the REST route: an unreachable
        # model files the note under its own first line.
        title, title_model = notes_svc.generate_title_sync(
            get_settings().db_path, body=body, context_label=label
        )
    note = notes_svc.create_note(
        conn,
        thread_id=thread["id"],
        meeting_id=meeting["id"] if meeting is not None else None,
        title=title,
        body=body,
        source="mcp",
        user_id=p.user.id,
        title_model=title_model,
    )
    # A note is thread content like any other child write -- same as the REST
    # route, so the thread rises in the list and its next step goes stale.
    threads_svc.touch_thread(conn, thread["id"])
    return _note_out(note, preview=False)


def append_to_note(conn: sqlite3.Connection, p: Principal, note_id: int, body: str) -> dict:
    """Append markdown to the end of an existing note (separated by a rule).
    The title is left alone. Requires a read_write token."""
    from app.schemas import NOTE_BODY_MAX

    body = (body or "").strip()
    if not body:
        raise ValidationError("body must not be empty")
    note = _owned_note(conn, p, note_id)
    if len(note["body"]) + len(body) + len(notes_svc.APPEND_SEPARATOR) > NOTE_BODY_MAX:
        raise ValidationError(f"The note would exceed {NOTE_BODY_MAX} characters")
    updated = notes_svc.append_to_note(conn, thread_id=note["thread_id"], note_id=note["id"], body=body)
    threads_svc.touch_thread(conn, note["thread_id"])
    return _note_out(updated, preview=False)


def set_action_item_status(
    conn: sqlite3.Connection,
    p: Principal,
    item_id: int,
    status: Literal["open", "done", "dropped"],
) -> dict:
    """Mark an action item open, done or dropped (ids come from
    list_action_items or get_meeting_summary). Requires a read_write token."""
    if status not in ("open", "done", "dropped"):
        raise ValidationError("status must be one of: open, done, dropped")
    row = conn.execute("SELECT meeting_id FROM action_items WHERE id = ?", (int(item_id),)).fetchone()
    if row is None:
        raise NotFoundError(f"Action item {item_id} not found")
    try:
        _owned_meeting(conn, p, row["meeting_id"])
    except NotFoundError:
        raise NotFoundError(f"Action item {item_id} not found") from None
    item = summarize_svc.update_action_item(conn, int(item_id), {"status": status})
    return {k: item.get(k) for k in ("id", "meeting_id", "text", "owner_label", "status", "done_at")}


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("search", "Search everything", search),
    ToolSpec("list_threads", "List threads", list_threads),
    ToolSpec("list_groups", "List thread groups", list_groups),
    ToolSpec("get_thread", "Get a thread", get_thread),
    ToolSpec("get_thread_timeline", "Get a thread's timeline", get_thread_timeline),
    ToolSpec("list_meetings", "List meetings", list_meetings),
    ToolSpec("get_meeting", "Get a meeting", get_meeting),
    ToolSpec("get_meeting_transcript", "Get a meeting transcript", get_meeting_transcript),
    ToolSpec("get_meeting_summary", "Get a meeting summary", get_meeting_summary),
    ToolSpec("list_action_items", "List action items", list_action_items),
    ToolSpec("list_notes", "List notes", list_notes),
    ToolSpec("get_note", "Get a note", get_note),
    ToolSpec("list_emails", "List a thread's emails", list_emails),
    ToolSpec("get_email", "Get an email", get_email),
    ToolSpec("list_calendar_events", "List attached calendar events", list_calendar_events),
    ToolSpec("get_upcoming_events", "Get upcoming calendar events", get_upcoming_events, is_async=True),
    ToolSpec("create_note", "Create a note", create_note, write=True),
    ToolSpec("append_to_note", "Append to a note", append_to_note, write=True),
    ToolSpec("set_action_item_status", "Set an action item's status", set_action_item_status, write=True),
)

WRITE_TOOL_NAMES = frozenset(s.name for s in TOOL_SPECS if s.write)
