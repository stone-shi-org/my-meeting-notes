"""Cached "what's next" suggestion for a thread.

One LLM call, same fallback discipline as ``matching.rank_sync``: on failure
the thread keeps whatever suggestion it already had rather than losing it.
Staleness is derived, not stamped -- see ``threads_svc.compute_next_step_fingerprint``.
"""

from __future__ import annotations

import json
import sqlite3

from app.db import get_conn, utcnow
from app.logging_config import get_logger
from app.services import llm as llm_svc
from app.services import prompts as prompts_svc
from app.services import threads as threads_svc

log = get_logger("next_step")

RECENT_MEETINGS = 5
RECENT_EVENTS = 8
RECENT_EMAILS = 8
RECENT_NOTES = 5

# Notes go in whole rather than as a snippet -- see chat.NOTE_BODY_LIMIT for
# why -- but this payload holds five of them next to everything else, so the
# per-note cap is tighter than the chat digest's.
NOTE_BODY_LIMIT = 1200


def _payload(conn: sqlite3.Connection, thread_id: int) -> dict:
    """What the prompt is told. Bounded to the most recent handful of each
    kind -- a thread that has been running for a year doesn't need its whole
    history to say what matters right now."""
    thread = threads_svc.require_thread(conn, thread_id)

    meetings = conn.execute(
        """
        SELECT m.title, m.meeting_at, m.status,
               (SELECT s.tldr FROM summaries s
                 WHERE s.meeting_id = m.id AND s.is_current = 1) AS tldr,
               (SELECT GROUP_CONCAT(a.text, ' | ') FROM action_items a
                 WHERE a.meeting_id = m.id AND a.status = 'open') AS open_action_items
          FROM meetings m
         WHERE m.thread_id = ?
         ORDER BY m.meeting_at DESC, m.id DESC
         LIMIT ?
        """,
        (thread_id, RECENT_MEETINGS),
    ).fetchall()

    events = conn.execute(
        """
        SELECT summary, start_at, location
          FROM thread_calendar_events
         WHERE thread_id = ?
         ORDER BY start_at DESC
         LIMIT ?
        """,
        (thread_id, RECENT_EVENTS),
    ).fetchall()

    emails = conn.execute(
        """
        SELECT subject, sender, date, snippet
          FROM thread_emails
         WHERE thread_id = ?
         ORDER BY date DESC
         LIMIT ?
        """,
        (thread_id, RECENT_EMAILS),
    ).fetchall()

    notes = conn.execute(
        """
        SELECT title, body, source, created_at
          FROM thread_notes
         WHERE thread_id = ?
         ORDER BY created_at DESC
         LIMIT ?
        """,
        (thread_id, RECENT_NOTES),
    ).fetchall()

    return {
        "thread_title": thread["title"],
        "thread_description": thread["description"] or "",
        "recent_meetings": [dict(r) for r in meetings],
        "recent_calendar_events": [dict(r) for r in events],
        "recent_emails": [dict(r) for r in emails],
        "recent_notes": [
            {**dict(r), "body": (r["body"] or "")[:NOTE_BODY_LIMIT]} for r in notes
        ],
    }


def generate_sync(db_path, thread_id: int, model: str | None = None) -> dict:
    """One LLM call, run off the event loop by the caller (it's a blocking
    HTTP round trip, same as ``rank_sync``).

    Returns ``{"next_step", "next_step_generated_at", "next_step_stale", "error"}``.
    On failure ``error`` is set and the thread's existing cached suggestion is
    left untouched in the database -- the caller can fall back to what's
    already on the ``Thread`` it has.
    """
    with get_conn(db_path) as conn:
        config = llm_svc.LLMConfig.from_db(conn, model_override=model)
        payload = _payload(conn, thread_id)
        fingerprint = threads_svc.compute_next_step_fingerprint(conn, thread_id)

    prompt = prompts_svc.load("next_step_prompt")
    if prompt.temperature is not None:
        config.temperature = prompt.temperature

    system, user = prompt.render(
        {"payload": json.dumps(payload, ensure_ascii=False, indent=2)}
    )

    try:
        parsed, _, _ = llm_svc.chat_json(config, system, user)
        next_step = (parsed.get("next_step") or "").strip()
        if not next_step:
            raise llm_svc.LLMError("Model returned an empty next step")
    except Exception as exc:
        log.warning("next-step generation failed for thread %s: %s", thread_id, exc)
        return {"next_step": None, "next_step_generated_at": None,
                "next_step_stale": True, "error": str(exc)}

    generated_at = utcnow()
    with get_conn(db_path) as conn:
        conn.execute(
            """
            UPDATE threads
               SET next_step = ?, next_step_generated_at = ?,
                   next_step_fingerprint = ?, next_step_model = ?
             WHERE id = ?
            """,
            (next_step, generated_at, fingerprint, config.model, thread_id),
        )

    return {
        "next_step": next_step,
        "next_step_generated_at": generated_at,
        "next_step_stale": False,
        "error": None,
    }
