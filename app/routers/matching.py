"""Search for related emails and calendar events, then attach the user's picks."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.deps import CurrentUser, active_user, assert_can_access, get_db
from app.errors import NotFoundError
from app.jobs import queue as queue_mod
from app.logging_config import get_logger
from app.services import matching as matching_svc
from app.services import threads as threads_svc

router = APIRouter(prefix="/api/meetings", tags=["matching"])
log = get_logger("matching_api")


class MatchRequest(BaseModel):
    window_days_before: int | None = Field(default=None, ge=0, le=365)
    window_days_after: int | None = Field(default=None, ge=0, le=365)
    extra_keywords: list[str] = Field(default_factory=list)
    model: str | None = None


class ConfirmRequest(BaseModel):
    event_uids: list[str] = Field(default_factory=list)
    email_message_ids: list[str] = Field(default_factory=list)
    append_event_title_to_meeting_title: bool = True


def _authorised_meeting(conn, meeting_id: int, user: CurrentUser):
    row = threads_svc.get_meeting(conn, meeting_id)
    assert_can_access(row, user)
    return row


@router.post("/{meeting_id}/match", status_code=202)
async def start_match(
    meeting_id: int,
    payload: MatchRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    row = _authorised_meeting(conn, meeting_id, user)

    job_id = queue_mod.create_job(
        conn,
        job_type="match",
        user_id=user.id,
        meeting_id=meeting_id,
        thread_id=row["thread_id"],
        payload={
            "meeting_id": meeting_id,
            "user_id": user.id,
            "window_days_before": payload.window_days_before,
            "window_days_after": payload.window_days_after,
            "extra_keywords": payload.extra_keywords,
            "model": payload.model,
        },
    )
    conn.commit()
    await queue_mod.get_queue().enqueue(job_id)
    return {"job_id": job_id}


@router.get("/{meeting_id}/match/latest")
def latest_match(
    meeting_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    _authorised_meeting(conn, meeting_id, user)
    run = matching_svc.latest_match_run(conn, meeting_id)
    if run is None:
        raise NotFoundError("No match has been run for this meeting yet")
    return run


@router.post("/{meeting_id}/match/confirm")
def confirm_match(
    meeting_id: int,
    payload: ConfirmRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Attach what the user ticked.

    Emails land on the thread rather than the meeting: they are context for the
    whole run of work, not for one recording.
    """
    _authorised_meeting(conn, meeting_id, user)

    result = matching_svc.attach_selected(
        conn,
        meeting_id=meeting_id,
        user_id=user.id,
        event_uids=payload.event_uids,
        email_message_ids=payload.email_message_ids,
        append_event_title=payload.append_event_title_to_meeting_title,
    )

    log.info(
        "user %s attached %d event(s) and %d email(s) to meeting %s",
        user.username, result["attached_events"], result["attached_emails"], meeting_id,
    )
    return result
