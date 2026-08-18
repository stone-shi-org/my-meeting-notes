"""Live insights over an in-progress recording -- see services/insights.py.

A plain POST, not a websocket like live_caption.py: this only needs a
request/response every insights_interval_sec, driven by a setInterval on the
client, not a standing connection. FastAPI runs a sync ``def`` route in its
threadpool, so the blocking LLM call in insights_svc.analyze does not block
the event loop -- same as settings_api.py's /llm/test.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.deps import CurrentUser, active_user, get_db
from app.services import insights as insights_svc

router = APIRouter(prefix="/api/insights", tags=["insights"])


class InsightsAnalyzeRequest(BaseModel):
    # An insight_types.slug -- no longer a fixed Literal now that the list is
    # admin-extensible (see app/services/insight_types.py). An unknown slug
    # 404s out of insights_svc.analyze rather than failing validation here.
    meeting_type: str = Field(min_length=1, max_length=100)
    # Trimmed further server-side (see insights_svc.MAX_TRANSCRIPT_CHARS);
    # bounded here mainly so a runaway client can't post an arbitrarily large
    # body.
    transcript: str = Field(min_length=1, max_length=200_000)
    # Opaque: whatever analyze() returned last call, echoed back so the model
    # can grow it rather than starting over. None on the first call.
    previous: dict | None = None


@router.post("/analyze")
def analyze_insights(
    payload: InsightsAnalyzeRequest,
    _: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    return insights_svc.analyze(
        conn, payload.meeting_type, payload.transcript, payload.previous
    )
