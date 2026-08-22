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
    "web_search_base_url": ("str", False),
    "web_search_api_key": ("str", True),
    "web_search_timeout_sec": ("int", False),
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
    # Telegram push notifications. Off by default, same reasoning as
    # auto_match_enabled: a working bot token is enough to spam a channel, not
    # something to switch on for an operator without them asking for it.
    # The bot itself is app-wide, admin-configured infrastructure; who it
    # actually messages is per-user now (users.telegram_chat_id and the four
    # users.telegram_notify_* columns in LATE_COLUMNS), not a setting here.
    "telegram_enabled": ("bool", False),
    "telegram_bot_token": ("str", True),
    # Realtime-session text captions during an active recording, over the
    # same LocalAI instance/host as batch diarization but a different
    # endpoint (see services/diarize.realtime_url and routers/live_caption.py's
    # module docstring). Off by default: every open connection holds a
    # persistent /v1/realtime session per channel open for the whole
    # recording, on a box that also serves the LLM and the real diarizer, and
    # an operator should opt into that load deliberately -- same reasoning as
    # auto_match_enabled, not the env-only dev_provider_enabled bar, since
    # nothing here is written anywhere for good.
    "live_caption_enabled": ("bool", False),
    # How often channel_worker commits whatever audio has been forwarded
    # since the last commit. Server-side VAD is deliberately off (see
    # live_caption.py's module docstring: it only commits on a real pause,
    # so continuous speech got zero captions until the speaker stopped) --
    # this is what actually bounds "how long until words show up" now.
    # Tuned low: a manual commit measured ~50-150ms turnaround against this
    # deployment, cheap enough that a snappier cadence costs little.
    # channel_worker_transcriptions (live_caption_backend="transcriptions")
    # reuses the same setting as its chunk length -- there is no separate
    # window/chunk setting any more, see that function's docstring.
    "live_caption_commit_interval_sec": ("float", False),
    # How long one channel's /v1/realtime connection is allowed to take to
    # open and complete its session.update handshake before that channel
    # gives up for the rest of the recording (see live_caption.py's
    # _open_session). Generous on purpose: this is a cold-start budget paid
    # once per channel per recording, not a per-caption one -- there is no
    # equivalent of the old per-window round trip to bound any more, since a
    # session, once open, just stays open.
    "live_caption_timeout_sec": ("int", False),
    # The model must be registered on the LocalAI instance as a realtime
    # *pipeline* model -- confirmed against this deployment that every plain
    # ASR/LLM model (including the batch diarizer's own diarization_model)
    # gets rejected outright with "Model is not a pipeline model" the moment
    # a /v1/realtime connection is opened for it. There is deliberately no
    # "empty means fall back to diarization_model" behaviour here any more
    # (that used to be live_caption_model's default): diarization_model is
    # essentially never a realtime pipeline model, so falling back to it
    # would just swap one guaranteed rejection for another.
    "live_caption_model": ("str", False),
    # ISO-639-1 code ("en"), not a language name ("english") -- carried over
    # from the same requirement on the old per-chunk route, unconfirmed on
    # this one but kept narrow rather than risk the same silent breakage.
    # Empty means "let the model auto-detect", right for a genuinely
    # multilingual meeting. This is only the *default*: the recorder UI lets
    # someone pick a language per recording before Start (a `language`
    # websocket query param, checked in live_caption_ws before falling back
    # to this), for a one-off meeting in a different language than usual
    # without a trip to Settings.
    "live_caption_language": ("str", False),
    # Host:port for the standalone live-stt gRPC service (e.g. "localhost:4030").
    # Used when live_caption_model is set to a live-stt model (e.g. "realtime_eou_120m-v1").
    "live_stt_url": ("str", False),
    # Backend type for live captions: "live_stt" (gRPC), "realtime"
    # (persistent /v1/realtime WebSocket session, the default), or
    # "transcriptions" (periodic POST to the stateless /v1/audio/transcriptions
    # route -- see live_caption.py's module docstring for why this is the
    # last resort of the three, not the default).
    "live_caption_backend": ("str", False),


    # Periodic LLM analysis of the rolling live-caption transcript during a
    # recording (see routers/insights.py) -- a distinct model from llm_model,
    # reusing llm_base_url/llm_api_key/llm_ssl_verify/llm_timeout_sec, since
    # this wants a cheaper/faster model than the one tuned for a full-length
    # summary. Empty model means "not configured": no separate *_enabled
    # flag, because setting a model here already is the opt-in -- unlike
    # live captions this is a plain periodic POST driven by a visibly open
    # panel, not a background connection that outlives the user noticing it.
    "insights_model": ("str", False),
    "insights_interval_sec": ("int", False),
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
    # A recording longer than this is diarized in pieces instead of one request
    # (see pipeline._diarize_in_chunks). vibevoice-cpp-asr has an internal
    # output budget, not a duration budget -- confirmed on a real ~59 min
    # recording (meeting 24) that came back as one degenerate segment holding
    # a truncated JSON dump instead of real turns, while a ~57 min recording
    # with less to say in it diarized fine. A talkative recording can hit the
    # limit sooner than a quiet one of the same length, so this stays a
    # deliberate margin below the observed ~57/~59 min boundary rather than
    # right up against it.
    diarize_chunk_threshold_sec: float = 3000.0  # 50 min
    diarize_chunk_size_sec: float = 1500.0  # 25 min

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

    # --- web search -----------------------------------------------------------
    # The AI chat's web_search tool -- an OpenAI-compatible-adjacent search API
    # (POST {base_url}/v1/search, {"query": ...}), same key/base_url shape as
    # the llm block above. Empty means the tool is off: format_tool_result()
    # says so rather than trying to reach a blank host.
    web_search_base_url: str = ""
    web_search_api_key: str = ""
    web_search_timeout_sec: int = 20

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

    # --- telegram -------------------------------------------------------------
    telegram_enabled: bool = False
    telegram_bot_token: str = ""

    # --- live captions ----------------------------------------------------
    # See RUNTIME_KEYS above for why this is a Settings toggle rather than an
    # env-only flag. Reuses diarization_url's host/key -- see
    # services/diarize.realtime_url -- but the model is its own setting
    # (live_caption_model), since it must specifically be one registered as a
    # realtime pipeline model, which diarization_model essentially never is.
    live_caption_enabled: bool = False
    # See RUNTIME_KEYS above -- server VAD is off, so this is what actually
    # paces captions now.
    live_caption_commit_interval_sec: float = 2.0
    # See RUNTIME_KEYS above -- a per-channel connect+handshake budget, not a
    # per-caption one.
    live_caption_timeout_sec: int = 45
    # No fallback-to-diarization_model default any more -- see RUNTIME_KEYS
    # above for why. This is the one model confirmed registered as a
    # realtime pipeline on the deployment this was built against; an
    # operator pointing at a different LocalAI instance will need to confirm
    # what that instance has registered the same way.
    live_caption_model: str = "lfm2.5-audio-1.5b-realtime"
    # ISO-639-1 code, or empty for auto-detect -- see RUNTIME_KEYS above.
    live_caption_language: str = ""
    live_stt_url: str = "localhost:4030"
    live_caption_backend: str = "live_stt"



    # --- insights ---------------------------------------------------------
    # See RUNTIME_KEYS above. 30s balances "feels live" against the cost of a
    # real reasoning call (not just ASR, like live captions) on every tick,
    # and gives a topic or a question a few exchanges to become unambiguous
    # before the model is asked to judge it.
    insights_model: str = ""
    insights_interval_sec: int = 30

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
