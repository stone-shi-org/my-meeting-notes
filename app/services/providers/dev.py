"""The Development provider: a calendar and inbox you fill in by hand.

Exists so the match pipeline, the ranking threshold and the follow-up sweep can
be exercised without a real account. It is a provider like any other rather than
a test double, which is the point -- everything downstream of
``loader.load_for_user`` sees exactly what it sees for Google or CalDAV, so the
thing being tested is the real code path and not a shortcut around it.

Two properties keep it honest, and both are easy to get wrong:

**It filters.** Window *and* keywords, like a real provider. One that returned
its whole table regardless of the query would mean ranking never sees a
plausible non-match -- and near-misses are the fixtures worth authoring.

**It namespaces its uids** through ``base.make_uid``, following the app-owned
rule rather than copying the documented MCP exception. Nothing here predates the
convention, so there is nothing to be bug-compatible with.

Unlike every other provider this one reads the database instead of the network.
It opens its own short-lived connection inside ``to_thread``: ``gather_candidates``
has already closed the request connection by the time it awaits a search, and
sqlite is blocking work like every other sqlite call in the app.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from app.config import get_settings
from app.db import get_conn
from app.logging_config import get_logger
from app.services.providers.base import (
    BaseProvider,
    Check,
    EmailCandidate,
    EventCandidate,
    clean_attendees,
    test_result,
)

log = get_logger("providers.dev")

PROVIDER_ID = "dev"

# How each item's timestamp is worked out. Only the last two survive contact with
# time: an absolute date falls out of the 60/60 match window within a couple of
# months and the fixture silently stops testing anything.
DATE_MODES = ("absolute", "relative", "anchored")

# A weekly series is expanded in the provider, so one authored row becomes N
# candidates sharing a source_uid. Bounded because every instance is persisted
# three times per match run (candidates_json, ranked_json, raw_json).
MAX_REPEAT = 52


def enabled() -> bool:
    """Whether this build will let the Development provider do anything.

    Env-only (``MMN_DEV_PROVIDER_ENABLED``), deliberately not a runtime setting:
    a real deployment should not be one checkbox away from attaching invented
    email to a real thread, and what gets attached stays attached.
    """
    return bool(get_settings().dev_provider_enabled)


# --------------------------------------------------------------------------- #
# When an item happens
# --------------------------------------------------------------------------- #


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # A bare date, or a naive stamp, is read as UTC -- the same assumption the
    # rest of the app makes about its own TEXT timestamps.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def resolve_when(row, *, now: datetime, anchor_at: str | None) -> datetime | None:
    """The moment an authored item actually happens.

    ``anchor_at`` is the anchor meeting's ``meeting_at``, looked up by the
    caller in the same query that fetched the row. An anchored item whose
    meeting has been deleted (``ON DELETE SET NULL``) falls back to *relative*
    rather than vanishing: the offset is still meaningful, and an item silently
    dropping out of every search is the harder failure to diagnose.
    """
    mode = row["date_mode"]
    offset = timedelta(minutes=row["offset_minutes"] or 0)

    if mode == "absolute":
        return _parse(row["at"])
    if mode == "anchored":
        anchor = _parse(anchor_at)
        if anchor is not None:
            return anchor + offset
    return now + offset


def _matches(keywords: list[str], *fields: str | None) -> bool:
    """Case-insensitive substring match on any keyword.

    No keywords means the window alone decides, which is how the real providers
    behave when ``extract_keywords`` came back empty.
    """
    if not keywords:
        return True
    haystack = " ".join(f for f in fields if f).casefold()
    return any(k.casefold() in haystack for k in keywords if k)


# --------------------------------------------------------------------------- #
# The provider
# --------------------------------------------------------------------------- #


class DevProvider(BaseProvider):
    """One account holding both a fake inbox and a fake calendar.

    Both capabilities on one provider (like Google, unlike the split MCP pair)
    so ``calendar_enabled``/``email_enabled`` can switch off half of it and
    reproduce "this user has only connected a calendar".
    """

    provider_id = PROVIDER_ID

    # ---------------------------------------------------------------- email

    async def search_emails(
        self, *, keywords: list[str], start: datetime, end: datetime
    ) -> list[EmailCandidate]:
        rows = await asyncio.to_thread(self._fetch, "dev_emails")
        now = datetime.now(timezone.utc)

        out: list[EmailCandidate] = []
        for row in rows:
            when = resolve_when(row, now=now, anchor_at=row["anchor_at"])
            if when is None or not (start <= when <= end):
                continue
            if not _matches(keywords, row["subject"], row["snippet"], row["sender"]):
                continue
            out.append(self._to_email(row, when))
        return out

    def _to_email(self, row: sqlite3.Row, when: datetime) -> EmailCandidate:
        native = f"email-{row['id']}"
        return EmailCandidate(
            message_id=self.uid_for(native),
            rfc_message_id=f"<{native}@dev.local>",
            id=native,
            sender=row["sender"],
            subject=row["subject"],
            # RFC 2822 on request: stored raw those sort lexically above every
            # ISO date, which is the trap normalize_timestamp exists for.
            date=format_datetime(when) if row["rfc2822_date"] else when.isoformat(),
            snippet=row["snippet"],
            account=row["account"] or self.ref.display,
            url=None,
            # triage_level/tag/reason/score stay None. Only the email-triage MCP
            # server supplies those, and a synthesised triage level reads as real.
            provider=self.provider_id,
            integration_id=self.ref.id,
        )

    # ------------------------------------------------------------- calendar

    async def search_events(
        self, *, query: str | None, start: datetime, end: datetime
    ) -> list[EventCandidate]:
        rows = await asyncio.to_thread(self._fetch, "dev_events")
        now = datetime.now(timezone.utc)
        # The calendar side is handed one pre-joined string rather than a list.
        keywords = [k for k in (query or "").split() if k]

        out: list[EventCandidate] = []
        for row in rows:
            first = resolve_when(row, now=now, anchor_at=row["anchor_at"])
            if first is None:
                continue
            if not _matches(keywords, row["summary"], row["description"], row["location"]):
                continue

            # Expanded here, exactly as the CalDAV provider expands a series
            # locally: one row becomes N instances that dedupe_events has to
            # keep apart, sharing the one source_uid that identifies the series.
            for index in range(max(1, min(row["repeat_weekly"] or 1, MAX_REPEAT))):
                occurrence = first + timedelta(weeks=index)
                if not (start <= occurrence <= end):
                    continue
                out.append(self._to_event(row, occurrence, index))
        return out

    def _to_event(self, row: sqlite3.Row, when: datetime, index: int) -> EventCandidate:
        series = f"event-{row['id']}"
        finish = when + timedelta(minutes=row["duration_minutes"] or 60)

        if row["all_day"]:
            # A bare date with no time, which is what a real all-day event
            # carries -- and what lands on the wrong day west of Greenwich if
            # anything coerces it to midnight.
            start_at, end_at = when.date().isoformat(), finish.date().isoformat()
        else:
            start_at, end_at = when.isoformat(), finish.isoformat()

        try:
            attendees = json.loads(row["attendees_json"] or "[]")
        except ValueError:
            attendees = []

        return EventCandidate(
            uid=self.uid_for(f"{series}-{index}"),
            # One series identity across every instance: this is the half of the
            # pair that cross-provider dedup keys on.
            source_uid=series,
            summary=row["summary"],
            description=row["description"],
            location=row["location"],
            start=start_at,
            end=end_at,
            attendees=clean_attendees(attendees),
            calendar_name=row["calendar_name"] or "Development",
            account=self.ref.display,
            type=row["event_type"],
            url=None,
            provider=self.provider_id,
            integration_id=self.ref.id,
        )

    # ------------------------------------------------------------------ misc

    def _fetch(self, table: str) -> list[sqlite3.Row]:
        """Rows for this account, each carrying its anchor meeting's time.

        Joined here rather than looked up per row: resolving dates one query at
        a time would be an N+1 against the same table the search is already
        reading.
        """
        with get_conn() as conn:
            return conn.execute(
                f"""
                SELECT d.*, m.meeting_at AS anchor_at
                  FROM {table} d
                  LEFT JOIN meetings m ON m.id = d.anchor_meeting_id
                 WHERE d.integration_id = ?
                 ORDER BY d.id
                """,
                (self.ref.id,),
            ).fetchall()

    async def test(self) -> dict:
        """Cannot fail to connect -- but "0 emails, 0 events" is worth saying."""
        started = time.monotonic()
        emails, events = await asyncio.gather(
            asyncio.to_thread(self._fetch, "dev_emails"),
            asyncio.to_thread(self._fetch, "dev_events"),
        )
        latency = int((time.monotonic() - started) * 1000)
        return test_result(
            [
                Check(name=f"{len(emails)} email(s) authored", ok=True),
                Check(name=f"{len(events)} event(s) authored", ok=True),
            ],
            latency,
        )
