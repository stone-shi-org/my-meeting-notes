"""The Development provider: date resolution, filtering, and the traps it exists to exercise.

Nothing is faked here. The provider reads the real database and returns the real
frozen dataclasses -- that is the whole point of it being a provider rather than
a test double.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.db import get_conn, utcnow
from app.services.providers import dev
from app.services.providers.base import IntegrationRef

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
WIDE_START = NOW - timedelta(days=60)
WIDE_END = NOW + timedelta(days=60)


@pytest.fixture(autouse=True)
def dev_on(monkeypatch):
    from app.config import reset_settings_cache

    monkeypatch.setenv("MMN_DEV_PROVIDER_ENABLED", "1")
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def seeded(initialised_db):
    """A user, a thread, a meeting to anchor to, and a dev integration."""
    with get_conn(initialised_db) as conn:
        now = utcnow()
        conn.execute(
            "INSERT INTO users (id, username, password_hash, password_salt, "
            "created_at, updated_at) VALUES (1, 'u', 'h', 's', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO threads (id, owner_id, title, created_at, updated_at) "
            "VALUES (1, 1, 'Atlas Migration', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO meetings (id, thread_id, owner_id, title, meeting_at, "
            "created_at, updated_at) VALUES (1, 1, 1, 'Kickoff', ?, ?, ?)",
            (NOW.isoformat(), now, now),
        )
        conn.execute(
            "INSERT INTO integrations (id, user_id, provider, account_key, account_label, "
            "calendar_enabled, email_enabled, auth_type, config_json, created_at, updated_at) "
            "VALUES (7, 1, 'dev', 'default', 'Fixtures', 1, 1, 'none', '{}', ?, ?)",
            (now, now),
        )
    return initialised_db


def provider() -> dev.DevProvider:
    return dev.DevProvider(
        IntegrationRef(
            id=7, provider="dev", account_label="Fixtures",
            calendar_enabled=True, email_enabled=True,
        ),
        {},
        {},
    )


def add_email(db_path, **fields):
    values = {
        "integration_id": 7,
        "subject": "Re: Atlas cutover window",
        "sender": "Jane Doe <jane@example.com>",
        "snippet": "Confirming the rollback plan.",
        "date_mode": "relative",
        "offset_minutes": -1440,
        "created_at": utcnow(),
        "updated_at": utcnow(),
        **fields,
    }
    with get_conn(db_path) as conn:
        cols = ", ".join(values)
        conn.execute(
            f"INSERT INTO dev_emails ({cols}) VALUES ({', '.join('?' * len(values))})",
            list(values.values()),
        )


def add_event(db_path, **fields):
    values = {
        "integration_id": 7,
        "summary": "Atlas standup",
        "description": "Weekly sync",
        "date_mode": "relative",
        "offset_minutes": 60,
        "created_at": utcnow(),
        "updated_at": utcnow(),
        **fields,
    }
    with get_conn(db_path) as conn:
        cols = ", ".join(values)
        conn.execute(
            f"INSERT INTO dev_events ({cols}) VALUES ({', '.join('?' * len(values))})",
            list(values.values()),
        )


# --------------------------------------------------------------------------- #
# resolve_when
# --------------------------------------------------------------------------- #


class TestResolveWhen:
    def test_absolute_is_taken_verbatim(self):
        row = {"date_mode": "absolute", "at": "2026-03-11T09:00:00+00:00", "offset_minutes": 999}
        assert dev.resolve_when(row, now=NOW, anchor_at=None) == datetime(
            2026, 3, 11, 9, 0, tzinfo=timezone.utc
        )

    def test_relative_is_an_offset_from_now(self):
        row = {"date_mode": "relative", "at": None, "offset_minutes": -1440}
        assert dev.resolve_when(row, now=NOW, anchor_at=None) == NOW - timedelta(days=1)

    def test_anchored_is_an_offset_from_the_meeting(self):
        row = {"date_mode": "anchored", "at": None, "offset_minutes": 2880}
        resolved = dev.resolve_when(row, now=NOW, anchor_at="2026-01-02T10:00:00+00:00")
        assert resolved == datetime(2026, 1, 4, 10, 0, tzinfo=timezone.utc)

    def test_anchored_survives_its_meeting_being_deleted(self):
        """ON DELETE SET NULL leaves the anchor NULL. Falling back to relative
        keeps the item findable; silently vanishing is the harder bug to spot."""
        row = {"date_mode": "anchored", "at": None, "offset_minutes": 60}
        assert dev.resolve_when(row, now=NOW, anchor_at=None) == NOW + timedelta(hours=60 / 60)

    def test_a_naive_stamp_is_read_as_utc(self):
        row = {"date_mode": "absolute", "at": "2026-03-11T09:00:00", "offset_minutes": 0}
        assert dev.resolve_when(row, now=NOW, anchor_at=None).tzinfo is timezone.utc

    def test_unparseable_absolute_is_dropped_not_guessed(self):
        row = {"date_mode": "absolute", "at": "next tuesday", "offset_minutes": 0}
        assert dev.resolve_when(row, now=NOW, anchor_at=None) is None


# --------------------------------------------------------------------------- #
# Email search
# --------------------------------------------------------------------------- #


class TestEmailSearch:
    async def test_returns_a_candidate_in_window(self, seeded):
        add_email(seeded)
        found = await provider().search_emails(
            keywords=["Atlas"], start=WIDE_START, end=WIDE_END
        )
        assert [e.subject for e in found] == ["Re: Atlas cutover window"]

    async def test_filters_on_keywords(self, seeded):
        """A provider that ignored the query would mean ranking never sees a
        plausible non-match, which is the fixture worth authoring."""
        add_email(seeded, subject="Expenses reminder", snippet="Submit by Friday.")
        assert await provider().search_emails(
            keywords=["Atlas"], start=WIDE_START, end=WIDE_END
        ) == []

    async def test_no_keywords_means_the_window_alone_decides(self, seeded):
        add_email(seeded, subject="Expenses reminder")
        found = await provider().search_emails(keywords=[], start=WIDE_START, end=WIDE_END)
        assert len(found) == 1

    async def test_filters_on_the_window(self, seeded):
        add_email(seeded, offset_minutes=-60 * 24 * 400)
        assert await provider().search_emails(
            keywords=["Atlas"], start=WIDE_START, end=WIDE_END
        ) == []

    async def test_keywords_match_the_sender_too(self, seeded):
        add_email(seeded, subject="Lunch?", snippet="Thai?")
        found = await provider().search_emails(
            keywords=["jane"], start=WIDE_START, end=WIDE_END
        )
        assert len(found) == 1

    async def test_message_id_is_namespaced(self, seeded):
        """App-owned, per the make_uid rule -- not the MCP verbatim exception."""
        add_email(seeded)
        found = await provider().search_emails(keywords=[], start=WIDE_START, end=WIDE_END)
        assert found[0].message_id.startswith("dev:7:")

    async def test_dates_are_iso_by_default(self, seeded):
        add_email(seeded)
        found = await provider().search_emails(keywords=[], start=WIDE_START, end=WIDE_END)
        assert found[0].date.startswith("20")

    async def test_rfc2822_is_emitted_on_request(self, seeded):
        """Stored raw, RFC 2822 sorts lexically above every ISO date. This is how
        matching.normalize_timestamp gets exercised on purpose."""
        add_email(seeded, rfc2822_date=1)
        found = await provider().search_emails(keywords=[], start=WIDE_START, end=WIDE_END)
        assert found[0].date.split(",")[0] in (
            "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"
        )

    async def test_triage_fields_are_never_invented(self, seeded):
        add_email(seeded)
        found = await provider().search_emails(keywords=[], start=WIDE_START, end=WIDE_END)
        assert (found[0].triage_level, found[0].tag, found[0].score) == (None, None, None)

    async def test_only_this_accounts_inbox(self, seeded):
        """Two dev accounts are two inboxes -- which is what makes "aggregate an
        error only when every account of a kind failed" reproducible."""
        with get_conn(seeded) as conn:
            now = utcnow()
            conn.execute(
                "INSERT INTO integrations (id, user_id, provider, account_key, "
                "calendar_enabled, email_enabled, auth_type, config_json, "
                "created_at, updated_at) "
                "VALUES (8, 1, 'dev', 'second', 1, 1, 'none', '{}', ?, ?)",
                (now, now),
            )
        add_email(seeded)
        add_email(seeded, integration_id=8, subject="Atlas someone else's")

        found = await provider().search_emails(keywords=[], start=WIDE_START, end=WIDE_END)
        assert [e.subject for e in found] == ["Re: Atlas cutover window"]


