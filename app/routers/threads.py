"""Thread CRUD, the paginated list, and the merged timeline."""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3

from fastapi import APIRouter, Depends, Query

from app.config import get_settings
from app.db import get_conn, utcnow
from app.deps import (
    CurrentUser,
    active_user,
    assert_can_access,
    get_db,
    owner_scope,
    paginate,
)
from app.errors import NotFoundError
from app.logging_config import get_logger
from app.schemas import (
    EmailSummariseRequest,
    MeetingOut,
    MoveItemRequest,
    Page,
    ThreadCreateRequest,
    ThreadOut,
    ThreadUpdateRequest,
    TimelineItem,
)
from app.services import email_bodies as email_bodies_svc
from app.services import email_chains as email_chains_svc
from app.services import followups as followups_svc
from app.services import matching as matching_svc
from app.services import next_step as next_step_svc
from app.services import notes as notes_svc
from app.services import threads as threads_svc

router = APIRouter(prefix="/api/threads", tags=["threads"])
log = get_logger("threads")


@router.get("", response_model=Page[ThreadOut])
async def list_threads(
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1),
    q: str | None = Query(None, max_length=200),
    sort: str = Query("updated_at"),
    order: str = Query("desc"),
    archived: bool | None = Query(False),
    group: str | None = Query(
        None, description="A group id, or 'none' for ungrouped. Omit for every thread."
    ),
    all: bool = Query(False, description="Admins only: include other users' threads"),
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> Page[ThreadOut]:
    p, size, offset = paginate(page, page_size)
    scope_sql, scope_params = owner_scope(user, all)

    rows, total = threads_svc.list_threads(
        conn,
        scope_sql=scope_sql,
        scope_params=scope_params,
        q=q,
        archived=archived,
        sort=sort,
        order=order,
        limit=size,
        offset=offset,
        group=group,
    )

    items = [threads_svc.row_to_thread(r) for r in rows]

    # A page of the home screen doubles as "generate what's missing": any
    # thread here with no next step, a stale one, or one old enough to have
    # fallen out of date gets refreshed before the response goes out, capped
    # and cooled-down by next_step_svc so this can't turn into an LLM call on
    # every poll for a thread that keeps failing.
    stale_ids = [r["id"] for r in rows if threads_svc.next_step_needs_refresh(conn, r)]
    if stale_ids:
        refreshed = await next_step_svc.refresh_many(get_settings().db_path, stale_ids)
        by_id = {item["id"]: item for item in items}
        for thread_id, result in refreshed.items():
            if result.get("error"):
                continue
            item = by_id[thread_id]
            item["next_step"] = result["next_step"]
            item["next_step_generated_at"] = result["next_step_generated_at"]
            item["next_step_stale"] = False

    return Page[ThreadOut](
        items=[ThreadOut(**item) for item in items],
        page=p,
        page_size=size,
        total=total,
        total_pages=max(1, -(-total // size)),
    )


def _thread_out(conn: sqlite3.Connection, row: sqlite3.Row) -> ThreadOut:
    """``ThreadOut`` with ``next_step_stale`` actually computed.

    Only the single-thread responses (create/get/patch) pay for the extra
    fingerprint query; ``list_threads`` leaves it at ``row_to_thread``'s
    default ``False`` rather than doing it once per row on every page load.
    """
    data = threads_svc.row_to_thread(row)
    stored_fingerprint = row["next_step_fingerprint"] if "next_step_fingerprint" in row.keys() else None
    data["next_step_stale"] = threads_svc.is_next_step_stale(conn, row["id"], stored_fingerprint)
    return ThreadOut(**data)


@router.post("", response_model=ThreadOut, status_code=201)
def create_thread(
    payload: ThreadCreateRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> ThreadOut:
    row = threads_svc.create_thread(
        conn, owner_id=user.id, title=payload.title, description=payload.description
    )
    return _thread_out(conn, row)


@router.get("/{thread_id}", response_model=ThreadOut)
def get_thread(
    thread_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> ThreadOut:
    row = threads_svc.get_thread(conn, thread_id)
    assert_can_access(row, user)
    return _thread_out(conn, row)


@router.patch("/{thread_id}", response_model=ThreadOut)
def update_thread(
    thread_id: int,
    payload: ThreadUpdateRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> ThreadOut:
    row = threads_svc.get_thread(conn, thread_id)
    assert_can_access(row, user)

    updates: dict = {}
    if payload.title is not None:
        updates["title"] = payload.title
    if payload.description is not None:
        updates["description"] = payload.description
    if payload.archived is not None:
        updates["archived"] = int(payload.archived)

    if updates:
        updates["updated_at"] = utcnow()
        assignments = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE threads SET {assignments} WHERE id = ?",
            [*updates.values(), thread_id],
        )

    return _thread_out(conn, threads_svc.require_thread(conn, thread_id))


@router.delete("/{thread_id}")
def delete_thread(
    thread_id: int,
    purge_files: bool = Query(True, description="Also remove audio from disk"),
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    row = threads_svc.get_thread(conn, thread_id)
    assert_can_access(row, user)

    meeting_ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM meetings WHERE thread_id = ?", (thread_id,)
        ).fetchall()
    ]

    # The DB cascade handles rows; audio lives on disk and needs explicit removal.
    removed = 0
    if purge_files:
        audio_root = get_settings().audio_dir
        for mid in meeting_ids:
            target = audio_root / str(mid)
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
                removed += 1

    conn.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
    log.info(
        "user %s deleted thread %s (%d meetings, %d audio dirs)",
        user.username,
        thread_id,
        len(meeting_ids),
        removed,
    )
    return {"ok": True, "deleted_meetings": len(meeting_ids), "purged_audio_dirs": removed}


@router.get("/{thread_id}/meetings", response_model=Page[MeetingOut])
def list_thread_meetings(
    thread_id: int,
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1),
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> Page[MeetingOut]:
    thread = threads_svc.get_thread(conn, thread_id)
    assert_can_access(thread, user)

    p, size, offset = paginate(page, page_size)
    # Scope by the thread we already authorised rather than re-deriving it.
    rows, total = threads_svc.list_meetings(
        conn,
        thread_id=thread_id,
        scope_sql="1=1",
        scope_params=[],
        limit=size,
        offset=offset,
    )

    return Page[MeetingOut](
        items=[MeetingOut(**threads_svc.row_to_meeting(r)) for r in rows],
        page=p,
        page_size=size,
        total=total,
        total_pages=max(1, -(-total // size)),
    )


# --------------------------------------------------------------------------- #
# Attachments
# --------------------------------------------------------------------------- #


def _row_to_email(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "meeting_id": row["meeting_id"],
        "message_id": row["message_id"],
        "sender": row["sender"],
        "subject": row["subject"],
        "date": row["date"],
        "snippet": row["snippet"],
        "account": row["account"],
        "triage_level": row["triage_level"],
        "tag": row["tag"],
        "summary": row["summary"],
        "score": row["score"],
        "relevance_score": row["relevance_score"],
        "relevance_reason": row["relevance_reason"],
        "attached_at": row["attached_at"],
        # Late columns: absent on rows written before the provider refactor, so
        # read them defensively rather than assuming the column is populated.
        "url": _optional(row, "url"),
        "rfc_message_id": _optional(row, "rfc_message_id"),
        "provider": _optional(row, "provider"),
        "conversation_id": _optional(row, "conversation_id"),
        "to_recipients": _optional(row, "to_recipients"),
        "cc_recipients": _optional(row, "cc_recipients"),
        # NULL means "we genuinely could not tell", and every surface must render
        # it as unknown rather than picking a side.
        "direction": _optional(row, "direction"),
        "ai_summary": _optional(row, "ai_summary"),
        "ai_summary_model": _optional(row, "ai_summary_model"),
        # `has_body` and `body_fetched_at` together are the three states the UI
        # needs: not fetched yet, fetched, or asked-and-this-account-cannot.
        # The body itself is deliberately absent -- see EMAIL_COLUMNS.
        "has_body": bool(_optional(row, "has_body")),
        "body_fetched_at": _optional(row, "body_fetched_at"),
        **_unread(row),
    }


def _optional(row: sqlite3.Row, column: str):
    """Read a column that may predate this row, without raising."""
    return row[column] if column in row.keys() else None


def _unread(row: sqlite3.Row) -> dict:
    """The unread mark, derived once so both attachment kinds agree on it.

    ``unread`` is the only thing the SPA branches on; the two columns behind it
    are returned as well because "the app attached this for you" is worth saying
    in the UI even after it has been read.
    """
    auto = bool(_optional(row, "auto_attached"))
    seen_at = _optional(row, "seen_at")
    return {"auto_attached": auto, "seen_at": seen_at, "unread": auto and seen_at is None}


def _row_to_event(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "meeting_id": row["meeting_id"],
        "uid": row["uid"],
        "url": row["url"],
        "summary": row["summary"],
        "description": row["description"],
        "location": row["location"],
        "start_at": row["start_at"],
        "end_at": row["end_at"],
        "calendar_name": row["calendar_name"],
        "account": row["account"],
        "type": row["event_type"],
        "relevance_score": row["relevance_score"],
        "relevance_reason": row["relevance_reason"],
        "attached_at": row["attached_at"],
        "source_uid": _optional(row, "source_uid"),
        "provider": _optional(row, "provider"),
        **_unread(row),
    }


@router.get("/{thread_id}/emails")
def list_thread_emails(
    thread_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    assert_can_access(threads_svc.get_thread(conn, thread_id), user)
    rows = conn.execute(
        f"SELECT {email_bodies_svc.ROW_COLUMNS} FROM thread_emails "
        "WHERE thread_id = ? ORDER BY date DESC",
        (thread_id,),
    ).fetchall()
    return [_row_to_email(r) for r in rows]


def _account_addresses(conn: sqlite3.Connection, user_id: int) -> list[str]:
    """This user's own connected addresses, for the chain grouper.

    Needed because *every* message in your own mailbox shares you as a
    participant, so without subtracting these the participant-overlap test would
    merge every subject-matched thread you are on. `thread_emails.account` is
    included as well as the integration label: it is what the provider recorded
    at attach time, and it is the only account identity a row that predates a
    disconnected integration still has.
    """
    rows = conn.execute(
        "SELECT account_label AS a FROM integrations WHERE user_id = ? "
        "UNION SELECT DISTINCT account AS a FROM thread_emails te "
        "JOIN threads t ON t.id = te.thread_id WHERE t.owner_id = ?",
        (user_id, user_id),
    ).fetchall()
    return [r["a"] for r in rows if r["a"]]


def _email_chains(
    conn: sqlite3.Connection, thread_id: int, user: CurrentUser
) -> list[dict]:
    """This thread's emails, grouped into conversations.

    Grouping is computed on read rather than stored -- see
    ``services/email_chains`` for why -- so this is the one place that turns rows
    into chains, shared by the timeline and the standalone chains route.
    """
    rows = conn.execute(
        f"SELECT {email_bodies_svc.ROW_COLUMNS} FROM thread_emails WHERE thread_id = ?",
        (thread_id,),
    ).fetchall()
    if not rows:
        return []

    payloads = [_row_to_email(r) for r in rows]
    chains = email_chains_svc.build_chains(
        payloads, account_addresses=_account_addresses(conn, user.id)
    )
    for chain in chains:
        # The earliest message's row id: stable as the conversation grows.
        chain["root_id"] = chain["messages"][0]["id"]
    return chains


@router.get("/{thread_id}/email-chains")
def list_thread_email_chains(
    thread_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    """Additive: ``GET /emails`` keeps its flat shape for anything still using it."""
    assert_can_access(threads_svc.get_thread(conn, thread_id), user)
    return _email_chains(conn, thread_id, user)


@router.get("/{thread_id}/emails/{email_id}/body")
def get_email_body(
    thread_id: int,
    email_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """One email's stored body. Its own route because the list routes never
    carry bodies -- SQLite reads whole rows."""
    assert_can_access(threads_svc.get_thread(conn, thread_id), user)
    row = email_bodies_svc.body_of(conn, thread_id, email_id)
    if row is None:
        raise NotFoundError("Email not attached to this thread")
    return {
        "id": row["id"],
        "body": row["body"],
        "body_fetched_at": row["body_fetched_at"],
        "has_body": bool(row["has_body"]),
        "ai_summary": row["ai_summary"],
        "ai_summary_model": row["ai_summary_model"],
    }


@router.post("/{thread_id}/emails/hydrate")
async def hydrate_thread_emails(
    thread_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Fetch bodies for this thread's un-hydrated emails, bounded per call.

    Bodies only -- no LLM call happens here. Summaries are opt-in, on the route
    below, because one model call per message is spend and latency nobody asked
    for. The response carries ``remaining`` so the SPA can keep going rather than
    leaving a long thread half-filled.

    Deliberately synchronous rather than a queued job: this fires on every thread
    open, so the progress dock would fill with one-second jobs while a 40-minute
    diarization scrolled out of sight. See ``services/email_bodies``.
    """
    thread = threads_svc.get_thread(conn, thread_id)
    assert_can_access(thread, user)
    # The request connection must not be held across provider round trips, so the
    # service opens its own short-lived ones -- same rule as the match route.
    return await email_bodies_svc.hydrate_thread_emails(
        None, thread_id=thread_id, user_id=user.id
    )


@router.post("/{thread_id}/emails/summarise")
async def summarise_thread_emails(
    thread_id: int,
    payload: EmailSummariseRequest | None = None,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Summarise stored email bodies that have none yet, on request.

    Scoped to specific ids when the caller names them -- which is how the
    "Summarise" button on one conversation avoids paying for every other
    conversation on the thread. Ids are filtered to this thread by the query, so
    a foreign id simply selects nothing.

    Pressing it again *is* the retry: a failed summary leaves both columns NULL
    and stays selectable, unlike a failed body fetch which is stamped so a
    provider that cannot supply one is never re-asked.
    """
    thread = threads_svc.get_thread(conn, thread_id)
    assert_can_access(thread, user)
    return await email_bodies_svc.summarise_thread_emails(
        None, thread_id=thread_id, email_ids=(payload.email_ids if payload else None)
    )


@router.post("/{thread_id}/emails/{email_id}/hydrate")
async def hydrate_one_email(
    thread_id: int,
    email_id: int,
    force: bool = False,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """One email, for expand-on-demand and for retrying a failure.

    ``force`` is what re-asks a provider that previously returned nothing; the
    ordinary path deliberately never does, or an account with no fetch-by-id tool
    would be re-asked on every page view.
    """
    thread = threads_svc.get_thread(conn, thread_id)
    assert_can_access(thread, user)
    if conn.execute(
        "SELECT 1 FROM thread_emails WHERE id = ? AND thread_id = ?",
        (email_id, thread_id),
    ).fetchone() is None:
        raise NotFoundError("Email not attached to this thread")

    return await email_bodies_svc.hydrate_thread_emails(
        None, thread_id=thread_id, user_id=user.id, email_id=email_id, force=force
    )


@router.delete("/{thread_id}/emails/{email_id}")
def detach_email(
    thread_id: int,
    email_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    assert_can_access(threads_svc.get_thread(conn, thread_id), user)
    cur = conn.execute(
        "DELETE FROM thread_emails WHERE id = ? AND thread_id = ?", (email_id, thread_id)
    )
    if cur.rowcount == 0:
        raise NotFoundError("Email not attached to this thread")
    return {"ok": True}


@router.post("/{thread_id}/emails/{email_id}/move")
def move_email(
    thread_id: int,
    email_id: int,
    payload: MoveItemRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    assert_can_access(threads_svc.get_thread(conn, thread_id), user)
    assert_can_access(threads_svc.get_thread(conn, payload.target_thread_id), user)
    threads_svc.move_item(
        conn,
        kind="emails",
        thread_id=thread_id,
        item_id=email_id,
        target_thread_id=payload.target_thread_id,
    )
    threads_svc.touch_thread(conn, payload.target_thread_id)
    return {"ok": True}


@router.get("/{thread_id}/calendar-events")
def list_thread_events(
    thread_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    assert_can_access(threads_svc.get_thread(conn, thread_id), user)
    rows = conn.execute(
        "SELECT * FROM thread_calendar_events WHERE thread_id = ? ORDER BY start_at",
        (thread_id,),
    ).fetchall()
    return [_row_to_event(r) for r in rows]


@router.delete("/{thread_id}/calendar-events/{event_id}")
def detach_event(
    thread_id: int,
    event_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    assert_can_access(threads_svc.get_thread(conn, thread_id), user)
    cur = conn.execute(
        "DELETE FROM thread_calendar_events WHERE id = ? AND thread_id = ?",
        (event_id, thread_id),
    )
    if cur.rowcount == 0:
        raise NotFoundError("Event not attached to this thread")
    return {"ok": True}


@router.post("/{thread_id}/calendar-events/{event_id}/move")
def move_event(
    thread_id: int,
    event_id: int,
    payload: MoveItemRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    assert_can_access(threads_svc.get_thread(conn, thread_id), user)
    assert_can_access(threads_svc.get_thread(conn, payload.target_thread_id), user)
    threads_svc.move_item(
        conn,
        kind="calendar-events",
        thread_id=thread_id,
        item_id=event_id,
        target_thread_id=payload.target_thread_id,
    )
    threads_svc.touch_thread(conn, payload.target_thread_id)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# The unread mark
#
# Only the periodic sweep creates unread rows, and reading is one-way: there is
# no "mark as unread". The point of the mark is "this arrived without you", and
# that stops being true the moment you look at it.
# --------------------------------------------------------------------------- #


def _mark_read(conn, thread_id: int, user, kind: str, item_id: int) -> dict:
    assert_can_access(threads_svc.get_thread(conn, thread_id), user)
    changed = threads_svc.mark_seen(conn, thread_id=thread_id, kind=kind, item_id=item_id)
    if changed == 0:
        # Zero rows means "already read" as often as it means "no such row", and
        # the SPA fires this on every click of an item's link -- including the
        # second one. 404ing that would put an error toast on a working link.
        exists = conn.execute(
            f"SELECT 1 FROM {threads_svc.UNREAD_TABLES[kind]} WHERE id = ? AND thread_id = ?",
            (item_id, thread_id),
        ).fetchone()
        if exists is None:
            raise NotFoundError("Not attached to this thread")
    return {"ok": True, "marked": changed}


@router.post("/{thread_id}/emails/{email_id}/read")
def mark_email_read(
    thread_id: int,
    email_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    return _mark_read(conn, thread_id, user, "emails", email_id)


@router.post("/{thread_id}/calendar-events/{event_id}/read")
def mark_event_read(
    thread_id: int,
    event_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    return _mark_read(conn, thread_id, user, "calendar-events", event_id)


@router.post("/{thread_id}/read")
def mark_thread_read(
    thread_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Clear every unread mark on the thread at once."""
    assert_can_access(threads_svc.get_thread(conn, thread_id), user)
    marked = sum(
        threads_svc.mark_seen(conn, thread_id=thread_id, kind=kind)
        for kind in threads_svc.UNREAD_TABLES
    )
    return {"ok": True, "marked": marked}


@router.post("/{thread_id}/follow-ups")
async def check_follow_ups(
    thread_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Run the periodic sweep for this thread now, without waiting for its turn.

    Same code path as the scheduler, including the confidence threshold: this is
    "look now", not "attach more freely". It takes its own short-lived
    connections for the same reason the upcoming list does -- there are provider
    round trips in the middle, and the request connection would sit inside them.
    """
    assert_can_access(threads_svc.get_thread(conn, thread_id), user)
    result = await followups_svc.sweep_thread(
        get_conn, thread_id=thread_id, user_id=user.id
    )
    log.info(
        "user %s swept thread %s: %d event(s), %d email(s) attached",
        user.username, thread_id, result["attached_events"], result["attached_emails"],
    )
    return result


@router.post("/{thread_id}/next-step")
async def refresh_next_step(
    thread_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Regenerate the cached "what's next" suggestion, one LLM call.

    Run off the event loop via ``to_thread``: it's a blocking HTTP round trip,
    the same reason ``rank_sync`` never runs inline in a coroutine. On failure
    the response carries ``error`` and the thread's previously cached
    suggestion is untouched -- see :func:`next_step_svc.generate_sync`.
    """
    assert_can_access(threads_svc.get_thread(conn, thread_id), user)
    result = await asyncio.to_thread(
        next_step_svc.generate_sync, get_settings().db_path, thread_id
    )
    if result.get("error"):
        log.warning("next-step refresh failed for thread %s: %s", thread_id, result["error"])
    return result


@router.get("/{thread_id}/timeline", response_model=list[TimelineItem])
def thread_timeline(
    thread_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[TimelineItem]:
    """Meetings, calendar events, email conversations and notes, date-sorted.

    Merged server-side so the SPA renders one array. Emails arrive grouped into
    conversations (``kind="email_chain"``) rather than one item per message --
    grouped *here* rather than in the client for two reasons: the client's
    day-bucketing only compares against the previous group, so it is correct only
    for a pre-sorted array; and re-sorting client-side would mean a second
    implementation of ``normalize_timestamp``, which is the rule that stops a
    legacy RFC 2822 row sorting above every ISO one.

    Note that chains make the timeline non-append-only: a chain moves out of its
    old day bucket when a reply lands. So the "load older can page coherently"
    premise no longer holds for emails -- don't add paging without deciding that
    chains page on ``last_message_at`` alone.
    """
    assert_can_access(threads_svc.get_thread(conn, thread_id), user)

    items: list[TimelineItem] = []

    for r in conn.execute(
        f"SELECT m.*, {threads_svc.MEETING_EXTRAS_SQL} FROM meetings m WHERE m.thread_id = ?",
        (thread_id,),
    ):
        items.append(
            TimelineItem(
                kind="meeting",
                at=r["meeting_at"] or r["created_at"],
                id=r["id"],
                payload=threads_svc.row_to_meeting(r),
            )
        )

    for r in conn.execute(
        "SELECT * FROM thread_calendar_events WHERE thread_id = ?", (thread_id,)
    ):
        items.append(
            TimelineItem(kind="event", at=r["start_at"], id=r["id"], payload=_row_to_event(r))
        )

    for chain in _email_chains(conn, thread_id, user):
        items.append(
            TimelineItem(
                kind="email_chain",
                # The newest message: a conversation sits on the timeline where
                # it last moved.
                at=chain["last_message_at"],
                # The *root* message's row id, not the newest. The SPA keys cards
                # on `${kind}-${id}`, so tracking the newest message would change
                # the key when a reply arrives, remounting the card and wiping the
                # reader's expanded state on a background refetch.
                id=chain["root_id"],
                payload=chain,
            )
        )

    # Dated by when it was written, not when it was last edited: a note's place
    # on the timeline is "what I was working on that day", and fixing a typo
    # three weeks later should not jump it to the top.
    for note in notes_svc.list_notes(conn, thread_id=thread_id):
        items.append(
            TimelineItem(kind="note", at=note["created_at"], id=note["id"], payload=note)
        )

    # Sort on a normalised timestamp: rows written before dates were coerced may
    # still hold RFC 2822, which sorts lexically above ISO-8601 and would put an
    # older email at the top of the timeline.
    def sort_key(item: TimelineItem) -> tuple[bool, str]:
        normalised = matching_svc.normalize_timestamp(item.at)
        return (normalised is None, normalised or "")

    items.sort(key=sort_key, reverse=True)
    return items
