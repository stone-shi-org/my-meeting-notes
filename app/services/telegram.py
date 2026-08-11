"""Telegram: per-user push notifications, and the pairing/lookup logic behind
the inbound linked AI chat.

Best-effort outbound: a failed send must never break the work it's reporting
on, so every ``notify_*`` function -- the only entry points the rest of the
app calls -- swallows every error itself and logs a warning instead of
raising.

The bot token is app-wide, admin-configured infrastructure (one bot serves
everyone), but *who it talks to* is per-user: each account links its own
Telegram chat via a one-time code (see ``create_link_code``/``consume_link_code``),
since there is no way for a bot to learn someone's chat id except that person
messaging it first. `app/jobs/telegram_poller.py` owns the getUpdates loop and
calls the pairing functions here; this module is just the transport plus the
per-user config they act on.
"""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timedelta, timezone

import httpx

from app.config import effective
from app.db import get_conn, utcnow
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

# A linking code is a bearer secret for exactly as long as it's valid --
# whoever sends it to the bot gets their Telegram linked to whichever app
# user it was generated for. Short-lived and single-use, the same posture as
# a password-reset token, not a low-stakes convenience string.
LINK_CODE_TTL_MINUTES = 15


class TelegramError(AppError):
    status_code = 502
    code = "telegram_error"


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


def get_updates(bot_token: str, *, offset: int, timeout: int) -> list[dict]:
    """Long-poll for new messages. Telegram holds the connection open up to
    ``timeout`` seconds and returns as soon as an update arrives (or the
    timeout elapses with none), so the request timeout needs real slack over
    it -- a client timeout shorter than the long-poll one would fire first on
    every quiet cycle.
    """
    url = f"{API_BASE}/bot{bot_token}/getUpdates"
    try:
        response = httpx.get(
            url,
            params={"offset": offset, "timeout": timeout},
            timeout=timeout + 5,
        )
    except httpx.HTTPError as exc:
        raise TelegramError(f"Could not reach Telegram: {exc}") from exc

    try:
        body = response.json()
    except ValueError:
        body = {}

    if response.status_code != 200 or not body.get("ok"):
        description = body.get("description") or f"HTTP {response.status_code}"
        raise TelegramError(f"getUpdates failed: {description}")
    return body.get("result") or []


def get_me(bot_token: str) -> dict:
    """A cheap probe that confirms the token is valid without sending anything."""
    url = f"{API_BASE}/bot{bot_token}/getMe"
    try:
        response = httpx.get(url, timeout=REQUEST_TIMEOUT_SEC)
    except httpx.HTTPError as exc:
        raise TelegramError(f"Could not reach Telegram: {exc}") from exc

    try:
        body = response.json()
    except ValueError:
        body = {}

    if response.status_code != 200 or not body.get("ok"):
        description = body.get("description") or f"HTTP {response.status_code}"
        raise TelegramError(f"Telegram rejected the bot token: {description}")
    return body.get("result") or {}


def test_connection(bot_token: str | None, recipient_chat_id: str | None) -> dict:
    """The Settings 'Test' button.

    With a recipient (the calling admin has their own Telegram linked) this is
    a real send -- a cheap probe wouldn't confirm the thing this feature
    promises, that a message actually arrives. Without one (nobody has paired
    yet) it falls back to a ``getMe`` probe so there's still something to test
    before that.
    """
    started = time.monotonic()
    if not bot_token:
        return {"ok": False, "latency_ms": 0, "error": "No bot token configured."}

    if recipient_chat_id:
        try:
            send_message(bot_token, recipient_chat_id, "✅ Test message from My Meeting Notes.")
        except TelegramError as exc:
            return {
                "ok": False,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "error": exc.message,
            }
        return {
            "ok": True,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": None,
            "response": "sent to your linked chat",
        }

    try:
        me = get_me(bot_token)
    except TelegramError as exc:
        return {
            "ok": False,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": exc.message,
        }
    return {
        "ok": True,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "error": None,
        "response": f"bot token valid (@{me.get('username', '?')}) -- link your own "
                    "Telegram below to send a real test message",
    }


def _config(conn) -> dict:
    return {
        "enabled": bool(effective(conn, "telegram_enabled")),
        "bot_token": effective(conn, "telegram_bot_token"),
        "base_url": (effective(conn, "public_base_url") or "").rstrip("/"),
    }


