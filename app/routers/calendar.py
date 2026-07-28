"""Upcoming calendar events, and creating a meeting from one."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.db import get_conn
from app.deps import CurrentUser, active_user, get_db
from app.errors import ConflictError
from app.logging_config import get_logger
from app.routers.meetings import resolve_thread
from app.schemas import MeetingOut
from app.services import matching as matching_svc
from app.services import threads as threads_svc
from app.services import upcoming as upcoming_svc

router = APIRouter(prefix="/api/calendar", tags=["calendar"])
log = get_logger("calendar_api")


class UpcomingEventIn(BaseModel):
    """One event, handed straight back from ``GET /upcoming``.

    The client echoes the event rather than sending a uid for the server to look
    up again: there is nothing to look up. The listing is not persisted, and no
    provider offers fetch-by-uid across every calendar. Accepting it is safe
    because integrations are per-user and this is the caller's own calendar --
    the worst a forged payload achieves is a meeting on the caller's own thread
    with a title they could have typed anyway. Lengths are bounded so it cannot
    become a way to write megabytes into ``raw_json``.
    """

    uid: str = Field(min_length=1, max_length=512)
    summary: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=20_000)
    location: str | None = Field(default=None, max_length=500)
    start: str | None = Field(default=None, max_length=64)
    end: str | None = Field(default=None, max_length=64)
    attendees: list[str] = Field(default_factory=list, max_length=100)
    calendar_name: str | None = Field(default=None, max_length=200)
    account: str | None = Field(default=None, max_length=200)
    type: str | None = Field(default=None, max_length=64)
    url: str | None = Field(default=None, max_length=2000)
    source_uid: str | None = Field(default=None, max_length=512)
    provider: str | None = Field(default=None, max_length=64)
    integration_id: int | None = None


class CreateFromEventRequest(BaseModel):
    event: UpcomingEventIn
    # All three default to the event's own values, so the endpoint is usable
    # without the dialog having filled anything in.
    title: str | None = Field(default=None, max_length=500)
    meeting_at: str | None = None
    speaker_names: list[str] = Field(default_factory=list, max_length=100)
    notes: str | None = None
    thread_id: int | None = None
    new_thread_title: str | None = Field(default=None, max_length=500)
    new_thread_description: str | None = None


@router.get("/upcoming")
async def list_upcoming(
    days: int = Query(upcoming_svc.DEFAULT_DAYS, ge=1, le=upcoming_svc.MAX_DAYS),
    user: CurrentUser = Depends(active_user),
) -> dict:
    """What is on this user's calendars between this morning and ``days`` ahead.

    Takes its own short-lived connections rather than the request one: the
    provider calls in the middle are network round trips, and the request
    connection is inside an open transaction for the whole handler.
    """
    return await upcoming_svc.collect(get_conn, user_id=user.id, days=days)


@router.post("/upcoming/meeting", status_code=201)
def create_meeting_from_event(
    payload: CreateFromEventRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Create a meeting from a calendar event and attach the event to it.

    One request, not "create then confirm": the event *is* the reason the
    meeting exists, so a meeting that got created while the attach failed would
    be a worse outcome than neither happening. Both writes share the request
    transaction, which ``get_db`` rolls back as a unit.
    """
    event = payload.event.model_dump()

    already = upcoming_svc.attached_by_uid(conn, user.id).get(event["uid"])
    if already:
        # A stale home screen, most likely -- the listing marks attached events.
        raise ConflictError(
            f"That event is already attached to “{already['meeting_title'] or 'a meeting'}”."
        )

    thread_id = resolve_thread(
        conn,
        user,
        thread_id=payload.thread_id,
        # Falling back to the event title keeps the common path -- a one-off
        # meeting that is its own thread -- down to a single click.
        new_thread_title=payload.new_thread_title
        or (None if payload.thread_id else (payload.title or event["summary"] or "Meeting")),
        new_thread_description=payload.new_thread_description,
    )

    meeting = threads_svc.create_meeting(
        conn,
        thread_id=thread_id,
        owner_id=user.id,
        title=payload.title or event["summary"] or "Untitled meeting",
        meeting_at=payload.meeting_at or event["start"],
        notes=payload.notes,
    )

    speakers = payload.speaker_names or event["attendees"]
    seeded = threads_svc.seed_speaker_names(conn, meeting["id"], speakers)

    matching_svc.attach_event(
        conn,
        thread_id=thread_id,
        meeting_id=meeting["id"],
        event=event,
        user_id=user.id,
    )
    threads_svc.touch_thread(conn, thread_id)

    log.info(
        "user %s created meeting %s from calendar event %s (%d speaker hint(s))",
        user.username, meeting["id"], event["uid"], seeded,
    )
    return {
        # Re-read: the row from create_meeting predates the speaker hints, so its
        # derived speaker_count would come back 0 for a meeting that has them.
        "meeting": MeetingOut(
            **threads_svc.row_to_meeting(threads_svc.require_meeting(conn, meeting["id"]))
        ).model_dump(),
        "thread_id": thread_id,
        "speaker_hints": seeded,
    }
