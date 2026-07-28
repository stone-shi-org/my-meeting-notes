"""The next fortnight of calendar events, and turning one of them into a meeting.

The match pipeline works backwards: upload a recording, then go looking for the
event it belongs to. This is the other direction -- the event comes first. The
home screen lists what is coming up across every connected calendar, and one
click creates the meeting with the title, time and attendee names already filled
in and the event attached to it.

Fan-out mirrors ``matching.gather_candidates`` deliberately, down to sharing its
dedup and error-aggregation rules: one dead calendar must not blank the list, and
the same event seen through two providers must appear once.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

from app.logging_config import get_logger
from app.services import matching as matching_svc
from app.services.providers import loader as providers_svc

log = get_logger("upcoming")

DEFAULT_DAYS = 14
# Zoho Calendar rejects a range over 31 days outright, so the ceiling is set
# below it: the window runs from midnight this morning, which is already up to a
# day longer than the requested span.
MAX_DAYS = 30
MAX_EVENTS = 200


def window(days: int, *, now: datetime | None = None) -> tuple[datetime, datetime]:
    """Midnight this morning to ``days`` ahead, both UTC.

    Starts at midnight rather than at "now" so an event from earlier today -- the
    stand-up you are writing up over lunch -- is still on the list.
    """
    anchor = now or datetime.now(timezone.utc)
    start = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, anchor + timedelta(days=days)


def _sort_key(event: dict) -> tuple[int, str]:
    """Chronological, with anything undated last rather than first.

    All-day events carry a bare ``2026-07-30`` and timed ones a full stamp; both
    sort correctly as ISO strings, and a bare date sorting ahead of that day's
    timed events is the right answer anyway.
    """
    start = event.get("start") or ""
    return (1, "") if not start else (0, start)


def attached_by_uid(conn: sqlite3.Connection, user_id: int) -> dict[str, dict]:
    """Which of this user's calendar events are already on a thread.

    Reads every attached event rather than filtering by the uids just gathered:
    an ``IN`` clause over a few hundred provider uids means chunking around
    SQLite's variable limit, and this table holds one row per event a user has
    ever attached -- thousands, on a single-box app, for a full table scan that
    is cheaper than the round trips it replaces.
    """
    rows = conn.execute(
        """
        SELECT e.uid, e.thread_id, e.meeting_id, m.title AS meeting_title
          FROM thread_calendar_events e
          JOIN threads t ON t.id = e.thread_id
          LEFT JOIN meetings m ON m.id = e.meeting_id
         WHERE t.owner_id = ?
        """,
        (user_id,),
    ).fetchall()

    return {
        row["uid"]: {
            "thread_id": row["thread_id"],
            "meeting_id": row["meeting_id"],
            "meeting_title": row["meeting_title"],
        }
        for row in rows
    }


async def collect(
    conn_factory,
    *,
    user_id: int,
    days: int = DEFAULT_DAYS,
    limit: int = MAX_EVENTS,
) -> dict:
    """Every upcoming event this user's calendars can see, annotated and sorted.

    A user with nothing connected gets an empty list, not an error: this is the
    home screen, and it renders for everyone. ``connected`` is what the SPA
    branches on to offer the "connect a calendar" hint instead.
    """
    days = max(1, min(days, MAX_DAYS))
    start, end = window(days)

    with conn_factory() as conn:
        sources = providers_svc.load_for_user(conn, user_id, kind="calendar")

    if not sources:
        return {
            "connected": 0,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "events": [],
            "error": None,
            "source_errors": [],
        }

    results = await asyncio.gather(
        *(source.search_events(query=None, start=start, end=end) for source in sources),
        return_exceptions=True,
    )

    events: list[dict] = []
    source_errors: list[dict] = []
    for source, result in zip(sources, results):
        if isinstance(result, BaseException):
            message = getattr(result, "message", None) or str(result)
            source_errors.append(
                {
                    "kind": "calendar",
                    "provider": source.ref.provider,
                    "integration_id": source.ref.id,
                    "account": source.ref.display,
                    "error": message,
                }
            )
            log.warning("upcoming search failed for %s: %s", source.ref.display, message)
            continue
        events.extend(candidate.to_dict() for candidate in result)

    # Empty set: an already-attached event stays on the list, marked, so the user
    # can see it is handled instead of wondering why it vanished.
    events = matching_svc.dedupe_events(events, set())
    events.sort(key=_sort_key)
    events = events[:limit]

    with conn_factory() as conn:
        attached = attached_by_uid(conn, user_id)

    for event in events:
        event["attached"] = attached.get(event.get("uid") or "")

    failures = {"calendar": len(source_errors)}
    return {
        "connected": len(sources),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "events": events,
        "error": matching_svc.aggregate_error(source_errors, "calendar", sources, failures),
        "source_errors": source_errors,
    }
