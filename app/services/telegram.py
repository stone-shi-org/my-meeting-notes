"""Telegram push notifications for the sweep, next-step generation and
transcription.

Best-effort: a failed send must never break the work it's reporting on, so
every ``notify_*`` function -- the only entry points the rest of the app
calls -- swallows every error itself and logs a warning instead of raising.
"""

from __future__ import annotations

import time

import httpx

from app.config import effective
from app.db import get_conn
from app.errors import AppError
from app.logging_config import get_logger
from app.services import threads as threads_svc

log = get_logger("telegram")

API_BASE = "https://api.telegram.org"
REQUEST_TIMEOUT_SEC = 10
# How many attached item titles to list in a notification before just giving
# the count -- a sweep can attach up to MAX_ATTACH_PER_SWEEP (10) items and a
# message that long is more noise than signal.
MAX_ITEMS_LISTED = 5
# Telegram's own message cap is 4096 chars; a next step is a short suggestion
# but nothing stops a model from returning a wall of text.
NEXT_STEP_MSG_LIMIT = 1000
# A DiarizationError's message can carry a provider's raw response body.
ERROR_MSG_LIMIT = 500


class TelegramError(AppError):
    status_code = 502
    code = "telegram_error"


def parse_chat_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [chat_id.strip() for chat_id in raw.split(",") if chat_id.strip()]


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_message(bot_token: str, chat_id: str, text: str) -> None:
    url = f"{API_BASE}/bot{bot_token}/sendMessage"
    try:
        response = httpx.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=REQUEST_TIMEOUT_SEC,
        )
    except httpx.HTTPError as exc:
        raise TelegramError(f"Could not reach Telegram: {exc}") from exc

    try:
        body = response.json()
    except ValueError:
        body = {}

    if response.status_code != 200 or not body.get("ok"):
        description = body.get("description") or f"HTTP {response.status_code}"
        raise TelegramError(f"Telegram rejected the message to {chat_id}: {description}")


def send_to_all(bot_token: str, chat_ids: list[str], text: str) -> dict:
    """Send to every recipient, collecting failures rather than stopping at the first."""
    errors = []
    for chat_id in chat_ids:
        try:
            send_message(bot_token, chat_id, text)
        except TelegramError as exc:
            errors.append({"chat_id": chat_id, "error": exc.message})
    return {"ok": not errors, "sent": len(chat_ids) - len(errors), "errors": errors}


def test_connection(bot_token: str | None, chat_ids: list[str]) -> dict:
    """The Settings 'Test' button: sends a real message to every recipient.

    Unlike the LLM/diarization tests, a cheap probe (e.g. just calling
    getMe) wouldn't confirm the thing this feature promises -- that a
    message actually arrives -- so the test *is* a real send.
    """
    started = time.monotonic()
    if not bot_token:
        return {"ok": False, "latency_ms": 0, "error": "No bot token configured."}
    if not chat_ids:
        return {"ok": False, "latency_ms": 0, "error": "No chat or channel IDs configured."}

    result = send_to_all(bot_token, chat_ids, "✅ Test message from My Meeting Notes.")
    latency_ms = int((time.monotonic() - started) * 1000)

    if result["ok"]:
        return {
            "ok": True,
            "latency_ms": latency_ms,
            "error": None,
            "response": f"sent to {result['sent']} recipient(s)",
        }
    return {
        "ok": False,
        "latency_ms": latency_ms,
        "error": "; ".join(f"{e['chat_id']}: {e['error']}" for e in result["errors"]),
    }


def _config(conn) -> dict:
    return {
        "enabled": bool(effective(conn, "telegram_enabled")),
        "bot_token": effective(conn, "telegram_bot_token"),
        "chat_ids": parse_chat_ids(effective(conn, "telegram_chat_ids")),
        "notify_new_attachments": bool(effective(conn, "telegram_notify_new_attachments")),
        "notify_next_steps": bool(effective(conn, "telegram_notify_next_steps")),
        "notify_transcript_ready": bool(effective(conn, "telegram_notify_transcript_ready")),
        "notify_transcript_failed": bool(effective(conn, "telegram_notify_transcript_failed")),
        "base_url": (effective(conn, "public_base_url") or "").rstrip("/"),
    }


def _thread_link(base_url: str, thread_id: int) -> str:
    return f"{base_url}/threads/{thread_id}" if base_url else ""


def _meeting_link(base_url: str, meeting_id: int) -> str:
    return f"{base_url}/meetings/{meeting_id}" if base_url else ""


