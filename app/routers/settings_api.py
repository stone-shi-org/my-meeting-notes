"""Runtime settings, prompt editing, and MCP server configuration."""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.config import RUNTIME_KEYS, effective, get_settings
from app.db import utcnow
from app.deps import CurrentUser, active_user, get_db, require_admin
from app.errors import NotFoundError, ValidationError
from app.logging_config import get_logger
from app.services import diarize as diarize_svc
from app.services import llm as llm_svc
from app.services import mcpclient as mcp_svc
from app.services import prompts as prompts_svc

router = APIRouter(prefix="/api", tags=["settings"])
log = get_logger("settings")

MASK = "••••"


def mask(value: str | None) -> str | None:
    """Show only the tail so the UI can round-trip without leaking the secret."""
    if not value:
        return None
    return f"{MASK}{value[-4:]}" if len(value) > 4 else MASK


class SettingsUpdate(BaseModel):
    values: dict[str, str | int | float | bool | None]


class PromptUpdate(BaseModel):
    body: str = Field(min_length=1, max_length=200_000)


class MCPServerUpdate(BaseModel):
    transport: str | None = Field(default=None, pattern="^(sse|stdio)$")
    enabled: bool | None = None
    base_url: str | None = Field(default=None, max_length=500)
    auth_token: str | None = Field(default=None, max_length=500)
    command: str | None = Field(default=None, max_length=500)
    args: list[str] | None = None
    cwd: str | None = Field(default=None, max_length=500)
    env: dict[str, str] | None = None
    tool_name: str | None = Field(default=None, max_length=100)
    default_profile: str | None = Field(default=None, max_length=100)
    timeout_sec: int | None = Field(default=None, ge=1, le=3600)


class MCPCallRequest(BaseModel):
    tool: str
    arguments: dict = Field(default_factory=dict)


class LLMTestRequest(BaseModel):
    base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=200)


class DiarizationTestRequest(BaseModel):
    url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=200)


# A cheap connectivity probe (GET /v1/models) doesn't need the multi-minute
# timeout the real diarization POST is configured with.
DIARIZATION_TEST_TIMEOUT_SEC = 15


# --------------------------------------------------------------------------- #
# App settings
# --------------------------------------------------------------------------- #


