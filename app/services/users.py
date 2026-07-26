"""User and session persistence.

Every function takes an open connection so callers control the transaction
boundary; nothing here opens or commits on its own.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.db import utcnow
from app.errors import ConflictError, NotFoundError, ValidationError
from app.logging_config import get_logger
from app.security import (
    hash_password,
    hash_token,
    needs_rehash,
    new_session_token,
    validate_password,
    verify_password,
)

log = get_logger("users")

# Extending the session on every request would mean a write per request. Once
# per this interval is plenty for a sliding window measured in weeks.
SLIDING_REFRESH_SECONDS = 600


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def row_to_user(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "is_admin": bool(row["is_admin"]),
        "is_active": bool(row["is_active"]),
        "must_change_password": bool(row["must_change_password"]),
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
    }


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #


def get_user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_username(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
    ).fetchone()


def count_users(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def count_active_admins(conn: sqlite3.Connection, exclude_id: int | None = None) -> int:
    sql = "SELECT COUNT(*) FROM users WHERE is_admin = 1 AND is_active = 1"
    params: list = []
    if exclude_id is not None:
        sql += " AND id != ?"
        params.append(exclude_id)
    return conn.execute(sql, params).fetchone()[0]


def create_user(
    conn: sqlite3.Connection,
    *,
    username: str,
    password: str,
    display_name: str | None = None,
    is_admin: bool = False,
    must_change_password: bool = True,
) -> sqlite3.Row:
    settings = get_settings()
    error = validate_password(password, min_length=settings.password_min_length)
    if error:
        raise ValidationError(error)

    if get_user_by_username(conn, username) is not None:
        raise ConflictError(f"Username {username!r} is already taken")

    now = utcnow()
    creds = hash_password(password)
    cur = conn.execute(
        """
        INSERT INTO users (username, display_name, password_hash, password_salt,
                           password_algo, password_params, is_admin, is_active,
                           must_change_password, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
        """,
        (
            username,
            display_name,
            creds["password_hash"],
            creds["password_salt"],
            creds["password_algo"],
            creds["password_params"],
            int(is_admin),
            int(must_change_password),
            now,
            now,
        ),
    )
    return get_user(conn, cur.lastrowid)  # type: ignore[arg-type]


def set_password(
    conn: sqlite3.Connection,
    user_id: int,
    password: str,
    *,
    must_change: bool = False,
) -> None:
    creds = hash_password(password)
    conn.execute(
        """
        UPDATE users
           SET password_hash = ?, password_salt = ?, password_algo = ?,
               password_params = ?, must_change_password = ?, updated_at = ?
         WHERE id = ?
        """,
        (
            creds["password_hash"],
            creds["password_salt"],
            creds["password_algo"],
            creds["password_params"],
            int(must_change),
            utcnow(),
            user_id,
        ),
    )


def authenticate(
    conn: sqlite3.Connection, username: str, password: str
) -> sqlite3.Row | None:
    """Return the user row on success, else None. Inactive users never succeed."""
    row = get_user_by_username(conn, username)
    if row is None:
        # Spend comparable time on an unknown username so the response time
        # doesn't reveal which accounts exist.
        verify_password(
            password,
            password_hash="0" * 64,
            password_salt="00" * 16,
        )
        return None

    if not row["is_active"]:
        return None

    ok = verify_password(
        password,
        password_hash=row["password_hash"],
        password_salt=row["password_salt"],
        password_algo=row["password_algo"],
        password_params=row["password_params"],
    )
    if not ok:
        return None

    if needs_rehash(row["password_params"], row["password_algo"]):
        log.info("upgrading password hash parameters for user %s", row["id"])
        set_password(
            conn, row["id"], password, must_change=bool(row["must_change_password"])
        )
        row = get_user(conn, row["id"])  # type: ignore[assignment]

    conn.execute(
        "UPDATE users SET last_login_at = ? WHERE id = ?", (utcnow(), row["id"])
    )
    return row


def seed_admin(conn: sqlite3.Connection) -> bool:
    """Create the bootstrap admin when the user table is empty.

    Idempotent: an existing user table is never touched, so changing
    MMN_BOOTSTRAP_ADMIN_PASSWORD later does nothing to a live install.
    """
    if count_users(conn) > 0:
        return False

    settings = get_settings()
    now = utcnow()
    creds = hash_password(settings.bootstrap_admin_password)
    conn.execute(
        """
        INSERT INTO users (username, display_name, password_hash, password_salt,
                           password_algo, password_params, is_admin, is_active,
                           must_change_password, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, 1, 1, ?, ?)
        """,
        (
            settings.bootstrap_admin_username,
            "Administrator",
            creds["password_hash"],
            creds["password_salt"],
            creds["password_algo"],
            creds["password_params"],
            now,
            now,
        ),
    )
    log.warning(
        "Seeded bootstrap admin %r with the default password. It must be changed "
        "at first login.",
        settings.bootstrap_admin_username,
    )
    return True


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #


def create_session(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    user_agent: str | None = None,
    ip: str | None = None,
) -> tuple[str, str]:
    """Create a session. Returns ``(raw_token, session_id)``."""
    settings = get_settings()
    token = new_session_token()
    session_id = hash_token(token)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=settings.session_ttl_hours)

    conn.execute(
        """
        INSERT INTO sessions (id, user_id, created_at, expires_at, last_seen_at,
                              user_agent, ip)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            user_id,
            now.isoformat(),
            expires.isoformat(),
            now.isoformat(),
            user_agent,
            ip,
        ),
    )
    return token, session_id


