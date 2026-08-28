"""Account-wide backfill status and the manual trigger.

Hydration is lazy, so a dormant account stays mostly un-backfilled and nothing in
the app says so. These cover the panel that says so, and the button that fixes it.
"""

from __future__ import annotations

import pytest

from app.db import get_conn, utcnow
from app.services import email_bodies as eb
from app.services import matching as matching_svc
from app.services.providers.base import FetchedEmail

LONG = "word " * 400


@pytest.fixture
def seeded(user_client, other_user_client, isolated_settings):
    """Two users with threads, so ownership scoping is exercised by every test."""
    mine = user_client.post(
        "/api/threads", json={"title": "Atlas", "description": ""}
    ).json()
    theirs = other_user_client.post(
        "/api/threads", json={"title": "Not mine", "description": ""}
    ).json()
    # Alice is not user 1 -- conftest seeds the bootstrap admin first -- so the
    # owner ids have to come from the rows themselves, or an integration seeded
    # under a guessed id belongs to somebody else and is invisible to the
    # owner-scoped lookup.
    return {
        "db": isolated_settings.db_path,
        "mine": mine["id"],
        "mine_owner": mine["owner_id"],
        "theirs": theirs["id"],
        "theirs_owner": theirs["owner_id"],
    }


def attach(db_path, thread_id, owner_id, count=1, **extra):
    ids = []
    with get_conn(db_path) as conn:
        for i in range(count):
            matching_svc.attach_email(
                conn, thread_id=thread_id, meeting_id=None, user_id=owner_id,
                email={
                    "message_id": f"google:5:t{thread_id}m{i}",
                    "id": f"m{i}",
                    "subject": f"Subject {i}",
                    "sender": "priya@acme.com",
                    "date": f"2026-03-{i + 1:02d}T09:00:00+00:00",
                    "provider": "google",
                    "integration_id": 5,
                    **extra,
                },
            )
        ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM thread_emails WHERE thread_id = ?", (thread_id,)
            )
        ]
    return ids


def set_state(db_path, email_id, **cols):
    with get_conn(db_path) as conn:
        conn.execute(
            f"UPDATE thread_emails SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?",
            [*cols.values(), email_id],
        )


class TestStats:
    def test_an_account_with_no_email_reports_zeroes(self, user_client):
        stats = user_client.get("/api/email-backfill/stats").json()
        assert stats["total"] == 0
        assert stats["body_pending"] == 0

    def test_the_three_body_buckets_are_distinct(self, seeded, user_client):
        """"Asked and this account cannot" is not pending. Counting it as pending
        would leave the progress bar permanently short of 100%."""
        ids = attach(seeded["db"], seeded["mine"], seeded["mine_owner"], count=3)
        set_state(seeded["db"], ids[0], body=LONG, body_fetched_at=utcnow())
        set_state(seeded["db"], ids[1], body_fetched_at=utcnow())  # unavailable
        # ids[2] untouched -> pending

        stats = user_client.get("/api/email-backfill/stats").json()

        assert stats["total"] == 3
        assert stats["bodies"] == 1
        assert stats["unavailable"] == 1
        assert stats["body_pending"] == 1

    def test_the_summary_buckets_separate_too_short_from_outstanding(
        self, seeded, user_client
    ):
        ids = attach(seeded["db"], seeded["mine"], seeded["mine_owner"], count=3)
        set_state(seeded["db"], ids[0], body=LONG, body_fetched_at=utcnow(),
                  ai_summary="done", ai_summary_model="m")
        set_state(seeded["db"], ids[1], body=LONG, body_fetched_at=utcnow())
        set_state(seeded["db"], ids[2], body="tiny", body_fetched_at=utcnow())

        stats = user_client.get("/api/email-backfill/stats").json()

        assert stats["summaries"] == 1
        assert stats["summary_pending"] == 1
        assert stats["summary_not_needed"] == 1

    def test_direction_and_threading_coverage_is_reported(self, seeded, user_client):
        """The numbers that say how much of the grouping is fact and how much is
        the subject-overlap guess."""
        attach(seeded["db"], seeded["mine"], seeded["mine_owner"], count=1,
               direction="outbound", conversation_id="google:5:tA")
        ids = attach(seeded["db"], seeded["mine"], seeded["mine_owner"], count=2)
        set_state(seeded["db"], ids[-1], direction="inbound", in_reply_to="<a@x>")

        stats = user_client.get("/api/email-backfill/stats").json()

        assert stats["outbound"] == 1
        assert stats["inbound"] == 1
        assert stats["with_conversation_id"] == 1
        assert stats["with_rfc_headers"] == 1
        # Everything in neither tier chains by subject + participants, a guess.
        assert stats["subject_only"] == stats["total"] - 2

    def test_stats_never_count_another_users_email(self, seeded, user_client):
        attach(seeded["db"], seeded["mine"], seeded["mine_owner"], count=2)
        attach(seeded["db"], seeded["theirs"], seeded["theirs_owner"], count=5)

        assert user_client.get("/api/email-backfill/stats").json()["total"] == 2

    def test_threads_pending_counts_threads_not_messages(self, seeded, user_client):
        attach(seeded["db"], seeded["mine"], seeded["mine_owner"], count=4)
        assert user_client.get("/api/email-backfill/stats").json()["threads_pending"] == 1