class TestEmailBody:
    async def test_returns_the_snippet_verbatim(self, seeded):
        """Dev fixtures have no separate body column -- the snippet is the
        whole fake email, which is the right amount of fidelity for a provider
        that exists to exercise the pipeline rather than model every real
        provider's body-fetch behaviour."""
        add_email(seeded, snippet="Confirming the rollback plan for Friday.")
        [found] = await provider().search_emails(keywords=[], start=WIDE_START, end=WIDE_END)

        body = await provider().get_email_body(native_id=found.id)
        assert body == "Confirming the rollback plan for Friday."

    async def test_an_unknown_id_returns_none(self, seeded):
        assert await provider().get_email_body(native_id="email-999") is None


# --------------------------------------------------------------------------- #
# Calendar search
# --------------------------------------------------------------------------- #


class TestEventSearch:
    async def test_returns_a_candidate(self, seeded):
        add_event(seeded)
        found = await provider().search_events(
            query="Atlas", start=WIDE_START, end=WIDE_END
        )
        assert [e.summary for e in found] == ["Atlas standup"]

    async def test_attendees_are_cleaned(self, seeded):
        add_event(seeded, attendees_json=json.dumps(["Jane Doe", "jane doe", "", "Bob"]))
        found = await provider().search_events(query="", start=WIDE_START, end=WIDE_END)
        assert found[0].attendees == ("Jane Doe", "Bob")

    async def test_all_day_keeps_a_bare_date(self, seeded):
        """Coercing this to midnight is what puts an all-day event on the wrong
        day west of Greenwich."""
        add_event(seeded, all_day=1)
        found = await provider().search_events(query="", start=WIDE_START, end=WIDE_END)
        assert found[0].start == NOW.date().isoformat() or "T" not in found[0].start

    async def test_a_weekly_series_shares_one_source_uid(self, seeded):
        """Distinct uids, one series identity -- what dedupe_events has to keep
        apart. The cheap stand-in for icloud_recurring.ics."""
        add_event(seeded, repeat_weekly=4)
        found = await provider().search_events(query="", start=WIDE_START, end=WIDE_END)
        assert len(found) == 4
        assert len({e.uid for e in found}) == 4
        assert len({e.source_uid for e in found}) == 1

    async def test_series_instances_outside_the_window_are_dropped(self, seeded):
        # Anchored on the fixed NOW, not the default relative mode -- that
        # resolves against the real wall clock inside search_events, which
        # drifts this test's expected count as real time passes NOW.
        add_event(seeded, repeat_weekly=8, date_mode="absolute", at=NOW.isoformat())
        found = await provider().search_events(
            query="", start=NOW - timedelta(days=1), end=NOW + timedelta(days=15)
        )
        assert len(found) == 3

    async def test_anchored_to_a_meeting(self, seeded):
        add_event(seeded, date_mode="anchored", anchor_meeting_id=1, offset_minutes=2880)
        found = await provider().search_events(query="", start=WIDE_START, end=WIDE_END)
        assert found[0].start.startswith("2026-08-07")


