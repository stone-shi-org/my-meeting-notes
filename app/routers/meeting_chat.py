"""Ask questions about a single meeting's transcript."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import get_settings
from app.deps import CurrentUser, active_user, assert_can_access, get_db
from app.logging_config import get_logger
from app.services import meeting_chat as meeting_chat_svc
from app.services import threads as threads_svc

router = APIRouter(prefix="/api/meetings", tags=["meeting_chat"])
log = get_logger("meeting_chat")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)


def _authorised_meeting(conn: sqlite3.Connection, meeting_id: int, user: CurrentUser):
    row = threads_svc.get_meeting(conn, meeting_id)
    assert_can_access(row, user)
    return row


@router.get("/{meeting_id}/chat")
def list_chat_messages(
    meeting_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    _authorised_meeting(conn, meeting_id, user)
    return meeting_chat_svc.list_messages(conn, meeting_id)


@router.delete("/{meeting_id}/chat")
def clear_chat_messages(
    meeting_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    _authorised_meeting(conn, meeting_id, user)
    removed = meeting_chat_svc.clear_messages(conn, meeting_id)
    return {"ok": True, "removed": removed}


@router.post("/{meeting_id}/chat")
async def send_chat_message(
    meeting_id: int,
    payload: ChatRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> StreamingResponse:
    """Streams the reply as SSE (`token`/`done`/`error` events), same contract
    as `routers/chat.py`. The ownership check runs synchronously on the
    request-scoped `conn` before the stream starts.
    """
    _authorised_meeting(conn, meeting_id, user)
    return StreamingResponse(
        meeting_chat_svc.stream_chat_response(
            get_settings().db_path, meeting_id, user.id, payload.message
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
