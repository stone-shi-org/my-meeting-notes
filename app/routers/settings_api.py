"""Runtime settings and prompt editing.

Calendar and email connections are *not* here: they are per-user and live under
/api/integrations. This module is app-wide configuration an admin owns.
"""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.config import RUNTIME_KEYS, effective, get_settings
from app.db import utcnow
from app.deps import CurrentUser, active_user, get_db, require_admin
from app.errors import ValidationError
from app.logging_config import get_logger
from app.services import diarize as diarize_svc
from app.services import llm as llm_svc
from app.services import prompts as prompts_svc
from app.services import telegram as telegram_svc
from app.services import web_search as web_search_svc

router = APIRouter(prefix="/api", tags=["settings"])
log = get_logger("settings")

MASK = "••••"


def mask(value: str | None) -> str | None:
    """Show only the tail so the UI can round-trip without leaking the secret."""
    if not value:
        return None
    return f"{MASK}{value[-4:]}" if len(value) > 4 else MASK


class SettingsUpdate(BaseModel):
    values: dict[str, str | int | float | bool | list[str] | None]


class PromptUpdate(BaseModel):
    body: str = Field(min_length=1, max_length=200_000)


class LLMTestRequest(BaseModel):
    base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=200)


class DiarizationTestRequest(BaseModel):
    url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=200)
    live_stt_url: str | None = Field(default=None, max_length=500)
    live_caption_backend: str | None = Field(default=None, max_length=50)




class TelegramTestRequest(BaseModel):
    bot_token: str | None = Field(default=None, max_length=200)


class WebSearchTestRequest(BaseModel):
    base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)


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

        if value_type == "json":
            if value is not None and not isinstance(value, list):
                raise ValidationError(f"{key} must be a list")
            stored = "" if value is None else json.dumps(value)
        else:
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
# Test connection: LLM, diarization, Telegram and web search
#
# All accept an optional body so the Settings form can try unsaved edits before
# Save, and all stay admin-only because that body can point the server at an
# arbitrary host.
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

    live_stt_url = effective(conn, "live_stt_url")
    backend = effective(conn, "live_caption_backend")

    if payload is not None:
        if payload.url:
            url = payload.url
        if payload.model:
            model = payload.model
        if payload.api_key is not None and not payload.api_key.startswith(MASK):
            api_key = payload.api_key
        if payload.live_stt_url:
            live_stt_url = payload.live_stt_url
        if payload.live_caption_backend:
            backend = payload.live_caption_backend

    result = diarize_svc.test_connection(
        url, model, api_key or None, timeout=DIARIZATION_TEST_TIMEOUT_SEC, live_stt_url=live_stt_url, backend=backend
    )



    log.info(
        "admin %s tested diarization (%s): ok=%s %sms",
        admin.username, model, result["ok"], result["latency_ms"],
    )
    return result


@router.post("/telegram/test")
def test_telegram(
    payload: TelegramTestRequest | None = None,
    admin: CurrentUser = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Sends a real message if the calling admin has their own Telegram
    linked; otherwise just validates the bot token via a cheap ``getMe``
    probe, since there's nowhere to send a real message until someone has
    paired (see ``telegram_svc.test_connection``).
    """
    bot_token = effective(conn, "telegram_bot_token")
    if payload is not None and payload.bot_token and not payload.bot_token.startswith(MASK):
        bot_token = payload.bot_token

    link = telegram_svc.get_link_status(conn, admin.id)
    recipient_chat_id = None
    if link["linked"]:
        recipient_chat_id = conn.execute(
            "SELECT telegram_chat_id FROM users WHERE id = ?", (admin.id,)
        ).fetchone()["telegram_chat_id"]

    result = telegram_svc.test_connection(bot_token, recipient_chat_id)
    log.info("admin %s tested Telegram: ok=%s", admin.username, result["ok"])
    return result


@router.post("/web-search/test")
def test_web_search(
    payload: WebSearchTestRequest | None = None,
    admin: CurrentUser = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    config = web_search_svc.WebSearchConfig.from_db(conn)

    if payload is not None:
        if payload.base_url:
            config.base_url = payload.base_url.rstrip("/")
        if payload.api_key is not None and not payload.api_key.startswith(MASK):
            config.api_key = payload.api_key

    result = web_search_svc.test_connection(config)
    log.info(
        "admin %s tested web search: ok=%s %sms",
        admin.username, result["ok"], result["latency_ms"],
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