def notify_new_attachments(
    conn_factory, *, thread_id: int, thread_title: str, events: list[dict], emails: list[dict]
) -> None:
    """Tell Telegram the sweep just auto-attached something to a watched thread.

    ``conn_factory`` opens its own connection per call (see
    ``scheduler.py``'s ``_conn_factory``), the same pattern ``rank_sync``
    uses -- this runs off the event loop via ``asyncio.to_thread``, and a
    fresh connection is cheaper to reason about than sharing one across the
    thread boundary.
    """
    try:
        with conn_factory() as conn:
            cfg = _config(conn)
        if not cfg["enabled"] or not cfg["notify_new_attachments"]:
            return
        if not cfg["bot_token"] or not cfg["chat_ids"]:
            return

        lines = [f"\U0001f514 <b>{_escape(thread_title)}</b>"]
        if events:
            lines.append(f"\U0001f4c5 {len(events)} new calendar event(s)")
        if emails:
            lines.append(f"\U0001f4e7 {len(emails)} new email(s)")
        for item in (events + emails)[:MAX_ITEMS_LISTED]:
            label = item.get("summary") or item.get("subject") or "(untitled)"
            lines.append(f"• {_escape(str(label))}")
        link = _thread_link(cfg["base_url"], thread_id)
        if link:
            lines.append(link)

        result = send_to_all(cfg["bot_token"], cfg["chat_ids"], "\n".join(lines))
        if not result["ok"]:
            log.warning(
                "telegram notify (new attachments) for thread %s: %s", thread_id, result["errors"]
            )
    except Exception:
        log.exception("telegram notify (new attachments) for thread %s failed", thread_id)


def notify_next_step(db_path, *, thread_id: int, thread_title: str, next_step: str) -> None:
    """Tell Telegram a fresh next step was generated for a thread.

    Runs inline -- ``generate_sync`` already executes entirely inside a
    worker thread (its callers wrap it in ``asyncio.to_thread``), so no
    further off-loading is needed here.
    """
    try:
        with get_conn(db_path) as conn:
            cfg = _config(conn)
        if not cfg["enabled"] or not cfg["notify_next_steps"]:
            return
        if not cfg["bot_token"] or not cfg["chat_ids"]:
            return

        text = next_step
        if len(text) > NEXT_STEP_MSG_LIMIT:
            text = text[:NEXT_STEP_MSG_LIMIT].rstrip() + "…"

        link = _thread_link(cfg["base_url"], thread_id)
        lines = [f"✅ <b>{_escape(thread_title)}</b>", _escape(text)]
        if link:
            lines.append(link)

        result = send_to_all(cfg["bot_token"], cfg["chat_ids"], "\n".join(lines))
        if not result["ok"]:
            log.warning("telegram notify (next step) for thread %s: %s", thread_id, result["errors"])
    except Exception:
        log.exception("telegram notify (next step) for thread %s failed", thread_id)


def notify_transcript_ready(db_path, *, meeting_id: int) -> None:
    """Tell Telegram a meeting's transcript finished diarizing.

    Called from ``pipeline._diarize_stage`` off the event loop via
    ``asyncio.to_thread``, right after the transcript is persisted -- not on
    a checkpoint resume or a no-audio skip, both of which return before that
    point.
    """
    try:
        with get_conn(db_path) as conn:
            cfg = _config(conn)
            if not cfg["enabled"] or not cfg["notify_transcript_ready"]:
                return
            if not cfg["bot_token"] or not cfg["chat_ids"]:
                return
            meeting = threads_svc.require_meeting(conn, meeting_id)

        link = _meeting_link(cfg["base_url"], meeting_id)
        lines = [f"\U0001f399️ <b>{_escape(meeting['title'])}</b> is ready"]
        if link:
            lines.append(link)

        result = send_to_all(cfg["bot_token"], cfg["chat_ids"], "\n".join(lines))
        if not result["ok"]:
            log.warning(
                "telegram notify (transcript ready) for meeting %s: %s", meeting_id, result["errors"]
            )
    except Exception:
        log.exception("telegram notify (transcript ready) for meeting %s failed", meeting_id)


def notify_transcript_failed(db_path, *, meeting_id: int, error: str) -> None:
    """Tell Telegram a meeting's transcript failed to generate.

    Called from ``pipeline._diarize_stage`` when the diarization call itself
    raises -- not when a later, independent stage (e.g. summarizing) fails,
    since the transcript is already there by that point.
    """
    try:
        with get_conn(db_path) as conn:
            cfg = _config(conn)
            if not cfg["enabled"] or not cfg["notify_transcript_failed"]:
                return
            if not cfg["bot_token"] or not cfg["chat_ids"]:
                return
            meeting = threads_svc.require_meeting(conn, meeting_id)

        text = error
        if len(text) > ERROR_MSG_LIMIT:
            text = text[:ERROR_MSG_LIMIT].rstrip() + "…"

        link = _meeting_link(cfg["base_url"], meeting_id)
        lines = [f"⚠️ <b>{_escape(meeting['title'])}</b> failed to transcribe", _escape(text)]
        if link:
            lines.append(link)

        result = send_to_all(cfg["bot_token"], cfg["chat_ids"], "\n".join(lines))
        if not result["ok"]:
            log.warning(
                "telegram notify (transcript failed) for meeting %s: %s", meeting_id, result["errors"]
            )
    except Exception:
        log.exception("telegram notify (transcript failed) for meeting %s failed", meeting_id)