def _owner_telegram_config(conn, owner_id: int) -> dict | None:
    """One user's own notification preferences and linked chat. None if
    they've never linked Telegram at all."""
    row = conn.execute(
        "SELECT telegram_chat_id, telegram_notify_new_attachments, telegram_notify_next_steps, "
        "telegram_notify_transcript_ready, telegram_notify_transcript_failed "
        "FROM users WHERE id = ?",
        (owner_id,),
    ).fetchone()
    if row is None or not row["telegram_chat_id"]:
        return None
    return {
        "chat_id": row["telegram_chat_id"],
        "notify_new_attachments": bool(row["telegram_notify_new_attachments"]),
        "notify_next_steps": bool(row["telegram_notify_next_steps"]),
        "notify_transcript_ready": bool(row["telegram_notify_transcript_ready"]),
        "notify_transcript_failed": bool(row["telegram_notify_transcript_failed"]),
    }


def _thread_link(base_url: str, thread_id: int) -> str:
    return f"{base_url}/threads/{thread_id}" if base_url else ""


def _meeting_link(base_url: str, meeting_id: int) -> str:
    return f"{base_url}/meetings/{meeting_id}" if base_url else ""


def notify_new_attachments(
    conn_factory, *, thread_id: int, thread_title: str, events: list[dict], emails: list[dict]
) -> None:
    """Tell the thread's owner the sweep just auto-attached something.

    ``conn_factory`` opens its own connection per call (see
    ``scheduler.py``'s ``_conn_factory``) -- this runs off the event loop via
    ``asyncio.to_thread``, and a fresh connection is cheaper to reason about
    than sharing one across the thread boundary.
    """
    try:
        with conn_factory() as conn:
            cfg = _config(conn)
            if not cfg["enabled"] or not cfg["bot_token"]:
                return
            owner = conn.execute(
                "SELECT owner_id FROM threads WHERE id = ?", (thread_id,)
            ).fetchone()
            if owner is None:
                return
            owner_cfg = _owner_telegram_config(conn, owner["owner_id"])
        if owner_cfg is None or not owner_cfg["notify_new_attachments"]:
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

        send_message(cfg["bot_token"], owner_cfg["chat_id"], "\n".join(lines))
    except Exception:
        log.exception("telegram notify (new attachments) for thread %s failed", thread_id)


def notify_next_step(db_path, *, thread_id: int, thread_title: str, next_step: str) -> None:
    """Tell the thread's owner a fresh next step was generated.

    Runs inline -- ``generate_sync`` already executes entirely inside a
    worker thread (its callers wrap it in ``asyncio.to_thread``), so no
    further off-loading is needed here.
    """
    try:
        with get_conn(db_path) as conn:
            cfg = _config(conn)
            if not cfg["enabled"] or not cfg["bot_token"]:
                return
            owner = conn.execute(
                "SELECT owner_id FROM threads WHERE id = ?", (thread_id,)
            ).fetchone()
            if owner is None:
                return
            owner_cfg = _owner_telegram_config(conn, owner["owner_id"])
        if owner_cfg is None or not owner_cfg["notify_next_steps"]:
            return

        text = next_step
        if len(text) > NEXT_STEP_MSG_LIMIT:
            text = text[:NEXT_STEP_MSG_LIMIT].rstrip() + "…"

        link = _thread_link(cfg["base_url"], thread_id)
        lines = [f"✅ <b>{_escape(thread_title)}</b>", _escape(text)]
        if link:
            lines.append(link)

        send_message(cfg["bot_token"], owner_cfg["chat_id"], "\n".join(lines))
    except Exception:
        log.exception("telegram notify (next step) for thread %s failed", thread_id)


def notify_transcript_ready(db_path, *, meeting_id: int) -> None:
    """Tell the meeting's owner its transcript finished diarizing.

    Called from ``pipeline._diarize_stage`` off the event loop via
    ``asyncio.to_thread``, right after the transcript is persisted -- not on
    a checkpoint resume or a no-audio skip, both of which return before that
    point.
    """
    try:
        with get_conn(db_path) as conn:
            cfg = _config(conn)
            if not cfg["enabled"] or not cfg["bot_token"]:
                return
            meeting = threads_svc.require_meeting(conn, meeting_id)
            owner_cfg = _owner_telegram_config(conn, meeting["owner_id"])
        if owner_cfg is None or not owner_cfg["notify_transcript_ready"]:
            return

        link = _meeting_link(cfg["base_url"], meeting_id)
        lines = [f"\U0001f399️ <b>{_escape(meeting['title'])}</b> is ready"]
        if link:
            lines.append(link)

        send_message(cfg["bot_token"], owner_cfg["chat_id"], "\n".join(lines))
    except Exception:
        log.exception("telegram notify (transcript ready) for meeting %s failed", meeting_id)


