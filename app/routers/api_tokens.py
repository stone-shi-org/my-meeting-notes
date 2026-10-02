"""Personal API tokens for the MCP server (MMN-14). See ``services/api_tokens``.

Session-authenticated on purpose: these routes go through ``active_user``,
which only ever accepts a session, so an API token can never be used to mint
another API token.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.config import effective
from app.deps import CurrentUser, active_user, get_db
from app.logging_config import get_logger
from app.mcp_server.tools import TOOL_SPECS
from app.services import api_tokens as tokens_svc

router = APIRouter(prefix="/api/tokens", tags=["api-tokens"])
log = get_logger("api_tokens")


class TokenCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=tokens_svc.NAME_MAX)
    scope: Literal["read", "read_write"] = "read"
    #: Omit for a token that never expires.
    expires_in_days: int | None = Field(default=None, ge=1, le=tokens_svc.MAX_EXPIRY_DAYS)


@router.get("")
def list_tokens(
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """The caller's tokens, plus what the Settings page needs to explain the
    MCP endpoint: whether it is switched on, and what each scope can reach."""
    return {
        "mcp_enabled": bool(effective(conn, "mcp_enabled")),
        "endpoint_path": "/mcp",
        "tools": [
            {"name": spec.name, "title": spec.title, "write": spec.write}
            for spec in TOOL_SPECS
        ],
        "tokens": tokens_svc.list_tokens(conn, user_id=user.id),
    }


@router.post("", status_code=201)
def create_token(
    payload: TokenCreateRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    raw, token = tokens_svc.create_token(
        conn,
        user_id=user.id,
        name=payload.name,
        scope=payload.scope,
        expires_in_days=payload.expires_in_days,
    )
    log.info("user %s created API token %s (%s)", user.username, token["id"], token["scope"])
    # The only response that ever carries the raw value.
    return {**token, "token": raw}


@router.delete("/{token_id}")
def revoke_token(
    token_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    token = tokens_svc.revoke_token(conn, user_id=user.id, token_id=token_id)
    log.info("user %s revoked API token %s", user.username, token_id)
    return token
