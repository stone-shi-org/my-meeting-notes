"""Cached "what's next" suggestion for a thread.

One LLM call, same fallback discipline as ``matching.rank_sync``: on failure
the thread keeps whatever suggestion it already had rather than losing it.
Staleness is derived, not stamped -- see ``threads_svc.compute_next_step_fingerprint``
and ``threads_svc.next_step_needs_refresh``, which the thread list uses to decide
whether to call :func:`refresh_many` for a page of threads.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import weakref

from app.db import get_conn, utcnow
from app.logging_config import get_logger
from app.services import llm as llm_svc
from app.services import email_chains as email_chains_svc
from app.services import prompts as prompts_svc
from app.services import telegram as telegram_svc
from app.services import threads as threads_svc

log = get_logger("next_step")

RECENT_MEETINGS = 5
RECENT_EVENTS = 8
# Conversations, not messages: a chain is the unit the suggestion reasons about.
RECENT_EMAIL_CHAINS = 6
RECENT_NOTES = 5

# How many threads the list view will generate a next step for at once.
# LLM round trips, not Gmail metadata fetches, so kept well below the N+1
# fetch cap CLAUDE.md documents for Gmail (8) -- this is page-load latency a
# person is staring at, not a background sweep.
LIST_REFRESH_CONCURRENCY = 4

# One limiter per application event loop, shared by every concurrent list
# request on that loop. A limiter created inside ``refresh_many`` only caps one
# group section; the home page requests every group independently and could
# otherwise multiply this limit by the number of groups.
_LIST_REFRESH_LIMITERS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _list_refresh_limiter() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    limiter = _LIST_REFRESH_LIMITERS.get(loop)
    if limiter is None:
        limiter = asyncio.Semaphore(LIST_REFRESH_CONCURRENCY)
        _LIST_REFRESH_LIMITERS[loop] = limiter
    return limiter

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

    # Every email on the thread, not the newest N: chaining is a whole-set
    # operation, and taking a slice first would cut conversations in half and
    # then report the remainder as if it were the whole exchange. The *chains*
    # are capped below instead.
    emails = conn.execute(
        """
        SELECT id, message_id, subject, sender, date, snippet, account, provider,
               conversation_id, rfc_message_id, in_reply_to, references_json,
               to_recipients, cc_recipients, direction, ai_summary
          FROM thread_emails
         WHERE thread_id = ?
         ORDER BY date
        """,
        (thread_id,),
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
        # Renamed from `recent_emails` deliberately, and in the same commit as
        # the prompt bump: a stale prompt reading the old key would find nothing
        # and quietly decide the thread has no email on it, which is a worse
        # failure than an obvious one.
        "email_chains": _chain_payload(conn, thread_id, emails),
        "recent_notes": [
            {**dict(r), "body": (r["body"] or "")[:NOTE_BODY_LIMIT]} for r in notes
        ],
        "current_next_step": thread["next_step"],
    }


def _chain_payload(
    conn: sqlite3.Connection, thread_id: int, emails: list
) -> list[dict]:
    """Conversations, newest first, each saying who is being waited on.

    Only what the suggestion needs: who it is with, how it stands, and enough of
    the newest message to name a specific. The full bodies stay out -- this
    payload is one JSON blob in one prompt, and a thread with four long threads
    on it would otherwise be mostly quoted history.
    """
    account_addresses = [
        r["a"]
        for r in conn.execute(
            "SELECT DISTINCT i.account_label AS a FROM integrations i "
            "JOIN threads t ON t.owner_id = i.user_id WHERE t.id = ? "
            "UNION SELECT DISTINCT account AS a FROM thread_emails WHERE thread_id = ?",
            (thread_id, thread_id),
        ).fetchall()
        if r["a"]
    ]

    chains = email_chains_svc.build_chains(
        [dict(r) for r in emails], account_addresses=account_addresses
    )

    out = []
    for chain in chains[:RECENT_EMAIL_CHAINS]:
        newest = chain["messages"][-1]
        out.append(
            {
                "subject": chain["subject"],
                "with": chain["participants"][:4],
                "message_count": chain["message_count"],
                "last_message_at": chain["last_message_at"],
                # "you" | "them" | None. None means the direction was never
                # recorded, and the prompt is told not to guess from it.
                "last_message_from": chain["last_message_from"],
                "awaiting": chain["awaiting"],
                "newest_message": {
                    "from": "you" if newest.get("direction") == "outbound"
                    else newest.get("sender"),
                    "date": newest.get("date"),
                    # The AI summary where there is one, else the provider's
                    # snippet. Labelled either way so the model knows which it is.
                    "summary": newest.get("ai_summary"),
                    "snippet": None if newest.get("ai_summary") else newest.get("snippet"),
                },
            }
        )
    return out


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
        raw_next_step = parsed.get("next_step")
        if raw_next_step is None:
            # Model indicated current_next_step is still valid and unchanged
            if payload["current_next_step"]:
                next_step = payload["current_next_step"]
                is_changed = False
            else:
                raise llm_svc.LLMError("Model returned null next step for thread without an existing suggestion")
        else:
            next_step = str(raw_next_step).strip()
            if not next_step:
                raise llm_svc.LLMError("Model returned an empty next step")
            is_changed = (next_step != payload["current_next_step"])
    except Exception as exc:
        log.warning("next-step generation failed for thread %s: %s", thread_id, exc)
        # Stamped even on failure, unlike next_step_generated_at -- this is
        # what lets a caller back off retrying a thread whose generation keeps
        # failing rather than hitting the LLM again on the next poll.
        with get_conn(db_path) as conn:
            conn.execute(
                "UPDATE threads SET next_step_checked_at = ? WHERE id = ?",
                (utcnow(), thread_id),
            )
        return {"next_step": None, "next_step_generated_at": None,
                "next_step_stale": True, "error": str(exc)}

    generated_at = utcnow()
    with get_conn(db_path) as conn:
        conn.execute(
            """
            UPDATE threads
               SET next_step = ?, next_step_generated_at = ?, next_step_checked_at = ?,
                   next_step_fingerprint = ?, next_step_model = ?
             WHERE id = ?
            """,
            (next_step, generated_at, generated_at, fingerprint, config.model, thread_id),
        )

    if is_changed:
        telegram_svc.notify_next_step(
            db_path, thread_id=thread_id, thread_title=payload["thread_title"], next_step=next_step
        )

    return {
        "next_step": next_step,
        "next_step_generated_at": generated_at,
        "next_step_stale": False,
        "error": None,
    }


async def refresh_many(db_path, thread_ids: list[int]) -> dict[int, dict]:
    """Regenerate the next step for several threads at once, off the event loop.

    Used by the thread list when a page loads: several threads can need a
    refresh together, and generating them one at a time would multiply an
    already-slow LLM round trip by the page size. Capped by
    ``LIST_REFRESH_CONCURRENCY`` rather than left unbounded.
    """
    sem = _list_refresh_limiter()

    async def _one(thread_id: int) -> tuple[int, dict]:
        async with sem:
            result = await asyncio.to_thread(generate_sync, db_path, thread_id)
        return thread_id, result

    results = await asyncio.gather(*(_one(tid) for tid in thread_ids))
    return dict(results)
