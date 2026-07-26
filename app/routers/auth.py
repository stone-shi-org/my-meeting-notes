"""Login, logout, identity and password change."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request, Response

from app.config import get_settings
from app.deps import CurrentUser, active_user, current_user, get_db
from app.errors import AuthRequiredError, NotFoundError, ValidationError
from app.logging_config import get_logger
from app.schemas import (
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    SessionOut,
    UserOut,
)
from app.security import validate_password, verify_password
from app.services import users as users_svc

router = APIRouter(prefix="/api/auth", tags=["auth"])
log = get_logger("auth")


def _set_session_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        # Lax rather than Strict: a bookmarked deep link must still arrive
        # authenticated. Same-origin SPA + Lax means no CSRF token is needed.
        samesite="lax",
        secure=settings.session_cookie_secure,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(key=settings.session_cookie_name, path="/")


@router.post("/login", response_model=LoginResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    conn: sqlite3.Connection = Depends(get_db),
) -> LoginResponse:
    user = users_svc.authenticate(conn, payload.username, payload.password)
    if user is None:
        log.info("failed login for %r", payload.username)
        raise AuthRequiredError("Invalid username or password")

    token, _ = users_svc.create_session(
        conn,
        user["id"],
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    _set_session_cookie(response, token)

    # Login succeeds even when a password change is pending; the flag tells the
    # SPA to route to /change-password, and every data route 409s until it's done.
    return LoginResponse(
        user=UserOut(**users_svc.row_to_user(user)),
        must_change_password=bool(user["must_change_password"]),
    )


@router.post("/logout")
def logout(
    response: Response,
    user: CurrentUser = Depends(current_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    users_svc.delete_session(conn, user.session_id)
    _clear_session_cookie(response)
    return {"ok": True}


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser = Depends(current_user)) -> UserOut:
    return UserOut(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        is_admin=user.is_admin,
        is_active=user.is_active,
        must_change_password=user.must_change_password,
        created_at="",
        last_login_at=None,
    )


@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest,
    user: CurrentUser = Depends(current_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    settings = get_settings()
    row = users_svc.get_user(conn, user.id)
    if row is None:
        raise NotFoundError("User not found")

    ok = verify_password(
        payload.current_password,
        password_hash=row["password_hash"],
        password_salt=row["password_salt"],
        password_algo=row["password_algo"],
        password_params=row["password_params"],
    )
    if not ok:
        raise ValidationError("Current password is incorrect")

    error = validate_password(
        payload.new_password,
        min_length=settings.password_min_length,
        current=payload.current_password,
    )
    if error:
        raise ValidationError(error)

    users_svc.set_password(conn, user.id, payload.new_password, must_change=False)
    # Changing a password should evict anyone else holding a live session for
    # this account, but not log the current browser out.
    revoked = users_svc.delete_other_sessions(conn, user.id, user.session_id)

    log.info("user %s changed password, revoked %d other session(s)", user.id, revoked)
    return {"ok": True, "revoked_sessions": revoked}


@router.get("/sessions", response_model=list[SessionOut])
def list_sessions(
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[SessionOut]:
    rows = users_svc.list_sessions(conn, user.id)
    return [
        SessionOut(
            id=r["id"],
            created_at=r["created_at"],
            expires_at=r["expires_at"],
            last_seen_at=r["last_seen_at"],
            user_agent=r["user_agent"],
            ip=r["ip"],
            current=r["id"] == user.session_id,
        )
        for r in rows
    ]


@router.delete("/sessions/{session_id}")
def revoke_session(
    session_id: str,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    row = conn.execute(
        "SELECT * FROM sessions WHERE id = ? AND user_id = ?", (session_id, user.id)
    ).fetchone()
    if row is None:
        raise NotFoundError("Session not found")
    users_svc.delete_session(conn, session_id)
    return {"ok": True}