class TestBodiesTrigger:
    def test_it_reports_done_when_there_is_nothing_to_do(self, user_client):
        assert user_client.post("/api/email-backfill/bodies").json()["done"] is True

    def test_it_names_the_thread_it_worked_on(self, seeded, user_client):
        attach(seeded["db"], seeded["mine"], seeded["mine_owner"], count=2)

        result = user_client.post("/api/email-backfill/bodies").json()

        assert result["done"] is False
        assert result["thread_id"] == seeded["mine"]
        assert result["thread_title"] == "Atlas"
        assert result["requested"] == 2

    def test_it_takes_the_thread_with_the_most_outstanding_first(
        self, seeded, user_client
    ):
        """So the bar moves fastest at the start, and one large thread is not
        starved behind a queue of small ones."""
        small = user_client.post("/api/threads", json={"title": "Small"}).json()
        attach(seeded["db"], small["id"], seeded["mine_owner"], count=1)
        attach(seeded["db"], seeded["mine"], seeded["mine_owner"], count=5)

        assert user_client.post("/api/email-backfill/bodies").json()["thread_id"] == (
            seeded["mine"]
        )

    def test_it_never_touches_another_users_thread(self, seeded, user_client):
        attach(seeded["db"], seeded["theirs"], seeded["theirs_owner"], count=3)
        assert user_client.post("/api/email-backfill/bodies").json()["done"] is True

    def test_it_makes_no_llm_call(self, seeded, user_client, monkeypatch):
        """Bodies are free; summaries cost money and are a separate button."""
        called = []
        monkeypatch.setattr(
            eb, "summarise_sync", lambda *a, **k: called.append(1) or ("x", "m")
        )
        attach(seeded["db"], seeded["mine"], seeded["mine_owner"], count=2)

        result = user_client.post("/api/email-backfill/bodies").json()

        assert called == []
        assert "summarised" not in result

    def test_repeated_calls_drain_the_account(self, seeded, user_client, monkeypatch):
        """The whole point of the loop: a provider that answers should leave
        nothing pending, however many threads it is spread across."""
        async def fake_message(self, *, native_id, folder_id=None):
            return FetchedEmail(body=LONG)

        monkeypatch.setattr(
            eb.providers_svc, "build_provider",
            lambda conn, row: type("P", (), {"get_email_message": fake_message})(),
        )
        with get_conn(seeded["db"]) as conn:
            now = utcnow()
            conn.execute(
                "INSERT INTO integrations (id, user_id, provider, account_key, "
                "account_label, calendar_enabled, email_enabled, auth_type, "
                "config_json, created_at, updated_at) VALUES "
                "(5, ?, 'google', 'k', 'me@acme.com', 0, 1, 'oauth', '{}', ?, ?)",
                (seeded["mine_owner"], now, now),
            )
        second = user_client.post("/api/threads", json={"title": "Second"}).json()
        attach(seeded["db"], seeded["mine"], seeded["mine_owner"], count=3)
        attach(seeded["db"], second["id"], seeded["mine_owner"], count=2)

        for _ in range(10):
            if user_client.post("/api/email-backfill/bodies").json()["done"]:
                break

        stats = user_client.get("/api/email-backfill/stats").json()
        assert stats["body_pending"] == 0
        assert stats["bodies"] == 5

    def test_an_unauthenticated_caller_is_rejected(self, client):
        assert client.post("/api/email-backfill/bodies").status_code in (401, 403)


class TestSummariesTrigger:
    def test_it_reports_done_when_nothing_is_eligible(self, seeded, user_client):
        # Bodies exist but are all too short to be worth a call.
        ids = attach(seeded["db"], seeded["mine"], seeded["mine_owner"], count=2)
        for i in ids:
            set_state(seeded["db"], i, body="tiny", body_fetched_at=utcnow())

        assert user_client.post("/api/email-backfill/summaries").json()["done"] is True

    def test_it_summarises_a_stored_body(self, seeded, user_client, monkeypatch):
        monkeypatch.setattr(eb, "summarise_sync", lambda *a, **k: ("A summary", "m"))
        ids = attach(seeded["db"], seeded["mine"], seeded["mine_owner"], count=1)
        set_state(seeded["db"], ids[0], body=LONG, body_fetched_at=utcnow())

        result = user_client.post("/api/email-backfill/summaries").json()

        assert result["summarised"] == 1
        assert result["stalled"] is False
        assert user_client.get("/api/email-backfill/stats").json()["summaries"] == 1

    def test_a_failing_llm_reports_stalled_rather_than_looping_forever(
        self, seeded, user_client, monkeypatch
    ):
        """A failed summary deliberately stays eligible so it can be retried --
        which means the client would loop on it forever without this flag."""
        monkeypatch.setattr(eb, "summarise_sync", lambda *a, **k: (None, None))
        ids = attach(seeded["db"], seeded["mine"], seeded["mine_owner"], count=1)
        set_state(seeded["db"], ids[0], body=LONG, body_fetched_at=utcnow())

        result = user_client.post("/api/email-backfill/summaries").json()

        assert result["stalled"] is True
        assert result["requested"] == 1
        assert result["summarised"] == 0
        assert result["remaining"] == 1

    def test_it_never_touches_another_users_thread(self, seeded, user_client):
        ids = attach(seeded["db"], seeded["theirs"], seeded["theirs_owner"], count=1)
        set_state(seeded["db"], ids[0], body=LONG, body_fetched_at=utcnow())

        assert user_client.post("/api/email-backfill/summaries").json()["done"] is True
