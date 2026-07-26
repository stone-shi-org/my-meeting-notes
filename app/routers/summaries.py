"""Summaries, versions, regeneration and action items."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.db import utcnow
from app.deps import CurrentUser, active_user, assert_can_access, get_db
from app.errors import NotFoundError, ValidationError
from app.jobs import queue as queue_mod
from app.logging_config import get_logger
from app.services import summarize as summarize_svc
from app.services import threads as threads_svc

router = APIRouter(prefix="/api", tags=["summaries"])
log = get_logger("summaries")


class RegenerateRequest(BaseModel):
    model: str | None = None
    prompt_name: str | None = None
    prompt_override: str | None = Field(default=None, max_length=100_000)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)


class ActionItemUpdate(BaseModel):
    text: str | None = Field(default=None, max_length=2000)
    owner_label: str | None = Field(default=None, max_length=200)
    due_date: str | None = Field(default=None, max_length=32)
    priority: str | None = Field(default=None, pattern="^(high|medium|low)$")
    status: str | None = Field(default=None, pattern="^(open|done|dropped)$")


def _authorised_meeting(conn, meeting_id: int, user: CurrentUser):
    row = threads_svc.get_meeting(conn, meeting_id)
    assert_can_access(row, user)
    return row


@router.get("/meetings/{meeting_id}/summary")
def get_summary(
    meeting_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    _authorised_meeting(conn, meeting_id, user)
    return summarize_svc.get_current_summary(conn, meeting_id)


@router.get("/meetings/{meeting_id}/summaries")
def list_summaries(
    meeting_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    """Version history: model and prompt provenance, without the bodies."""
    _authorised_meeting(conn, meeting_id, user)
    rows = conn.execute(
        "SELECT * FROM summaries WHERE meeting_id = ? ORDER BY version DESC",
        (meeting_id,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "version": r["version"],
            "is_current": bool(r["is_current"]),
            "status": r["status"],
            "model": r["model"],
            "prompt_name": r["prompt_name"],
            "prompt_version": r["prompt_version"],
            "prompt_sha256": r["prompt_sha256"],
            "tldr": r["tldr"],
            "created_at": r["created_at"],
            "duration_sec": r["duration_sec"],
        }
        for r in rows
    ]


@router.get("/meetings/{meeting_id}/summaries/{version}")
def get_summary_version(
    meeting_id: int,
    version: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    _authorised_meeting(conn, meeting_id, user)
    row = conn.execute(
        "SELECT * FROM summaries WHERE meeting_id = ? AND version = ?",
        (meeting_id, version),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"Summary version {version} not found")

    items = conn.execute(
        "SELECT * FROM action_items WHERE summary_id = ? ORDER BY idx", (row["id"],)
    ).fetchall()
    summary = summarize_svc.row_to_summary(row, items)
    # The exact prompt this version ran with, for tuning comparisons.
    summary["prompt_text"] = row["prompt_text"]
    return summary


@router.post("/meetings/{meeting_id}/summaries/{version}/activate")
def activate_version(
    meeting_id: int,
    version: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    _authorised_meeting(conn, meeting_id, user)
    row = conn.execute(
        "SELECT * FROM summaries WHERE meeting_id = ? AND version = ?",
        (meeting_id, version),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"Summary version {version} not found")

    conn.execute("UPDATE summaries SET is_current = 0 WHERE meeting_id = ?", (meeting_id,))
    conn.execute("UPDATE summaries SET is_current = 1 WHERE id = ?", (row["id"],))
    conn.execute(
        "UPDATE meetings SET active_summary_id = ?, updated_at = ? WHERE id = ?",
        (row["id"], utcnow(), meeting_id),
    )
    return {"ok": True, "version": version}


@router.post("/meetings/{meeting_id}/summary/regenerate", status_code=202)
async def regenerate_summary(
    meeting_id: int,
    payload: RegenerateRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Re-run with the current prompt file, or a one-off override.

    The whole point of the prompt living in a file: edit, regenerate, compare
    versions, keep the one that reads best.
    """
    row = _authorised_meeting(conn, meeting_id, user)
    if row["active_diarization_id"] is None:
        raise ValidationError("This meeting has no transcript to summarize")

    job_id = queue_mod.create_job(
        conn,
        job_type="summarize",
        user_id=user.id,
        meeting_id=meeting_id,
        thread_id=row["thread_id"],
        payload={
            "meeting_id": meeting_id,
            "model": payload.model,
            "prompt_name": payload.prompt_name,
            "prompt_override": payload.prompt_override,
            "temperature": payload.temperature,
            "user_id": user.id,
        },
    )
    conn.commit()
    await queue_mod.get_queue().enqueue(job_id)
    return {"job_id": job_id}


# --------------------------------------------------------------------------- #
# Action items
# --------------------------------------------------------------------------- #


@router.get("/meetings/{meeting_id}/action-items")
def list_action_items(
    meeting_id: int,
    all_versions: bool = Query(False),
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    _authorised_meeting(conn, meeting_id, user)

    if all_versions:
        rows = conn.execute(
            "SELECT * FROM action_items WHERE meeting_id = ? ORDER BY summary_id, idx",
            (meeting_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT a.* FROM action_items a
              JOIN summaries s ON s.id = a.summary_id
             WHERE a.meeting_id = ? AND s.is_current = 1
             ORDER BY a.idx
            """,
            (meeting_id,),
        ).fetchall()

    return [summarize_svc.row_to_action_item(r) for r in rows]


@router.patch("/action-items/{item_id}")
def update_action_item(
    item_id: int,
    payload: ActionItemUpdate,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    row = conn.execute("SELECT * FROM action_items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise NotFoundError("Action item not found")
    _authorised_meeting(conn, row["meeting_id"], user)

    updates: dict = {}
    for field in ("text", "owner_label", "due_date", "priority", "status"):
        value = getattr(payload, field)
        if value is not None:
            updates[field] = value

    if "status" in updates:
        updates["done_at"] = utcnow() if updates["status"] == "done" else None

    if updates:
        assignments = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE action_items SET {assignments} WHERE id = ?",
            [*updates.values(), item_id],
        )

    updated = conn.execute(
        "SELECT * FROM action_items WHERE id = ?", (item_id,)
    ).fetchone()
    return summarize_svc.row_to_action_item(updated)
