"""FastAPI dependencies: database handle, current user, and access scoping."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterator

from fastapi import Depends, Request

from app.config import get_settings
from app.db import connect
from app.errors import (
    AuthRequiredError,
    ForbiddenError,
    NotFoundError,
    PasswordChangeRequiredError,
)
from app.services import users as users_svc


def get_db() -> Iterator[sqlite3.Connection]:
    """One short-lived connection per request."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@dataclass
class CurrentUser:
    id: int
    username: str
    display_name: str | None
    is_admin: bool
    is_active: bool
    must_change_password: bool
    session_id: str

    @property
    def row(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "is_admin": self.is_admin,
            "is_active": self.is_active,
            "must_change_password": self.must_change_password,
        }


def _token_from_request(request: Request) -> str | None:
    """An explicit Authorization header wins; otherwise the session cookie.

    Browsers never send the header, so the SPA always takes the cookie path.
    Header-first means a script can override an ambient cookie deliberately
    rather than silently acting as whoever the browser was logged in as.
    """
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        if token:
            return token

    settings = get_settings()
    return request.cookies.get(settings.session_cookie_name) or None


def current_user(
    request: Request, conn: sqlite3.Connection = Depends(get_db)
) -> CurrentUser:
    token = _token_from_request(request)
    if not token:
        raise AuthRequiredError("Not authenticated")

    resolved = users_svc.resolve_session(conn, token)
    if resolved is None:
        raise AuthRequiredError("Session expired or invalid")

    session, user = resolved
    users_svc.touch_session(conn, session)

    return CurrentUser(
        id=user["id"],
        username=user["username"],
        display_name=user["display_name"],
        is_admin=bool(user["is_admin"]),
        is_active=bool(user["is_active"]),
        must_change_password=bool(user["must_change_password"]),
        session_id=session["id"],
    )


def active_user(user: CurrentUser = Depends(current_user)) -> CurrentUser:
    """A logged-in user who has already dealt with a forced password change.

    Every data route depends on this. The three routes that don't -- /auth/me,
    /auth/change-password, /auth/logout -- deliberately use `current_user`, so a
    forced user can change their password or leave and do nothing else.
    """
    if user.must_change_password:
        raise PasswordChangeRequiredError()
    return user


def require_admin(user: CurrentUser = Depends(active_user)) -> CurrentUser:
    if not user.is_admin:
        raise ForbiddenError("Administrator access required")
    return user


# --------------------------------------------------------------------------- #
# Ownership scoping
# --------------------------------------------------------------------------- #


def owner_scope(user: CurrentUser, all_flag: bool = False) -> tuple[str, list]:
    """SQL fragment restricting a list query to what this user may see.

    A non-admin passing ?all=1 silently gets their own rows rather than a 403 --
    the flag is a view toggle, not a permission boundary, and 403ing it would
    force the SPA to branch on role before every request.
    """
    if user.is_admin and all_flag:
        return "1=1", []
    return "owner_id = ?", [user.id]


def assert_can_access(row, user: CurrentUser) -> None:
    """404 rather than 403 for someone else's object.

    A 403 would confirm the row exists, which leaks the existence of other
    users' threads.
    """
    if row is None:
        raise NotFoundError("Not found")
    owner_id = row["owner_id"] if "owner_id" in row.keys() else None
    if owner_id is None:
        return
    if owner_id != user.id and not user.is_admin:
        raise NotFoundError("Not found")


def paginate(page: int | None, page_size: int | None) -> tuple[int, int, int]:
    """Normalise pagination params into ``(page, page_size, offset)``."""
    settings = get_settings()
    p = max(1, page or 1)
    size = page_size or settings.page_size_default
    size = max(1, min(size, settings.page_size_max))
    return p, size, (p - 1) * size
