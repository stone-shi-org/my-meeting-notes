"""User administration. Every route here is admin-only."""

from __future__ import annotations

import secrets
import sqlite3

from fastapi import APIRouter, Depends, Query

from app.db import utcnow
from app.deps import CurrentUser, get_db, paginate, require_admin
from app.errors import ConflictError, NotFoundError, ValidationError
from app.logging_config import get_logger
from app.schemas import (
    Page,
    ResetPasswordRequest,
    ResetPasswordResponse,
    SetUserMcpProfileRequest,
    UserCreateRequest,
    UserMcpProfileOut,
    UserOut,
    UserUpdateRequest,
)
from app.security import validate_password
from app.services import mcpclient as mcp_svc
from app.services import users as users_svc
from app.config import get_settings

router = APIRouter(prefix="/api/users", tags=["users"])
log = get_logger("users_api")


@router.get("", response_model=Page[UserOut])
def list_users(
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1),
    include_inactive: bool = Query(True),
    _: CurrentUser = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
) -> Page[UserOut]:
    p, size, offset = paginate(page, page_size)
    where = "" if include_inactive else "WHERE is_active = 1"

    total = conn.execute(f"SELECT COUNT(*) FROM users {where}").fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM users {where} ORDER BY id LIMIT ? OFFSET ?", (size, offset)
    ).fetchall()

    return Page[UserOut](
        items=[UserOut(**users_svc.row_to_user(r)) for r in rows],
        page=p,
        page_size=size,
        total=total,
        total_pages=max(1, -(-total // size)),
    )


@router.post("", response_model=UserOut, status_code=201)
def create_user(
    payload: UserCreateRequest,
    admin: CurrentUser = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
) -> UserOut:
    row = users_svc.create_user(
        conn,
        username=payload.username,
        password=payload.password,
        display_name=payload.display_name,
        is_admin=payload.is_admin,
        must_change_password=True,
    )
    log.info("admin %s created user %s", admin.username, payload.username)
    return UserOut(**users_svc.row_to_user(row))


@router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    payload: UserUpdateRequest,
    admin: CurrentUser = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
) -> UserOut:
    row = users_svc.require_user(conn, user_id)

    updates: dict = {}
    if payload.display_name is not None:
        updates["display_name"] = payload.display_name
    if payload.is_admin is not None:
        updates["is_admin"] = int(payload.is_admin)
    if payload.is_active is not None:
        updates["is_active"] = int(payload.is_active)

    # Losing the last admin would lock everyone out of user management with no
    # way back in short of editing the database by hand.
    losing_admin = (
        payload.is_admin is False and row["is_admin"]
    ) or (payload.is_active is False and row["is_admin"] and row["is_active"])
    if losing_admin and users_svc.count_active_admins(conn, exclude_id=user_id) == 0:
        raise ConflictError("Cannot remove the last active administrator")

    if updates:
        updates["updated_at"] = utcnow()
        assignments = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE users SET {assignments} WHERE id = ?",
            [*updates.values(), user_id],
        )

    # A deactivated user should not keep browsing on an existing cookie.
    if payload.is_active is False:
        users_svc.delete_other_sessions(conn, user_id, keep_session_id=None)

    log.info("admin %s updated user %s: %s", admin.username, user_id, sorted(updates))
    return UserOut(**users_svc.row_to_user(users_svc.require_user(conn, user_id)))


@router.post("/{user_id}/reset-password", response_model=ResetPasswordResponse)
def reset_password(
    user_id: int,
    payload: ResetPasswordRequest,
    admin: CurrentUser = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
) -> ResetPasswordResponse:
    users_svc.require_user(conn, user_id)
    settings = get_settings()

    generated = None
    password = payload.new_password
    if not password:
        password = secrets.token_urlsafe(12)
        generated = password

    error = validate_password(password, min_length=settings.password_min_length)
    if error:
        raise ValidationError(error)

    users_svc.set_password(conn, user_id, password, must_change=True)
    revoked = users_svc.delete_other_sessions(conn, user_id, keep_session_id=None)

    log.info(
        "admin %s reset password for user %s, revoked %d session(s)",
        admin.username,
        user_id,
        revoked,
    )
    return ResetPasswordResponse(
        user=UserOut(**users_svc.row_to_user(users_svc.require_user(conn, user_id))),
        # Returned exactly once, at generation time. Never retrievable later.
        temporary_password=generated,
    )


@router.delete("/{user_id}")
def delete_user(
    user_id: int,
    admin: CurrentUser = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    row = users_svc.require_user(conn, user_id)

    if user_id == admin.id:
        raise ConflictError("You cannot delete your own account")

    if row["is_admin"] and users_svc.count_active_admins(conn, exclude_id=user_id) == 0:
        raise ConflictError("Cannot remove the last active administrator")

    owned = conn.execute(
        "SELECT COUNT(*) FROM threads WHERE owner_id = ?", (user_id,)
    ).fetchone()[0]

    # Soft delete: threads reference owner_id, and hard-deleting would either
    # orphan them or silently destroy someone's recordings.
    conn.execute(
        "UPDATE users SET is_active = 0, updated_at = ? WHERE id = ?",
        (utcnow(), user_id),
    )
    users_svc.delete_other_sessions(conn, user_id, keep_session_id=None)

    log.info("admin %s deactivated user %s (%d owned threads)", admin.username, user_id, owned)
    return {"ok": True, "deactivated": True, "owned_threads": owned}


# --------------------------------------------------------------------------- #
# MCP profiles on behalf of a user
#
# Mirrors /api/me/mcp-profiles but for someone else -- for onboarding a user
# who doesn't yet have their own token from whoever administers the
# calendar/email MCP servers.
# --------------------------------------------------------------------------- #


@router.get("/{user_id}/mcp-profiles", response_model=list[UserMcpProfileOut])
def list_user_mcp_profiles(
    user_id: int,
    _: CurrentUser = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[UserMcpProfileOut]:
    users_svc.require_user(conn, user_id)
    servers = conn.execute("SELECT name FROM mcp_servers ORDER BY name").fetchall()
    return [
        UserMcpProfileOut(**mcp_svc.describe_user_profile(conn, user_id, row["name"]))
        for row in servers
    ]


@router.put("/{user_id}/mcp-profiles/{server_name}", response_model=UserMcpProfileOut)
def set_user_mcp_profile(
    user_id: int,
    server_name: str,
    payload: SetUserMcpProfileRequest,
    admin: CurrentUser = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
) -> UserMcpProfileOut:
    target = users_svc.require_user(conn, user_id)
    auth_token, clear_token = mcp_svc.resolve_token_update(payload.auth_token)
    mcp_svc.set_user_override(
        conn,
        user_id,
        server_name,
        profile=payload.profile,
        auth_token=auth_token,
        clear_token=clear_token,
    )
    log.info(
        "admin %s set %s's %s profile to %r",
        admin.username, target["username"], server_name, payload.profile,
    )
    return UserMcpProfileOut(**mcp_svc.describe_user_profile(conn, user_id, server_name))


@router.delete("/{user_id}/mcp-profiles/{server_name}")
def clear_user_mcp_profile(
    user_id: int,
    server_name: str,
    admin: CurrentUser = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    target = users_svc.require_user(conn, user_id)
    removed = mcp_svc.delete_user_override(conn, user_id, server_name)
    log.info(
        "admin %s cleared %s's %s profile override", admin.username, target["username"], server_name
    )
    return {"ok": True, "removed": removed}
