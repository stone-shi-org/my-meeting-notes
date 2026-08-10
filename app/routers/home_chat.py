"""Ask questions across every thread on the home screen."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import get_settings
from app.deps import CurrentUser, active_user, get_db
from app.logging_config import get_logger
from app.services import home_chat as home_chat_svc
from app.services import llm as llm_svc

router = APIRouter(prefix="/api/home", tags=["home_chat"])
log = get_logger("home_chat")


class HomeChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    # None means "use the configured default"; see llm_svc.resolve_chat_model.
    model: str | None = Field(default=None, max_length=200)


@router.get("/chat")
def list_home_chat_messages(
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    return home_chat_svc.list_messages(conn, user.id)


@router.delete("/chat")
def clear_home_chat_messages(
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    removed = home_chat_svc.clear_messages(conn, user.id)
    return {"ok": True, "removed": removed}


@router.post("/chat")
async def send_home_chat_message(
    payload: HomeChatRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> StreamingResponse:
    """Streams the reply as SSE (`token`/`done`/`error` events), scoped by
    `user.id` alone -- there is no id in the URL to spoof, since this is
    inherently the caller's own data. Same reasoning as `chat.py` for opening
    the service's own short-lived connections rather than holding `conn` open
    across the LLM call.
    """
    model = llm_svc.resolve_chat_model(conn, payload.model)
    return StreamingResponse(
        home_chat_svc.stream_chat_response(
            get_settings().db_path, user.id, payload.message, model=model
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