@router.get("/settings")
def get_settings_api(
    _: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    out = {}
    for key, (value_type, is_secret) in RUNTIME_KEYS.items():
        value = effective(conn, key)
        out[key] = {
            "value": mask(str(value)) if (is_secret and value) else value,
            "type": value_type,
            "is_secret": is_secret,
            "overridden": conn.execute(
                "SELECT 1 FROM app_settings WHERE key = ? AND value != ''", (key,)
            ).fetchone()
            is not None,
        }
    return {"settings": out}


@router.put("/settings")
def update_settings(
    payload: SettingsUpdate,
    admin: CurrentUser = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    unknown = [k for k in payload.values if k not in RUNTIME_KEYS]
    if unknown:
        raise ValidationError(f"Unknown setting(s): {', '.join(sorted(unknown))}")

    updated = []
    for key, value in payload.values.items():
        value_type, is_secret = RUNTIME_KEYS[key]

        # A masked value coming back unchanged from the UI means "leave it".
        if is_secret and isinstance(value, str) and value.startswith(MASK):
            continue

        stored = "" if value is None else (
            str(value).lower() if value_type == "bool" and isinstance(value, bool)
            else str(value)
        )
        conn.execute(
            """
            INSERT INTO app_settings (key, value, value_type, is_secret, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value, updated_by = excluded.updated_by,
                updated_at = excluded.updated_at
            """,
            (key, stored, value_type, int(is_secret), admin.id, utcnow()),
        )
        updated.append(key)

    log.info("admin %s updated settings: %s", admin.username, sorted(updated))
    return {"ok": True, "updated": updated}


# --------------------------------------------------------------------------- #
# Test connection: LLM and diarization
#
# Same shape and admin gate as the MCP test below: accepts an optional body so
# the Settings form can try unsaved edits before Save, and stays admin-only
# because that body can point the server at an arbitrary host.
# --------------------------------------------------------------------------- #


@router.post("/llm/test")
def test_llm(
    payload: LLMTestRequest | None = None,
    admin: CurrentUser = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    config = llm_svc.LLMConfig.from_db(conn)

    if payload is not None:
        if payload.base_url:
            config.base_url = payload.base_url.rstrip("/")
        if payload.model:
            config.model = payload.model
        if payload.api_key is not None and not payload.api_key.startswith(MASK):
            config.api_key = payload.api_key

    result = llm_svc.test_connection(config)
    log.info(
        "admin %s tested the LLM (%s): ok=%s %sms",
        admin.username, config.model, result["ok"], result["latency_ms"],
    )
    return result


@router.post("/diarization/test")
def test_diarization(
    payload: DiarizationTestRequest | None = None,
    admin: CurrentUser = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    url = effective(conn, "diarization_url")
    model = effective(conn, "diarization_model")
    api_key = effective(conn, "diarization_api_key")

    if payload is not None:
        if payload.url:
            url = payload.url
        if payload.model:
            model = payload.model
        if payload.api_key is not None and not payload.api_key.startswith(MASK):
            api_key = payload.api_key

    result = diarize_svc.test_connection(
        url, model, api_key or None, timeout=DIARIZATION_TEST_TIMEOUT_SEC
    )
    log.info(
        "admin %s tested diarization (%s): ok=%s %sms",
        admin.username, model, result["ok"], result["latency_ms"],
    )
    return result


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #


@router.get("/prompts")
def list_prompts(_: CurrentUser = Depends(active_user)) -> list[dict]:
    return prompts_svc.list_prompts()


@router.get("/prompts/{name}")
def get_prompt(name: str, _: CurrentUser = Depends(active_user)) -> dict:
    prompt = prompts_svc.load(name)
    return {
        "name": prompt.name,
        "body": prompt.body,
        "meta": prompt.meta,
        "system": prompt.system,
        "user": prompt.user,
        "sha256": prompt.sha256,
        "version": prompt.version,
        "required_placeholders": prompt.required_placeholders,
    }


@router.put("/prompts/{name}")
def save_prompt(
    name: str,
    payload: PromptUpdate,
    admin: CurrentUser = Depends(require_admin),
) -> dict:
    prompt = prompts_svc.save(name, payload.body)
    log.info("admin %s edited prompt %s", admin.username, name)
    return {
        "ok": True,
        "name": prompt.name,
        "sha256": prompt.sha256,
        "version": prompt.version,
    }


# --------------------------------------------------------------------------- #
# MCP servers
# --------------------------------------------------------------------------- #


def _server_to_dict(config: mcp_svc.MCPServerConfig, row: sqlite3.Row) -> dict:
    return {
        "name": config.name,
        "kind": config.kind,
        "transport": config.transport,
        "enabled": config.enabled,
        "base_url": config.base_url,
        "auth_token": mask(config.auth_token),
        "has_token": bool(config.auth_token),
        "command": config.command,
        "args": config.args,
        "cwd": config.cwd,
        "env": config.env,
        "tool_name": config.tool_name,
        "default_profile": config.default_profile,
        "timeout_sec": config.timeout_sec,
        "last_test": {
            "at": row["last_test_at"],
            "ok": None if row["last_test_ok"] is None else bool(row["last_test_ok"]),
            "error": row["last_test_error"],
            "tools": json.loads(row["last_test_tools_json"] or "[]"),
        },
    }


@router.get("/mcp/servers")
def list_mcp_servers(
    _: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    rows = conn.execute("SELECT * FROM mcp_servers ORDER BY name").fetchall()
    return [_server_to_dict(mcp_svc.MCPServerConfig.from_row(r), r) for r in rows]


@router.put("/mcp/servers/{name}")
def update_mcp_server(
    name: str,
    payload: MCPServerUpdate,
    admin: CurrentUser = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    row = conn.execute("SELECT * FROM mcp_servers WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise NotFoundError(f"MCP server {name!r} not found")

    updates: dict = {}
    for field_name in (
        "transport", "base_url", "command", "cwd", "tool_name",
        "default_profile", "timeout_sec",
    ):
        value = getattr(payload, field_name)
        if value is not None:
            updates[field_name] = value

    if payload.enabled is not None:
        updates["enabled"] = int(payload.enabled)
    if payload.args is not None:
        updates["args_json"] = json.dumps(payload.args)
    if payload.env is not None:
        updates["env_json"] = json.dumps(payload.env)

    # Same masked-value convention as the secret settings.
    if payload.auth_token is not None and not payload.auth_token.startswith(MASK):
        updates["auth_token"] = payload.auth_token

    if updates:
        updates["updated_at"] = utcnow()
        assignments = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE mcp_servers SET {assignments} WHERE name = ?",
            [*updates.values(), name],
        )

    log.info("admin %s updated MCP server %s: %s", admin.username, name, sorted(updates))
    updated = conn.execute("SELECT * FROM mcp_servers WHERE name = ?", (name,)).fetchone()
    return _server_to_dict(mcp_svc.MCPServerConfig.from_row(updated), updated)


@router.post("/mcp/servers/{name}/test")
async def test_mcp_server(
    name: str,
    payload: MCPServerUpdate | None = None,
    admin: CurrentUser = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Connect, handshake, list tools.

    Accepts an optional body so the settings form can test edits *before*
    saving them.
    """
    config = mcp_svc.load_config(conn, name)

    if payload is not None:
        for field_name in (
            "transport", "base_url", "command", "cwd", "tool_name",
            "default_profile", "timeout_sec",
        ):
            value = getattr(payload, field_name)
            if value is not None:
                setattr(config, field_name, value)
        if payload.args is not None:
            config.args = payload.args
        if payload.env is not None:
            config.env = payload.env
        if payload.auth_token is not None and not payload.auth_token.startswith(MASK):
            config.auth_token = payload.auth_token

    result = await mcp_svc.MCPClient(config).test()
    mcp_svc.record_test_result(conn, name, result)

    log.info(
        "admin %s tested MCP %s: ok=%s %sms",
        admin.username, name, result["ok"], result["latency_ms"],
    )
    return result


@router.post("/mcp/servers/{name}/call")
async def call_mcp_tool(
    name: str,
    payload: MCPCallRequest,
    _: CurrentUser = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Debug passthrough. Handy for checking a profile name or query syntax."""
    config = mcp_svc.load_config(conn, name)
    results = await mcp_svc.MCPClient(config).call_tool(payload.tool, payload.arguments)
    return {"count": len(results), "results": results}