def _reap(conn: sqlite3.Connection, session_id: str) -> None:
    """Drop an unusable session and commit immediately.

    This runs on a request that is about to raise 401, and the request-scoped
    connection rolls back on any exception -- so without its own commit the
    cleanup would be undone every time and dead rows would accumulate forever.
    """
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()


def resolve_session(
    conn: sqlite3.Connection, token: str
) -> tuple[sqlite3.Row, sqlite3.Row] | None:
    """Look up ``(session, user)`` for a raw token, or None if unusable.

    An expired session is deleted on sight rather than left to the cleanup job.
    """
    session_id = hash_token(token)
    session = conn.execute(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if session is None:
        return None

    now = datetime.now(timezone.utc)
    if _parse(session["expires_at"]) <= now:
        _reap(conn, session_id)
        return None

    user = get_user(conn, session["user_id"])
    if user is None or not user["is_active"]:
        _reap(conn, session_id)
        return None

    return session, user


def touch_session(conn: sqlite3.Connection, session: sqlite3.Row) -> None:
    """Slide the expiry, at most once per SLIDING_REFRESH_SECONDS."""
    now = datetime.now(timezone.utc)
    last_seen = session["last_seen_at"]
    if last_seen and (now - _parse(last_seen)).total_seconds() < SLIDING_REFRESH_SECONDS:
        return

    settings = get_settings()
    expires = now + timedelta(hours=settings.session_ttl_hours)
    conn.execute(
        "UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE id = ?",
        (now.isoformat(), expires.isoformat(), session["id"]),
    )


def delete_session(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


def delete_other_sessions(
    conn: sqlite3.Connection, user_id: int, keep_session_id: str | None
) -> int:
    if keep_session_id:
        cur = conn.execute(
            "DELETE FROM sessions WHERE user_id = ? AND id != ?",
            (user_id, keep_session_id),
        )
    else:
        cur = conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    return cur.rowcount


def list_sessions(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM sessions WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()


def purge_expired_sessions(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "DELETE FROM sessions WHERE expires_at <= ?", (utcnow(),)
    )
    return cur.rowcount


def require_user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row:
    row = get_user(conn, user_id)
    if row is None:
        raise NotFoundError("User not found")
    return row
