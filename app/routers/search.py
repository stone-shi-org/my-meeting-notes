"""Search everything a user owns (MMN-15). See ``services/search.py``.

``GET /api/search`` is a plain ``def`` on purpose: the semantic arm makes one
blocking embedding call for the query, and FastAPI runs sync routes in its
threadpool, so that call never blocks the event loop the progress polls use.

The scope is always the caller's own documents -- admins included. ``?all=1``
is a view toggle elsewhere in the app; here it would mean "search every
user's transcripts", which nothing asked for.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, Query

from app.deps import CurrentUser, active_user, get_db, require_admin
from app.errors import NotFoundError
from app.jobs.search_indexer import nudge_indexer
from app.logging_config import get_logger
from app.services import search as search_svc
from app.services import search_index

router = APIRouter(prefix="/api/search", tags=["search"])
log = get_logger("search")


@router.get("")
def search(
    q: str = Query(..., min_length=1, max_length=500),
    kinds: str | None = Query(None, description="Comma-separated subset of the indexed kinds"),
    since: str | None = Query(None, description="ISO date/datetime, inclusive"),
    until: str | None = Query(None, description="ISO date/datetime; a bare date is inclusive"),
    thread_id: int | None = Query(None),
    mode: Literal["hybrid", "keyword", "semantic"] = Query("hybrid"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=10_000),
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    if thread_id is not None:
        row = conn.execute("SELECT owner_id FROM threads WHERE id = ?", (thread_id,)).fetchone()
        # 404 rather than 403 for someone else's thread, per the conventions.
        if row is None or row["owner_id"] != user.id:
            raise NotFoundError("Thread not found")
    return search_svc.search(
        conn,
        owner_id=user.id,
        q=q,
        kinds=kinds,
        since=since,
        until=until,
        thread_id=thread_id,
        mode=mode,
        limit=limit,
        offset=offset,
    )


@router.get("/status")
def search_status(
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    return search_svc.status(conn, owner_id=user.id, is_admin=user.is_admin)


@router.post("/rebuild")
def rebuild_index(
    user: CurrentUser = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Drop the keyword index and queue every scope. Returns at once; the
    background indexer does the work and Settings -> Search polls status."""
    queued = search_index.rebuild(conn)
    log.info("user %s rebuilt the search index (%d scopes queued)", user.username, queued)
    nudge_indexer()
    return {"ok": True, "queued_scopes": queued}