def notify_transcript_failed(db_path, *, meeting_id: int, error: str) -> None:
    """Tell the meeting's owner its transcript failed to generate.

    Called from ``pipeline._diarize_stage`` when the diarization call itself
    raises -- not when a later, independent stage (e.g. summarizing) fails,
    since the transcript is already there by that point.
    """
    try:
        with get_conn(db_path) as conn:
            cfg = _config(conn)
            if not cfg["enabled"] or not cfg["bot_token"]:
                return
            meeting = threads_svc.require_meeting(conn, meeting_id)
            owner_cfg = _owner_telegram_config(conn, meeting["owner_id"])
        if owner_cfg is None or not owner_cfg["notify_transcript_failed"]:
            return

        text = error
        if len(text) > ERROR_MSG_LIMIT:
            text = text[:ERROR_MSG_LIMIT].rstrip() + "…"

        link = _meeting_link(cfg["base_url"], meeting_id)
        lines = [f"⚠️ <b>{_escape(meeting['title'])}</b> failed to transcribe", _escape(text)]
        if link:
            lines.append(link)

        send_message(cfg["bot_token"], owner_cfg["chat_id"], "\n".join(lines))
    except Exception:
        log.exception("telegram notify (transcript failed) for meeting %s failed", meeting_id)


# --------------------------------------------------------------------------- #
# Pairing: linking a Telegram chat to an app user
# --------------------------------------------------------------------------- #


def create_link_code(conn, user_id: int) -> tuple[str, str]:
    """A fresh, single-use linking code for this user.

    Replaces any code already pending for them (only one at a time makes
    sense -- generating a new one means the old one should stop working) and
    sweeps expired rows opportunistically so the table doesn't grow forever.
    """
    now = utcnow()
    conn.execute(
        "DELETE FROM telegram_link_codes WHERE user_id = ? OR expires_at < ?", (user_id, now)
    )
    code = secrets.token_hex(4).upper()
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=LINK_CODE_TTL_MINUTES)).isoformat()
    conn.execute(
        "INSERT INTO telegram_link_codes (code, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (code, user_id, now, expires_at),
    )
    return code, expires_at


def consume_link_code(conn, code: str) -> int | None:
    """Resolve a code to the user it was generated for, if it's still valid.

    Deletes the row unconditionally -- valid, expired, or unknown -- so a
    code is never reusable regardless of the outcome.
    """
    row = conn.execute(
        "SELECT user_id, expires_at FROM telegram_link_codes WHERE code = ?", (code,)
    ).fetchone()
    conn.execute("DELETE FROM telegram_link_codes WHERE code = ?", (code,))
    if row is None or row["expires_at"] < utcnow():
        return None
    return row["user_id"]


def find_user_by_chat_id(conn, chat_id: str):
    return conn.execute("SELECT * FROM users WHERE telegram_chat_id = ?", (chat_id,)).fetchone()


def link_chat(conn, user_id: int, chat_id: str) -> None:
    conn.execute(
        "UPDATE users SET telegram_chat_id = ?, telegram_linked_at = ? WHERE id = ?",
        (chat_id, utcnow(), user_id),
    )


def unlink_chat(conn, user_id: int) -> None:
    conn.execute(
        "UPDATE users SET telegram_chat_id = NULL, telegram_linked_at = NULL WHERE id = ?",
        (user_id,),
    )


def get_link_status(conn, user_id: int) -> dict:
    row = conn.execute(
        "SELECT telegram_chat_id, telegram_linked_at, telegram_notify_new_attachments, "
        "telegram_notify_next_steps, telegram_notify_transcript_ready, "
        "telegram_notify_transcript_failed FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    pending = conn.execute(
        "SELECT code, expires_at FROM telegram_link_codes WHERE user_id = ? AND expires_at > ?",
        (user_id, utcnow()),
    ).fetchone()
    return {
        "linked": bool(row["telegram_chat_id"]) if row else False,
        "linked_at": (row["telegram_linked_at"] if row else None),
        "pending_code": pending["code"] if pending else None,
        "pending_code_expires_at": pending["expires_at"] if pending else None,
        "notify_new_attachments": bool(row["telegram_notify_new_attachments"]) if row else False,
        "notify_next_steps": bool(row["telegram_notify_next_steps"]) if row else False,
        "notify_transcript_ready": bool(row["telegram_notify_transcript_ready"]) if row else False,
        "notify_transcript_failed": bool(row["telegram_notify_transcript_failed"]) if row else False,
    }


def set_notify_preferences(
    conn,
    user_id: int,
    *,
    notify_new_attachments: bool,
    notify_next_steps: bool,
    notify_transcript_ready: bool,
    notify_transcript_failed: bool,
) -> None:
    conn.execute(
        "UPDATE users SET telegram_notify_new_attachments = ?, telegram_notify_next_steps = ?, "
        "telegram_notify_transcript_ready = ?, telegram_notify_transcript_failed = ? WHERE id = ?",
        (
            int(notify_new_attachments), int(notify_next_steps),
            int(notify_transcript_ready), int(notify_transcript_failed), user_id,
        ),
    )
