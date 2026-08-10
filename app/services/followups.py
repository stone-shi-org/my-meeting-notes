"""The periodic re-match: what turned up on a thread since anyone last looked.

The manual match runs backwards from a recording -- upload, then go looking for
the event it belongs to. This runs forwards and on a timer: for every watched
thread it re-searches the connected calendars and inboxes, and anything the
ranker is *confident* about is attached without being asked. The thread then
carries an unread mark until someone opens the item.

Three rules make unattended attaching safe enough to do at all:

* **A higher bar than suggesting.** ``auto_match_threshold`` defaults to 0.8
  against a 0.6 suggest threshold. Suggesting is reversible with a glance;
  attaching while nobody is watching is not.
* **No ranking, no attaching.** When the LLM is unavailable ``rank_sync``
  returns the candidates with ``relevance_score = None``, and None never clears
  the threshold. The sweep degrades to doing nothing, not to guessing.
* **``meeting_id`` stays NULL.** ``matching.attached_context`` is scoped to one
  meeting, so an unconfirmed follow-up must not become an input to that
  meeting's next summary. It belongs to the thread until a human says otherwise.

Everything else -- the fan-out, the dedup against what is already attached, the
"only aggregate an error when every account of a kind failed" rule -- is
``matching``'s, reused rather than re-implemented.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

from app.config import effective
from app.db import utcnow
from app.errors import NoIntegrationsError
from app.logging_config import get_logger
from app.services import matching as matching_svc
from app.services import telegram as telegram_svc
from app.services import threads as threads_svc

log = get_logger("followups")

# Titles of the last few meetings feed the keyword extraction: a thread's own
# title is often two words, and the recent meetings are what the follow-up email
# will actually be quoting.
KEYWORD_MEETINGS = 3

# A ranker that returns 1.0 for everything would otherwise attach the entire
# window in one tick. Whatever is dropped is logged, never silently discarded --
# it stays a candidate for the next sweep.
MAX_ATTACH_PER_SWEEP = 10


def watch_context(conn: sqlite3.Connection, thread_id: int, keywords: list[str]) -> dict:
    """What the ranking prompt is told about a thread being swept.

    Anchored on the thread's most recent meeting where there is one, so the same
    prompt sees the same shape of context it does in the manual flow.
    """
    latest = conn.execute(
        "SELECT id FROM meetings WHERE thread_id = ? "
        "ORDER BY meeting_at DESC, id DESC LIMIT 1",
        (thread_id,),
    ).fetchone()
    if latest is not None:
        return matching_svc.meeting_context(conn, latest["id"], keywords)

    thread = threads_svc.require_thread(conn, thread_id)
    return {
        "thread_title": thread["title"],
        "thread_description": thread["description"] or "",
        "meeting_title": thread["title"],
        "meeting_datetime": None,
        "meeting_tldr": "",
        "keywords": keywords,
    }


def _keywords(conn: sqlite3.Connection, thread_id: int, limit: int) -> list[str]:
    thread = threads_svc.require_thread(conn, thread_id)
    titles = [
        r["title"]
        for r in conn.execute(
            "SELECT title FROM meetings WHERE thread_id = ? "
            "ORDER BY meeting_at DESC, id DESC LIMIT ?",
            (thread_id, KEYWORD_MEETINGS),
        )
    ]
    return matching_svc.extract_keywords(
        thread["title"] or "", thread["description"] or "", *titles, limit=limit
    )


def _stamp(conn_factory, thread_id: int, error: str | None) -> None:
    """Record that the sweep looked, and how it went.

    Written even on failure: a thread whose provider is down must not be retried
    every tick, or one broken account turns into a request storm against it.
    """
    with conn_factory() as conn:
        conn.execute(
            "UPDATE threads SET auto_match_at = ?, auto_match_error = ? WHERE id = ?",
            (utcnow(), error, thread_id),
        )


def _result(thread_id: int, **fields) -> dict:
    base = {
        "thread_id": thread_id,
        "skipped": None,
        "candidates": 0,
        "attached_events": 0,
        "attached_emails": 0,
        "error": None,
    }
    base.update(fields)
    return base


async def sweep_thread(
    conn_factory,
    *,
    thread_id: int,
    user_id: int,
    db_path=None,
    now: datetime | None = None,
) -> dict:
    """Re-match one thread and attach whatever clears the confidence bar.

    Returns a summary rather than raising: a sweep is unattended work, and one
    thread whose calendar is unreachable must not stop the rest of the cycle.
    """
    anchor = now or datetime.now(timezone.utc)

    with conn_factory() as conn:
        threshold = float(effective(conn, "auto_match_threshold"))
        days_before = effective(conn, "match_window_days_before")
        days_after = effective(conn, "match_window_days_after")
        calendar_days_before = effective(conn, "match_window_calendar_days_before")
        calendar_days_after = effective(conn, "match_window_calendar_days_after")
        max_candidates = effective(conn, "match_max_candidates")
        keywords = _keywords(conn, thread_id, effective(conn, "match_max_keywords"))
        context = watch_context(conn, thread_id, keywords)

    # Anchored on now, not on a meeting: the question here is "what has arrived
    # lately", and the window walks forward with the clock on every tick.
    start = anchor - timedelta(days=days_before)
    end = anchor + timedelta(days=days_after)
    calendar_start = anchor - timedelta(days=calendar_days_before)
    calendar_end = anchor + timedelta(days=calendar_days_after)

    try:
        gathered = await matching_svc.gather_candidates(
            conn_factory,
            thread_id=thread_id,
            keywords=keywords,
            start=start,
            end=end,
            calendar_start=calendar_start,
            calendar_end=calendar_end,
            max_candidates=max_candidates,
            user_id=user_id,
        )
    except NoIntegrationsError:
        # Not an error worth surfacing: the user simply has nothing connected.
        _stamp(conn_factory, thread_id, None)
        return _result(thread_id, skipped="no_integrations")
    except Exception as exc:  # pragma: no cover - defensive
        message = getattr(exc, "message", None) or str(exc)
        _stamp(conn_factory, thread_id, message)
        log.warning("sweep of thread %s failed while searching: %s", thread_id, message)
        return _result(thread_id, error=message)

    candidates = len(gathered["events"]) + len(gathered["emails"])
    if candidates == 0:
        _stamp(conn_factory, thread_id, gathered.get("calendar_error") or gathered.get("email_error"))
        return _result(thread_id, skipped="nothing_new")

    ranked = await asyncio.to_thread(matching_svc.rank_sync, db_path, context, gathered)

    if ranked.get("error"):
        # Unranked candidates all score None, so nothing would attach anyway --
        # say so explicitly rather than reporting a silent zero.
        _stamp(conn_factory, thread_id, f"Ranking unavailable: {ranked['error'][:200]}")
        log.info("sweep of thread %s found %d candidate(s) but could not rank them",
                 thread_id, candidates)
        return _result(thread_id, candidates=candidates, error=ranked["error"])

    def confident(items: list[dict]) -> list[dict]:
        return [
            item for item in items
            if item.get("relevance_score") is not None
            and item["relevance_score"] >= threshold
        ]

    events = confident(ranked["events"])
    emails = confident(ranked["emails"])

    dropped = max(0, len(events) + len(emails) - MAX_ATTACH_PER_SWEEP)
    if dropped:
        log.warning(
            "thread %s: %d confident item(s) over the %d-per-sweep cap were left "
            "for the next sweep",
            thread_id, dropped, MAX_ATTACH_PER_SWEEP,
        )
        events = events[:MAX_ATTACH_PER_SWEEP]
        emails = emails[: max(0, MAX_ATTACH_PER_SWEEP - len(events))]

    if events or emails:
        with conn_factory() as conn:
            for event in events:
                matching_svc.attach_event(
                    conn,
                    thread_id=thread_id,
                    meeting_id=None,
                    event=event,
                    user_id=user_id,
                    auto=True,
                )
            for email in emails:
                matching_svc.attach_email(
                    conn,
                    thread_id=thread_id,
                    meeting_id=None,
                    email=email,
                    user_id=user_id,
                    auto=True,
                )
            # Only when something landed: a sweep that found nothing must not
            # keep bumping the thread to the top of the list every half hour.
            threads_svc.touch_thread(conn, thread_id)
            thread_title = threads_svc.require_thread(conn, thread_id)["title"]

        log.info(
            "thread %s: auto-attached %d event(s) and %d email(s) at or above %.2f",
            thread_id, len(events), len(emails), threshold,
        )

        # A Telegram HTTP call is exactly the blocking I/O this codebase
        # routes off the event loop -- see rank_sync above.
        await asyncio.to_thread(
            telegram_svc.notify_new_attachments,
            conn_factory,
            thread_id=thread_id,
            thread_title=thread_title,
            events=events,
            emails=emails,
        )

    _stamp(conn_factory, thread_id, gathered.get("calendar_error") or gathered.get("email_error"))
    return _result(
        thread_id,
        candidates=candidates,
        attached_events=len(events),
        attached_emails=len(emails),
    )
