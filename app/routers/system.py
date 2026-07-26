"""Health, version and effective-config endpoints."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends

from app.config import get_settings
from app.db import get_conn
from app.deps import CurrentUser, active_user, get_db
from app.errors import AppError
from app.logging_config import get_logger

router = APIRouter(prefix="/api", tags=["system"])
log = get_logger("system")

_VERSION_FILE = Path("version.txt")


def _ffmpeg_version() -> str | None:
    """First line of `ffmpeg -version`, or None if the binary is missing.

    Surfaced in /api/health so a broken image is obvious at startup rather than at
    the first upload.
    """
    if shutil.which("ffmpeg") is None:
        return None
    try:
        out = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, timeout=10
        )
        return out.stdout.splitlines()[0] if out.stdout else None
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("ffmpeg probe failed: %s", exc)
        return None


def read_version() -> dict:
    if not _VERSION_FILE.exists():
        return {"hash": "dev", "timestamp": None}
    info: dict[str, str] = {}
    for line in _VERSION_FILE.read_text().splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            info[key.strip()] = value.strip()
    return {"hash": info.get("hash", "dev"), "timestamp": info.get("timestamp")}


@router.get("/health")
def health() -> dict:
    settings = get_settings()

    db_ok, db_error = True, None
    try:
        with get_conn() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as exc:  # pragma: no cover - only on a broken volume
        db_ok, db_error = False, str(exc)

    ffmpeg = _ffmpeg_version()

    # Import here: the queue is optional during early phases and in unit tests.
    try:
        from app.jobs.queue import get_queue

        workers = get_queue().worker_count
    except Exception:
        workers = 0

    status = "ok" if (db_ok and ffmpeg is not None) else "degraded"
    return {
        "status": status,
        "db": {"ok": db_ok, "error": db_error, "path": str(settings.db_path)},
        "ffmpeg": ffmpeg,
        "workers": workers,
        "version": read_version(),
    }


@router.get("/version")
def version() -> dict:
    return read_version()


# Model lists change rarely and the upstream call is slow; a short cache keeps
# the Settings dropdown snappy without going stale in any meaningful way.
_MODEL_CACHE: dict[str, tuple[float, list]] = {}
_MODEL_CACHE_TTL = 300.0


def _cached(key: str, loader) -> list:
    import time as _time

    hit = _MODEL_CACHE.get(key)
    now = _time.monotonic()
    if hit and now - hit[0] < _MODEL_CACHE_TTL:
        return hit[1]
    value = loader()
    _MODEL_CACHE[key] = (now, value)
    return value


@router.get("/llm/models")
def llm_models(
    refresh: bool = False,
    _: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    from app.config import effective
    from app.services import llm as llm_svc

    base_url = effective(conn, "llm_base_url")
    api_key = effective(conn, "llm_api_key")
    ssl_verify = effective(conn, "llm_ssl_verify")

    if refresh:
        _MODEL_CACHE.pop("llm", None)

    try:
        models = _cached(
            "llm", lambda: llm_svc.list_models(base_url, api_key, ssl_verify)
        )
    except AppError as exc:
        # A dead endpoint must not break the Settings page; the field stays
        # free-text so the user can still type a model name.
        return {"models": [], "error": exc.message, "base_url": base_url}

    return {"models": models, "error": None, "base_url": base_url}


@router.get("/diarization/models")
def diarization_models(
    refresh: bool = False,
    _: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    from app.config import effective
    from app.services import diarize as diarize_svc

    url = effective(conn, "diarization_url")
    api_key = effective(conn, "diarization_api_key")

    if refresh:
        _MODEL_CACHE.pop("diarization", None)

    try:
        models = _cached(
            "diarization", lambda: diarize_svc.list_models(url, api_key or None)
        )
    except AppError as exc:
        return {"models": [], "error": exc.message, "base_url": url}

    return {"models": models, "error": None, "base_url": url}
