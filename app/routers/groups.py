"""Thread groups, and the one route that moves a thread between them.

Two routers, the same reason ``notes.py`` has two: the group routes are about a
group, but "which group is this thread in" is a fact about the *thread*, and the
home screen already holds a thread id when it drops a card. Both live here so
the whole feature is one file.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Query

from app.deps import CurrentUser, active_user, assert_can_access, get_db, owner_scope
from app.logging_config import get_logger
from app.schemas import (
    ThreadGroupAssignRequest,
    ThreadGroupCreateRequest,
    ThreadGroupOut,
    ThreadGroupUpdateRequest,
    ThreadOut,
)
from app.services import groups as groups_svc
from app.services import threads as threads_svc

router = APIRouter(prefix="/api/thread-groups", tags=["groups"])
thread_router = APIRouter(prefix="/api/threads", tags=["groups"])
log = get_logger("groups")


@router.get("", response_model=list[ThreadGroupOut])
def list_groups(
    all: bool = Query(False, description="Admins only: include other users' groups"),
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[ThreadGroupOut]:
    """Every group, including empty ones.

    An empty group has to come back: it is the drop target you just created and
    are about to drag the first thread into.
    """
    scope_sql, scope_params = owner_scope(user, all)
    rows = groups_svc.list_groups(conn, scope_sql=scope_sql, scope_params=scope_params)
    return [ThreadGroupOut(**groups_svc.row_to_group(r)) for r in rows]


@router.post("", response_model=ThreadGroupOut, status_code=201)
def create_group(
    payload: ThreadGroupCreateRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> ThreadGroupOut:
    row = groups_svc.create_group(conn, owner_id=user.id, name=payload.name.strip())
    return ThreadGroupOut(**groups_svc.row_to_group(row))


@router.patch("/{group_id}", response_model=ThreadGroupOut)
def rename_group(
    group_id: int,
    payload: ThreadGroupUpdateRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> ThreadGroupOut:
    assert_can_access(groups_svc.get_group(conn, group_id), user)
    row = groups_svc.rename_group(conn, group_id=group_id, name=payload.name.strip())
    return ThreadGroupOut(**groups_svc.row_to_group(row))


@router.delete("/{group_id}")
def delete_group(
    group_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Delete the folder, keep the threads. They reappear under Ungrouped."""
    assert_can_access(groups_svc.get_group(conn, group_id), user)
    released = groups_svc.delete_group(conn, group_id)
    log.info("user %s deleted group %s (%d thread(s) ungrouped)", user.username, group_id, released)
    return {"ok": True, "ungrouped_threads": released}


@thread_router.put("/{thread_id}/group", response_model=ThreadOut)
def set_thread_group(
    thread_id: int,
    payload: ThreadGroupAssignRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> ThreadOut:
    """Drop a thread into a group, or back out to Ungrouped with ``null``.

    Both ends are checked against the caller: someone else's group is a 404 for
    the same reason someone else's thread is, and without the second check a
    forged id would file your thread into a folder you cannot see.
    """
    thread = threads_svc.get_thread(conn, thread_id)
    assert_can_access(thread, user)

    if payload.group_id is not None:
        assert_can_access(groups_svc.get_group(conn, payload.group_id), user)

    groups_svc.set_thread_group(conn, thread_id=thread_id, group_id=payload.group_id)
    return ThreadOut(**threads_svc.row_to_thread(threads_svc.require_thread(conn, thread_id)))
