"""Application settings.

Two layers, in precedence order:

1. ``app_settings`` rows in the database (runtime-editable from the Settings page)
2. ``Settings`` below, populated from ``.env`` / environment

Use :func:`effective` to read anything a user is allowed to change at runtime; read
``get_settings()`` directly only for values that are fixed for the process lifetime
(paths, ports, worker counts).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Keys that live in the app_settings table and may be overridden at runtime.
# value_type drives coercion on the way out; is_secret drives masking in the API.
RUNTIME_KEYS: dict[str, tuple[str, bool]] = {
    "diarization_url": ("str", False),
    "diarization_model": ("str", False),
    "diarization_api_key": ("str", True),
    "diarization_timeout_sec": ("int", False),
    "llm_base_url": ("str", False),
    "llm_api_key": ("str", True),
    "llm_model": ("str", False),
    "llm_ssl_verify": ("bool", False),
    "llm_timeout_sec": ("int", False),
    "llm_temperature": ("float", False),
    "summary_prompt_name": ("str", False),
    "match_window_days_before": ("int", False),
    "match_window_days_after": ("int", False),
    "match_max_candidates": ("int", False),
    "match_max_keywords": ("int", False),
    "page_size_default": ("int", False),
}


class Settings(BaseSettings):
    """Environment-backed configuration. All variables are prefixed ``MMN_``."""

    model_config = SettingsConfigDict(
        env_prefix="MMN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- server -------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    data_dir: Path = Path("data")

    # --- auth ---------------------------------------------------------------
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str = "password"
    session_cookie_name: str = "mmn_session"
    session_cookie_secure: bool = False
    session_ttl_hours: int = 336  # 14 days
    password_min_length: int = 10

    # --- diarization --------------------------------------------------------
    diarization_url: str = "http://diarization.internal.example:4012/v1/audio/diarization"
    diarization_model: str = "vibevoice-cpp-asr"
    diarization_api_key: str = ""
    diarization_timeout_sec: int = 1800
    diarize_fake: bool = False
    diarize_fake_delay_sec: float = 2.0
    # Wall-clock seconds of processing per second of audio; used to synthesise a
    # progress bar for a service that reports none. Calibrated from a 22.5 min sample.
    diarize_seconds_per_audio_second: float = 0.3

    # --- llm ----------------------------------------------------------------
    llm_base_url: str = "https://llm.internal.example/v1"
    llm_api_key: str = ""
    llm_model: str = "localai/qwen3.6-35b-a3b"
    llm_ssl_verify: bool = True
    llm_timeout_sec: int = 600
    llm_temperature: float = 0.2
    summary_prompt_name: str = "summary_prompt"
    summary_max_input_tokens: int = 24000

    # --- mcp ----------------------------------------------------------------
    mcp_calendar_url: str = "http://calendar-mcp.internal.example:4006"
    mcp_calendar_token: str = ""
    mcp_email_url: str = "http://email-mcp.internal.example:4003"
    mcp_email_token: str = ""
    mcp_profile: str = "default"
    mcp_timeout_sec: int = 60

    # --- matching -----------------------------------------------------------
    match_window_days_before: int = 7
    match_window_days_after: int = 3
    match_max_candidates: int = 25
    match_max_keywords: int = 8

    # --- jobs ---------------------------------------------------------------
    job_concurrency: int = 2
    jobs_resume_on_start: bool = True
    job_stale_seconds: int = 3600
    job_shutdown_grace_sec: int = 10

    # --- uploads ------------------------------------------------------------
    max_upload_mb: int = 1024
    long_audio_warn_sec: int = 1800

    # --- misc ---------------------------------------------------------------
    page_size_default: int = 20
    page_size_max: int = 100
    web_dist: Path = Path("web/dist")

    allowed_origins: str = ""

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def allowed_origin_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached Settings. Tests use this after mutating the environment."""
    get_settings.cache_clear()


def _coerce(raw: str | None, value_type: str) -> Any:
    if raw is None:
        return None
    if value_type == "int":
        return int(raw)
    if value_type == "float":
        return float(raw)
    if value_type == "bool":
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if value_type == "json":
        return json.loads(raw)
    return raw


def effective(conn, key: str) -> Any:
    """Resolve a runtime-overridable setting: DB row wins, else the env-backed default.

    ``conn`` may be ``None``, in which case only the env layer is consulted.
    """
    if key not in RUNTIME_KEYS:
        raise KeyError(f"{key!r} is not a runtime-overridable setting")

    value_type, _ = RUNTIME_KEYS[key]

    if conn is not None:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        # An empty string means "unset" so a cleared field falls back to .env
        # rather than silently disabling the feature.
        if row is not None and row["value"] not in (None, ""):
            return _coerce(row["value"], value_type)

    return getattr(get_settings(), key)
