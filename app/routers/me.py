"""Per-user account settings.

Currently just "which MCP profile do my meetings search against" -- a shared
mcp_servers row says how to *reach* the calendar/email server, this says
*whose* calendar and inbox on it belongs to the caller. Anyone can set their
own; there is nothing here an admin needs to gate, since a user overriding
their own profile can't affect anyone else's matches.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from app.deps import CurrentUser, active_user, get_db
from app.logging_config import get_logger
from app.schemas import SetUserMcpProfileRequest, UserMcpProfileOut
from app.services import mcpclient as mcp_svc

router = APIRouter(prefix="/api/me", tags=["me"])
log = get_logger("me")


@router.get("/mcp-profiles", response_model=list[UserMcpProfileOut])
def list_my_mcp_profiles(
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[UserMcpProfileOut]:
    servers = conn.execute("SELECT name FROM mcp_servers ORDER BY name").fetchall()
    return [
        UserMcpProfileOut(**mcp_svc.describe_user_profile(conn, user.id, row["name"]))
        for row in servers
    ]


@router.put("/mcp-profiles/{server_name}", response_model=UserMcpProfileOut)
def set_my_mcp_profile(
    server_name: str,
    payload: SetUserMcpProfileRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> UserMcpProfileOut:
    auth_token, clear_token = mcp_svc.resolve_token_update(payload.auth_token)
    mcp_svc.set_user_override(
        conn,
        user.id,
        server_name,
        profile=payload.profile,
        auth_token=auth_token,
        clear_token=clear_token,
    )
    log.info("user %s set their %s profile to %r", user.username, server_name, payload.profile)
    return UserMcpProfileOut(**mcp_svc.describe_user_profile(conn, user.id, server_name))


@router.delete("/mcp-profiles/{server_name}")
def clear_my_mcp_profile(
    server_name: str,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    removed = mcp_svc.delete_user_override(conn, user.id, server_name)
    log.info("user %s cleared their %s profile override", user.username, server_name)
    return {"ok": True, "removed": removed}


@router.post("/mcp-profiles/{server_name}/test")
async def test_my_mcp_profile(
    server_name: str,
    payload: SetUserMcpProfileRequest | None = None,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Test the caller's own effective config, or unsaved edits from the form."""
    config = mcp_svc.resolve_effective_config(conn, server_name, user.id)

    if payload is not None:
        config.default_profile = payload.profile
        auth_token, clear_token = mcp_svc.resolve_token_update(payload.auth_token)
        if clear_token:
            base = mcp_svc.load_config(conn, server_name)
            config.auth_token = base.auth_token
        elif auth_token is not None:
            config.auth_token = auth_token

    result = await mcp_svc.MCPClient(config).test()
    log.info(
        "user %s tested their %s profile (%s): ok=%s %sms",
        user.username, server_name, config.default_profile, result["ok"], result["latency_ms"],
    )
    return result
