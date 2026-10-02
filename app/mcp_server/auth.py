"""Who is calling ``/mcp`` (MMN-14).

``Authorization: Bearer <token>`` is the only credential: no cookie fallback.
A browser tab holding a session cookie must not be able to drive the MCP
endpoint from another origin just because it happens to be logged in.

Two kinds of bearer are accepted, API tokens first:

* an **API token** (``mmn_...``) from Settings -> MCP server, carrying its own
  ``read`` / ``read_write`` scope -- what agents are meant to use;
* a **session token**, for a script that already logged in. Treated as
  ``read_write``, since it can already do everything through the REST API.

Every rejection is the same opaque 401 -- unknown, revoked, expired, a
deactivated user and a pending forced password change all look alike, so the
response never confirms that a token exists.
"""

from __future__ import annotations

import sqlite3

from app.deps import CurrentUser
from app.mcp_server.tools import Principal
from app.services import api_tokens as tokens_svc
from app.services import users as users_svc


def bearer_from_headers(headers: list[tuple[bytes, bytes]]) -> str | None:
    for name, value in headers:
        if name.lower() == b"authorization":
            text = value.decode("latin-1").strip()
            if text.lower().startswith("bearer "):
                token = text[7:].strip()
                return token or None
            return None
    return None


def _current_user(user: sqlite3.Row, session_id: str) -> CurrentUser:
    return CurrentUser(
        id=user["id"],
        username=user["username"],
        display_name=user["display_name"],
        is_admin=bool(user["is_admin"]),
        is_active=bool(user["is_active"]),
        must_change_password=bool(user["must_change_password"]),
        session_id=session_id,
    )


def authenticate(conn: sqlite3.Connection, raw: str | None) -> Principal | None:
    """The principal for a raw bearer, or None for any kind of "no"."""
    if not raw:
        return None

    if tokens_svc.looks_like_api_token(raw):
        resolved = tokens_svc.resolve_token(conn, raw)
        if resolved is None:
            return None
        token, user = resolved
        if user["must_change_password"]:
            return None
        tokens_svc.touch_token(conn, token)
        return Principal(
            user=_current_user(user, f"api-token:{token['id']}"),
            scope=token["scope"],
            token_id=token["id"],
        )

    session = users_svc.resolve_session(conn, raw)
    if session is None:
        return None
    session_row, user = session
    if user["must_change_password"]:
        return None
    users_svc.touch_session(conn, session_row)
    return Principal(user=_current_user(user, session_row["id"]), scope="read_write")
