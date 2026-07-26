"""Job status, the progress-polling endpoint, and the SSE upgrade."""

from __future__ import annotations

import asyncio
import json
import sqlite3

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from app.db import get_conn, utcnow
from app.deps import CurrentUser, active_user, get_db, owner_scope, paginate
from app.errors import ConflictError, NotFoundError
from app.jobs import queue as queue_mod
from app.logging_config import get_logger

router = APIRouter(prefix="/api/jobs", tags=["jobs"])
log = get_logger("jobs_api")

POLL_INTERVAL_SEC = 1.0
KEEPALIVE_SEC = 15.0


def _require_job(conn: sqlite3.Connection, job_id: str, user: CurrentUser) -> sqlite3.Row:
    row = queue_mod.get_job(conn, job_id)
    # 404 rather than 403 for someone else's job: a 403 would confirm it exists.
    if row is None or (row["user_id"] != user.id and not user.is_admin):
        raise NotFoundError("Job not found")
    return row


@router.get("")
def list_jobs(
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1),
    status: str | None = Query(None),
    type: str | None = Query(None),
    meeting_id: int | None = Query(None),
    thread_id: int | None = Query(None),
    all: bool = Query(False),
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    p, size, offset = paginate(page, page_size)

    where = []
    params: list = []
    scope_sql, scope_params = owner_scope(user, all)
    where.append(scope_sql.replace("owner_id", "user_id"))
    params.extend(scope_params)

    if status == "active":
        where.append("status IN ('queued', 'running')")
    elif status:
        where.append("status = ?")
        params.append(status)
    if type:
        where.append("type = ?")
        params.append(type)
    if meeting_id is not None:
        where.append("meeting_id = ?")
        params.append(meeting_id)
    if thread_id is not None:
        where.append("thread_id = ?")
        params.append(thread_id)

    where_sql = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {where_sql}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM jobs WHERE {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        [*params, size, offset],
    ).fetchall()

    return {
        "items": [queue_mod.row_to_job(r) for r in rows],
        "page": p,
        "page_size": size,
        "total": total,
        "total_pages": max(1, -(-total // size)),
    }


@router.get("/{job_id}")
def get_job(
    job_id: str,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    return queue_mod.row_to_job(_require_job(conn, job_id, user))


@router.get("/{job_id}/events")
def job_events(
    job_id: str,
    after_id: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """The primary progress channel.

    Plain polling with a monotonic cursor: works through every proxy, is
    trivially testable, and survives a reload. SSE is an optional upgrade.
    """
    row = _require_job(conn, job_id, user)

    events = conn.execute(
        "SELECT * FROM job_events WHERE job_id = ? AND id > ? ORDER BY id LIMIT ?",
        (job_id, after_id, limit),
    ).fetchall()

    return {
        "job": queue_mod.row_to_job(row),
        "events": [
            {
                "id": e["id"],
                "ts": e["ts"],
                "stage": e["stage"],
                "level": e["level"],
                "message": e["message"],
                "progress": e["progress"],
            }
            for e in events
        ],
        "next_after_id": events[-1]["id"] if events else after_id,
    }


@router.get("/{job_id}/stream")
async def job_stream(
    job_id: str,
    request: Request,
    after_id: int = Query(0, ge=0),
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> StreamingResponse:
    """SSE progress. Falls back to polling client-side if a proxy eats it."""
    _require_job(conn, job_id, user)

    async def generate():
        cursor = after_id
        idle = 0.0
        while True:
            if await request.is_disconnected():
                return

            with get_conn() as c:
                row = queue_mod.get_job(c, job_id)
                if row is None:
                    yield "event: error\ndata: {\"message\": \"job vanished\"}\n\n"
                    return
                events = c.execute(
                    "SELECT * FROM job_events WHERE job_id = ? AND id > ? ORDER BY id",
                    (job_id, cursor),
                ).fetchall()

            for e in events:
                cursor = e["id"]
                payload = json.dumps(
                    {
                        "id": e["id"],
                        "ts": e["ts"],
                        "stage": e["stage"],
                        "level": e["level"],
                        "message": e["message"],
                        "progress": e["progress"],
                    }
                )
                yield f"event: progress\ndata: {payload}\n\n"
                idle = 0.0

            job = queue_mod.row_to_job(row)
            if job["status"] in {"succeeded", "failed", "cancelled"}:
                yield f"event: done\ndata: {json.dumps(job)}\n\n"
                return

            await asyncio.sleep(POLL_INTERVAL_SEC)
            idle += POLL_INTERVAL_SEC
            if idle >= KEEPALIVE_SEC:
                # Comment frame: keeps intermediaries from timing out the
                # connection during a long silent diarization.
                yield ": keepalive\n\n"
                idle = 0.0

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{job_id}/cancel")
def cancel_job(
    job_id: str,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    row = _require_job(conn, job_id, user)
    if row["status"] in {"succeeded", "failed", "cancelled"}:
        raise ConflictError(f"Job is already {row['status']}")

    # Cooperative: the worker notices at its next stage boundary.
    conn.execute(
        "UPDATE jobs SET cancel_requested = 1, updated_at = ? WHERE id = ?",
        (utcnow(), job_id),
    )
    conn.execute(
        "INSERT INTO job_events (job_id, ts, level, message) "
        "VALUES (?, ?, 'warn', 'Cancellation requested')",
        (job_id, utcnow()),
    )
    return {"ok": True, "cancel_requested": True}


@router.post("/{job_id}/retry")
async def retry_job(
    job_id: str,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    row = _require_job(conn, job_id, user)
    if row["status"] not in {"failed", "interrupted", "cancelled"}:
        raise ConflictError(f"Cannot retry a job that is {row['status']}")

    conn.execute(
        """
        UPDATE jobs SET status = 'queued', cancel_requested = 0, error = NULL,
                        error_stage = NULL, error_trace = NULL,
                        max_attempts = MAX(max_attempts, attempts + 1),
                        finished_at = NULL, updated_at = ?
         WHERE id = ?
        """,
        (utcnow(), job_id),
    )
    conn.execute(
        "INSERT INTO job_events (job_id, ts, level, message) "
        "VALUES (?, ?, 'info', 'Retry requested')",
        (job_id, utcnow()),
    )
    conn.commit()

    await queue_mod.get_queue().enqueue(job_id)
    return {"ok": True, "job_id": job_id}
