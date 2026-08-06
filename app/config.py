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
    # Models selectable in the AI chat panels, in addition to llm_model (which
    # is always implicitly allowed -- see llm_svc.enabled_chat_models). Stored
    # as a JSON array; empty means "chat offers no choice, just llm_model".
    "llm_chat_models": ("json", False),
    "llm_ssl_verify": ("bool", False),
    "llm_timeout_sec": ("int", False),
    "llm_temperature": ("float", False),
    "summary_prompt_name": ("str", False),
    "match_window_days_before": ("int", False),
    "match_window_days_after": ("int", False),
    "match_window_calendar_days_before": ("int", False),
    "match_window_calendar_days_after": ("int", False),
    "match_max_candidates": ("int", False),
    "match_max_keywords": ("int", False),
    # Periodic re-matching. Off by default: it spends LLM budget and provider
    # quota on its own schedule, which is not something to switch on for an
    # operator without them asking for it.
    "auto_match_enabled": ("bool", False),
    "auto_match_interval_minutes": ("int", False),
    "auto_match_threshold": ("float", False),
    "auto_match_max_threads_per_cycle": ("int", False),
    "auto_match_idle_days": ("int", False),
    "page_size_default": ("int", False),
    # Where this app is reachable, used to build OAuth redirect URIs. Google
    # only accepts https:// or http://localhost, so a LAN address here will be
    # rejected by the provider, not by us.
    "public_base_url": ("str", False),
    # OAuth *client* registration is necessarily app-level: the redirect URI has
    # to be pre-registered with the provider. Each user still authorises their
    # own account, and their tokens are per-user.
    "google_client_id": ("str", False),
    "google_client_secret": ("str", True),
    "zoho_client_id": ("str", False),
    "zoho_client_secret": ("str", True),
    # Zoho is regional: an account lives in one data centre and its API hosts
    # carry that suffix. The wrong one authenticates fine and returns nothing.
    "zoho_dc": ("str", False),
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
    llm_chat_models: list[str] = Field(default_factory=list)
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
    match_window_days_after: int = 14
    # Calendars are cheap to search broadly (a date-range list call, not a
    # keyword search) and interviews/appointments are booked further out than
    # a stray email ever sits unanswered, so calendar gets its own, wider window.
    match_window_calendar_days_before: int = 60
    match_window_calendar_days_after: int = 60
    match_max_candidates: int = 25
    match_max_keywords: int = 8

    # --- automatic re-matching ----------------------------------------------
    auto_match_enabled: bool = False
    auto_match_interval_minutes: int = 30
    # Deliberately well above SUGGEST_THRESHOLD (0.6). Suggesting is reversible
    # with a glance; attaching without being asked is not, so the bar is higher.
    auto_match_threshold: float = 0.8
    # Bounds one sweep: with many threads the rest simply wait for the next tick,
    # rather than every provider seeing a burst of N searches at once.
    auto_match_max_threads_per_cycle: int = 20
    # A thread nobody has touched in this long stops being watched. Without it
    # the sweep grows forever and spends its whole budget on dead work.
    auto_match_idle_days: int = 30
    # How often the loop wakes to look for due threads. Process-lifetime, not
    # runtime-editable: it only bounds the granularity of the interval above.
    auto_match_tick_seconds: int = 60

    # --- jobs ---------------------------------------------------------------
    job_concurrency: int = 2
    jobs_resume_on_start: bool = True
    job_stale_seconds: int = 3600
    job_shutdown_grace_sec: int = 10

    # --- uploads ------------------------------------------------------------
    max_upload_mb: int = 1024
    long_audio_warn_sec: int = 1800

    # --- development ---------------------------------------------------------
    # The Development provider: a calendar and inbox you fill in by hand, so the
    # match pipeline and the follow-up sweep can be exercised without a real
    # account. Same idea as diarize_fake above, for the other expensive external
    # dependency.
    #
    # Deliberately env-only and absent from RUNTIME_KEYS: a real deployment must
    # not be one checkbox in Settings away from feeding itself invented email,
    # and anything attached from it lands in thread_emails for good.
    dev_provider_enabled: bool = False

    # --- secrets ------------------------------------------------------------
    # Encrypts per-user integration credentials. Blank means "generate and keep
    # data/secret.key"; set it explicitly to hold the key outside the volume the
    # database lives in. Any string works -- it is normalised into a Fernet key.
    secret_key: str = ""

    # --- oauth --------------------------------------------------------------
    public_base_url: str = "http://localhost:4020"
    google_client_id: str = ""
    google_client_secret: str = ""
    zoho_client_id: str = ""
    zoho_client_secret: str = ""
    zoho_dc: str = "com"

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