# --------------------------------------------------------------------------- #
# The flag, and the connection test
# --------------------------------------------------------------------------- #


async def test_test_reports_what_is_authored(seeded):
    add_email(seeded)
    add_event(seeded)
    result = await provider().test()
    assert result["ok"] is True
    assert [c["name"] for c in result["checks"]] == [
        "1 email(s) authored",
        "1 event(s) authored",
    ]


def test_the_provider_is_offered_only_when_enabled(monkeypatch):
    from app.config import reset_settings_cache
    from app.services.providers import registry

    assert any(s.id == "dev" for s in registry.all_specs())

    monkeypatch.setenv("MMN_DEV_PROVIDER_ENABLED", "0")
    reset_settings_cache()
    assert not any(s.id == "dev" for s in registry.all_specs())
    # Still resolvable by id, so an existing row does not break every other
    # integration the user has.
    assert registry.spec("dev").id == "dev"


def test_a_dev_row_goes_inert_when_disabled(seeded, monkeypatch):
    from app.config import reset_settings_cache
    from app.services.providers import loader

    with get_conn(seeded) as conn:
        row = conn.execute("SELECT * FROM integrations WHERE id = 7").fetchone()
        assert loader.build_provider(conn, row) is not None

        monkeypatch.setenv("MMN_DEV_PROVIDER_ENABLED", "0")
        reset_settings_cache()
        assert loader.build_provider(conn, row) is None


