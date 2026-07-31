"""Ask questions about a thread's meetings, calendar events and emails."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import get_settings
from app.deps import CurrentUser, active_user, assert_can_access, get_db
from app.logging_config import get_logger
from app.services import chat as chat_svc
from app.services import llm as llm_svc
from app.services import threads as threads_svc

router = APIRouter(prefix="/api/threads", tags=["chat"])
log = get_logger("chat")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    # None means "use the configured default"; see llm_svc.resolve_chat_model.
    model: str | None = Field(default=None, max_length=200)


def _authorised_thread(conn: sqlite3.Connection, thread_id: int, user: CurrentUser):
    row = threads_svc.get_thread(conn, thread_id)
    assert_can_access(row, user)
    return row


@router.get("/{thread_id}/chat")
def list_chat_messages(
    thread_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    _authorised_thread(conn, thread_id, user)
    return chat_svc.list_messages(conn, thread_id)


@router.delete("/{thread_id}/chat")
def clear_chat_messages(
    thread_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    _authorised_thread(conn, thread_id, user)
    removed = chat_svc.clear_messages(conn, thread_id)
    return {"ok": True, "removed": removed}


@router.post("/{thread_id}/chat")
async def send_chat_message(
    thread_id: int,
    payload: ChatRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> StreamingResponse:
    """Streams the reply as SSE (`token`/`done`/`error` events). The ownership
    check runs synchronously on the request-scoped ``conn`` before the stream
    starts; the service then opens its own short-lived connections for the
    duration of the (potentially long) LLM call, since ``conn`` must not stay
    open across it.
    """
    _authorised_thread(conn, thread_id, user)
    model = llm_svc.resolve_chat_model(conn, payload.model)
    return StreamingResponse(
        chat_svc.stream_chat_response(
            get_settings().db_path, thread_id, user.id, payload.message, model=model
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
