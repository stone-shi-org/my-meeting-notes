"""Notes on a thread, and on one meeting within it.

Two routers over the same table, because the two callers know two different
ids: the thread page has a ``thread_id``, the transcript page has a
``meeting_id`` and should not have to look its thread up first. The meeting
routes resolve the thread themselves and then do exactly what the thread routes
do — there is no second code path, only a second entry point.
"""

from __future__ import annotations

import asyncio
import sqlite3

from fastapi import APIRouter, Depends, Query

from app.config import get_settings
from app.deps import CurrentUser, active_user, assert_can_access, get_db
from app.errors import NotFoundError
from app.logging_config import get_logger
from app.schemas import (
    MoveItemRequest,
    NoteAppendRequest,
    NoteCreateRequest,
    NoteOut,
    NoteUpdateRequest,
)
from app.services import notes as notes_svc
from app.services import threads as threads_svc

router = APIRouter(prefix="/api/threads", tags=["notes"])
meeting_router = APIRouter(prefix="/api/meetings", tags=["notes"])
log = get_logger("notes")


def _authorised_thread(conn: sqlite3.Connection, thread_id: int, user: CurrentUser) -> sqlite3.Row:
    row = threads_svc.get_thread(conn, thread_id)
    assert_can_access(row, user)
    return row


def _authorised_meeting(conn: sqlite3.Connection, meeting_id: int, user: CurrentUser) -> sqlite3.Row:
    row = threads_svc.get_meeting(conn, meeting_id)
    assert_can_access(row, user)
    return row


async def _create(
    conn: sqlite3.Connection,
    *,
    thread: sqlite3.Row,
    meeting: sqlite3.Row | None,
    payload: NoteCreateRequest,
    user: CurrentUser,
) -> NoteOut:
    """Title the note if it arrived without one, then write it.

    The generation step is a blocking HTTP round trip, so it goes through
    ``to_thread`` on its own short-lived connection — same shape as
    ``threads.refresh_next_step``. It cannot fail the request: an unreachable
    model means the note is filed under its own first line, because the body is
    the part worth keeping.
    """
    title = (payload.title or "").strip()
    title_model: str | None = None

    if not title:
        label = thread["title"] or ""
        if meeting is not None and meeting["title"]:
            label = f"{label} — {meeting['title']}" if label else meeting["title"]
        title, title_model = await asyncio.to_thread(
            notes_svc.generate_title_sync,
            get_settings().db_path,
            body=payload.body,
            question=payload.question,
            context_label=label,
            model=payload.model,
        )

    note = notes_svc.create_note(
        conn,
        thread_id=thread["id"],
        meeting_id=meeting["id"] if meeting is not None else None,
        title=title,
        body=payload.body,
        source=payload.source,
        user_id=user.id,
        model=payload.model,
        title_model=title_model,
    )
    # A note is thread content like any other child write, so the thread rises
    # in the list -- and its cached next step goes stale off the fingerprint.
    threads_svc.touch_thread(conn, thread["id"])
    log.info("user %s added note %s to thread %s", user.username, note["id"], thread["id"])
    return NoteOut(**note)


# --------------------------------------------------------------------------- #
# Thread-scoped
# --------------------------------------------------------------------------- #


@router.get("/{thread_id}/notes", response_model=list[NoteOut])
def list_thread_notes(
    thread_id: int,
    meeting_id: int | None = Query(None, description="Only notes filed on this meeting"),
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[NoteOut]:
    _authorised_thread(conn, thread_id, user)
    return [
        NoteOut(**n)
        for n in notes_svc.list_notes(conn, thread_id=thread_id, meeting_id=meeting_id)
    ]


@router.post("/{thread_id}/notes", response_model=NoteOut, status_code=201)
async def create_thread_note(
    thread_id: int,
    payload: NoteCreateRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> NoteOut:
    thread = _authorised_thread(conn, thread_id, user)
    meeting = None
    if payload.meeting_id is not None:
        # Authorised through the thread we already checked rather than on its
        # own. A meeting on someone else's thread 404s here exactly as it would
        # on its own route -- and a meeting on one of *this* user's other
        # threads is refused too, because filing it would put the note on a
        # timeline the request never named.
        meeting = threads_svc.require_meeting(conn, payload.meeting_id)
        if meeting["thread_id"] != thread_id:
            raise NotFoundError("Meeting not found on this thread")
    return await _create(conn, thread=thread, meeting=meeting, payload=payload, user=user)


@router.patch("/{thread_id}/notes/{note_id}", response_model=NoteOut)
def update_thread_note(
    thread_id: int,
    note_id: int,
    payload: NoteUpdateRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> NoteOut:
    _authorised_thread(conn, thread_id, user)
    note = notes_svc.update_note(
        conn, thread_id=thread_id, note_id=note_id, title=payload.title, body=payload.body
    )
    threads_svc.touch_thread(conn, thread_id)
    return NoteOut(**note)


@router.post("/{thread_id}/notes/{note_id}/append", response_model=NoteOut)
def append_thread_note(
    thread_id: int,
    note_id: int,
    payload: NoteAppendRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> NoteOut:
    """Add another answer to the end of an existing note.

    No title generation here on purpose: the note already has a name the user
    chose to add to, and silently renaming it under them would be worse than a
    title that no longer covers every paragraph.
    """
    _authorised_thread(conn, thread_id, user)
    note = notes_svc.append_to_note(conn, thread_id=thread_id, note_id=note_id, body=payload.body)
    threads_svc.touch_thread(conn, thread_id)
    return NoteOut(**note)


@router.post("/{thread_id}/notes/{note_id}/move", response_model=NoteOut)
def move_thread_note(
    thread_id: int,
    note_id: int,
    payload: MoveItemRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> NoteOut:
    _authorised_thread(conn, thread_id, user)
    _authorised_thread(conn, payload.target_thread_id, user)
    note = notes_svc.move_note(
        conn, thread_id=thread_id, note_id=note_id, target_thread_id=payload.target_thread_id
    )
    threads_svc.touch_thread(conn, payload.target_thread_id)
    return NoteOut(**note)


@router.delete("/{thread_id}/notes/{note_id}")
def delete_thread_note(
    thread_id: int,
    note_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    _authorised_thread(conn, thread_id, user)
    notes_svc.delete_note(conn, thread_id=thread_id, note_id=note_id)
    threads_svc.touch_thread(conn, thread_id)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Meeting-scoped
# --------------------------------------------------------------------------- #


@meeting_router.get("/{meeting_id}/notes", response_model=list[NoteOut])
def list_meeting_notes(
    meeting_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[NoteOut]:
    meeting = _authorised_meeting(conn, meeting_id, user)
    return [
        NoteOut(**n)
        for n in notes_svc.list_notes(
            conn, thread_id=meeting["thread_id"], meeting_id=meeting_id
        )
    ]


@meeting_router.post("/{meeting_id}/notes", response_model=NoteOut, status_code=201)
async def create_meeting_note(
    meeting_id: int,
    payload: NoteCreateRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> NoteOut:
    """``meeting_id`` in the body is ignored here — the path already said which
    meeting this is being filed on."""
    meeting = _authorised_meeting(conn, meeting_id, user)
    thread = threads_svc.require_thread(conn, meeting["thread_id"])
    return await _create(conn, thread=thread, meeting=meeting, payload=payload, user=user)