# --------------------------------------------------------------------------- #
# Threading and direction, offline
# --------------------------------------------------------------------------- #


class TestDevThreading:
    """Authoring a reply chain and an outbound message without a real account.

    This is the whole reason the dev provider is a provider and not a test
    double: the chaining and direction fixtures run through the *real*
    provider -> candidate -> attach -> build_chains path.
    """

    async def test_an_outbound_fixture_is_addressed_from_the_account(self, seeded):
        add_email(seeded, subject="Atlas cutover window", sender="jane@example.com",
                  outbound=1, offset_minutes=-2880)

        [mail] = await provider().search_emails(
            keywords=["atlas"], start=WIDE_START, end=WIDE_END
        )

        assert mail.direction == "outbound"
        # Seen from the other side: the account sent it, to whoever the row names.
        assert mail.sender == "Fixtures"
        assert mail.to_recipients == "jane@example.com"

    async def test_an_inbound_fixture_keeps_its_sender(self, seeded):
        add_email(seeded, subject="Atlas cutover window", sender="jane@example.com")

        [mail] = await provider().search_emails(
            keywords=["atlas"], start=WIDE_START, end=WIDE_END
        )

        assert mail.direction == "inbound"
        assert mail.sender == "jane@example.com"
        assert mail.to_recipients == "Fixtures"

    async def test_a_reply_names_its_parent_by_row_id(self, seeded):
        """`in_reply_to` is another dev_emails row id, expanded into the same
        message-id shape the provider emits -- friendlier to type in the UI."""
        add_email(seeded, subject="Atlas cutover window", offset_minutes=-2880)
        add_email(seeded, subject="Re: Atlas cutover window", in_reply_to=1,
                  offset_minutes=-1440)

        mails = await provider().search_emails(
            keywords=["atlas"], start=WIDE_START, end=WIDE_END
        )
        reply = next(m for m in mails if m.subject.startswith("Re:"))
        parent = next(m for m in mails if not m.subject.startswith("Re:"))

        assert reply.in_reply_to == parent.rfc_message_id
        assert reply.references == (parent.rfc_message_id,)

    async def test_an_authored_chain_groups_and_says_who_is_awaited(self, seeded):
        """End-to-end offline: provider -> candidates -> attach -> build_chains.

        The case that matters is the last assertion. You emailed Jane, she
        replied, so the ball is in *your* court -- and a suggestion engine that
        cannot see that is the one that tells you to send a mail you already sent.
        """
        from app.services import matching as m
        from app.services.email_chains import build_chains

        add_email(seeded, subject="Atlas cutover window", sender="jane@example.com",
                  outbound=1, offset_minutes=-2880)
        add_email(seeded, subject="Re: Atlas cutover window",
                  sender="jane@example.com", in_reply_to=1, offset_minutes=-1440)
        add_email(seeded, subject="Unrelated vendor invoice",
                  sender="billing@vendor.example", offset_minutes=-4320)

        mails = await provider().search_emails(
            keywords=[], start=WIDE_START, end=WIDE_END
        )
        assert len(mails) == 3

        with get_conn(seeded) as conn:
            for mail in mails:
                m.attach_email(
                    conn, thread_id=1, meeting_id=None, user_id=1, email=mail.to_dict()
                )
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM thread_emails WHERE thread_id = 1"
            )]

        chains = build_chains(rows, account_addresses=["Fixtures"])
        by_size = sorted(chains, key=lambda c: c["message_count"])

        assert [c["message_count"] for c in by_size] == [1, 2]
        conversation = by_size[-1]
        assert conversation["subject"] == "Atlas cutover window"
        assert conversation["last_message_from"] == "them"
        assert conversation["awaiting"] == "you"

    async def test_two_dev_accounts_sharing_a_conversation_id_stay_distinct(self, seeded):
        """The namespacing guard, reproducible offline.

        A bare conversation id would merge these; `uid_for` keeps them apart.
        """
        add_email(seeded, subject="Atlas one", conversation_id="shared-thread")

        [mail] = await provider().search_emails(
            keywords=["atlas"], start=WIDE_START, end=WIDE_END
        )
        assert mail.conversation_id == "dev:7:shared-thread"
