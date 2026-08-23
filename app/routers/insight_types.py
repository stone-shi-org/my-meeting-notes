"""Meeting/insight types -- see app/services/insight_types.py for the model.

Two audiences, two route groups:
  - GET /insight-types            any active user; the recorder's type picker.
  - /settings/insight-types/*     admin-only CRUD; the Settings management page.
The split mirrors settings_api.py's own read-vs-write split (GET /prompts is
active_user, PUT /prompts/{name} is require_admin) -- everyone recording a
meeting needs to see the list, only an admin gets to change what's in it.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.deps import CurrentUser, active_user, get_db, require_admin
from app.logging_config import get_logger
from app.services import insight_types as insight_types_svc

router = APIRouter(prefix="/api", tags=["insight-types"])
log = get_logger("insight_types")


class InsightTypeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=50_000)


class InsightTypeUpdate(BaseModel):
    # None means "leave this alone" -- same convention as ThreadUpdateRequest
    # (see CLAUDE.md's Groups section) -- so a caller editing just the prompt
    # doesn't have to round-trip name it never looked at.
    name: str | None = Field(default=None, min_length=1, max_length=200)
    prompt: str | None = Field(default=None, min_length=1, max_length=50_000)


def _public(row: sqlite3.Row) -> dict:
    return {"slug": row["slug"], "name": row["name"]}


def _full(row: sqlite3.Row) -> dict:
    return {
        "slug": row["slug"],
        "name": row["name"],
        "prompt": row["prompt"],
        "sort_order": row["sort_order"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@router.get("/insight-types")
def list_insight_types(
    _: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    return [_public(row) for row in insight_types_svc.list_types(conn)]


@router.get("/settings/insight-types")
def list_insight_types_admin(
    _: CurrentUser = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    return [_full(row) for row in insight_types_svc.list_types(conn)]


@router.post("/settings/insight-types")
def create_insight_type(
    payload: InsightTypeCreate,
    admin: CurrentUser = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    row = insight_types_svc.create_type(
        conn, name=payload.name, prompt=payload.prompt
    )
    log.info("admin %s created meeting type %s", admin.username, row["slug"])
    return _full(row)


@router.put("/settings/insight-types/{slug}")
def update_insight_type(
    slug: str,
    payload: InsightTypeUpdate,
    admin: CurrentUser = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    row = insight_types_svc.update_type(
        conn, slug, name=payload.name, prompt=payload.prompt
    )
    log.info("admin %s edited meeting type %s", admin.username, slug)
    return _full(row)


@router.delete("/settings/insight-types/{slug}")
def delete_insight_type(
    slug: str,
    admin: CurrentUser = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    insight_types_svc.delete_type(conn, slug)
    log.info("admin %s deleted meeting type %s", admin.username, slug)
    return {"ok": True}
