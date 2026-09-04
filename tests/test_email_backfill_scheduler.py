"""The timer behind unattended email backfill.

Mirrors ``test_followups.py``'s ``TestScheduler``: driven with ``asyncio.run``
rather than as an async test (the scheduler shares nothing with the
TestClient's own event loop but the database file), against the real
``/api/email-backfill/*`` service functions rather than a fake.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.db import get_conn, utcnow
from app.jobs.email_backfill_scheduler import AutoBackfillScheduler
from app.services import email_bodies as eb
from tests.test_email_backfill import LONG, attach, set_state

DEFAULTS = dict(
    enabled=False,
    swept=0,
    bodies_fetched=0,
    summaries_done=0,
    stalled_users=0,
)

# due_users() operates at the *user* granularity (unlike due_threads(), which
# only ever sees threads that exist), so the bootstrap admin -- always active,
# never swept yet, and owning no threads of its own -- is itself a due
# candidate on every cycle in these tests. Its sweep is a true no-op (no
# threads, so both next_thread_needing_* calls return None immediately), but
# it still counts towards `swept`.
ADMIN_SWEEP = 1


def configure(admin_client, **values):
    resp = admin_client.put("/api/settings", json={"values": values})
    assert resp.status_code == 200, resp.text


def run_due(db_path, **kwargs) -> dict:
    return asyncio.run(AutoBackfillScheduler(db_path).run_due(**kwargs))


def add_fake_integration(db_path, owner_id, *, integration_id=5):
    """A connected account whose provider always returns a body.

    Same shape as test_email_backfill.py's `test_repeated_calls_drain_the_account`
    -- hydration needs a real provider to fetch anything, or every row is simply
    marked unavailable (still a valid "the sweep touched it" signal, but this is
    what lets a test assert a positive fetched count).
    """
    with get_conn(db_path) as conn:
        now = utcnow()
        conn.execute(
            "INSERT INTO integrations (id, user_id, provider, account_key, "
            "account_label, calendar_enabled, email_enabled, auth_type, "
            "config_json, created_at, updated_at) VALUES "
            "(?, ?, 'google', 'k', 'me@acme.com', 0, 1, 'oauth', '{}', ?, ?)",
            (integration_id, owner_id, now, now),
        )


def auto_backfill_at(db_path, user_id) -> str | None:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT auto_backfill_at FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return row["auto_backfill_at"]


class TestScheduler:
    def test_it_does_nothing_until_an_admin_turns_it_on(
        self, user_client, admin_client, db_path
    ):
        user = user_client.get("/api/auth/me").json()
        thread = user_client.post("/api/threads", json={"title": "Atlas"}).json()
        attach(db_path, thread["id"], user["id"], count=2)

        assert run_due(db_path) == DEFAULTS
        assert auto_backfill_at(db_path, user["id"]) is None

    def test_an_enabled_cycle_sweeps_and_fetches_bodies(
        self, user_client, admin_client, db_path, monkeypatch
    ):
        from app.services.providers.base import FetchedEmail

        async def fake_message(self, *, native_id, folder_id=None):
            return FetchedEmail(body=LONG)

        monkeypatch.setattr(
            eb.providers_svc, "build_provider",
            lambda conn, row: type("P", (), {"get_email_message": fake_message})(),
        )
        # Bodies are the whole point of this test -- keep the newly-stored
        # (and now summary-eligible) bodies from reaching the real LLM, which
        # is not mocked here and has nothing to talk to.
        monkeypatch.setattr(eb, "summarise_sync", lambda *a, **k: (None, None))

        user = user_client.get("/api/auth/me").json()
        thread = user_client.post("/api/threads", json={"title": "Atlas"}).json()
        add_fake_integration(db_path, user["id"])
        attach(db_path, thread["id"], user["id"], count=2, integration_id=5)
        configure(admin_client, auto_backfill_enabled=True)

        result = run_due(db_path)

        assert result["enabled"] is True
        assert result["swept"] == 1 + ADMIN_SWEEP
        assert result["bodies_fetched"] == 2
        assert auto_backfill_at(db_path, user["id"]) is not None

    def test_it_also_summarises_stored_bodies(
        self, user_client, admin_client, db_path, monkeypatch
    ):
        monkeypatch.setattr(eb, "summarise_sync", lambda *a, **k: ("A summary", "m"))

        user = user_client.get("/api/auth/me").json()
        thread = user_client.post("/api/threads", json={"title": "Atlas"}).json()
        ids = attach(db_path, thread["id"], user["id"], count=1)
        set_state(db_path, ids[0], body=LONG, body_fetched_at=utcnow())
        configure(admin_client, auto_backfill_enabled=True)

        result = run_due(db_path)

        assert result["summaries_done"] == 1
        assert result["stalled_users"] == 0

    def test_a_stalled_llm_is_reported_but_does_not_loop_forever(
        self, user_client, admin_client, db_path, monkeypatch
    ):
        """A failed summary deliberately stays eligible for retry, which is
        exactly what would make the inner loop spin without the stall check."""
        monkeypatch.setattr(eb, "summarise_sync", lambda *a, **k: (None, None))

        user = user_client.get("/api/auth/me").json()
        thread = user_client.post("/api/threads", json={"title": "Atlas"}).json()
        ids = attach(db_path, thread["id"], user["id"], count=1)
        set_state(db_path, ids[0], body=LONG, body_fetched_at=utcnow())
        configure(admin_client, auto_backfill_enabled=True)

        result = run_due(db_path)

        assert result["stalled_users"] == 1
        assert result["summaries_done"] == 0
        # Stamped anyway -- a stalled LLM on this account must not be retried
        # again next tick.
        assert auto_backfill_at(db_path, user["id"]) is not None

    def test_a_user_is_not_swept_again_until_their_interval_has_passed(
        self, user_client, admin_client, db_path
    ):
        user = user_client.get("/api/auth/me").json()
        thread = user_client.post("/api/threads", json={"title": "Atlas"}).json()
        attach(db_path, thread["id"], user["id"], count=1)
        configure(admin_client, auto_backfill_enabled=True)

        run_due(db_path)

        assert run_due(db_path)["swept"] == 0

    def test_the_interval_is_configurable(
        self, user_client, admin_client, db_path
    ):
        user = user_client.get("/api/auth/me").json()
        thread = user_client.post("/api/threads", json={"title": "Atlas"}).json()
        attach(db_path, thread["id"], user["id"], count=1)
        configure(
            admin_client, auto_backfill_enabled=True, auto_backfill_interval_minutes=5
        )
        run_due(db_path)

        later = datetime.now(timezone.utc) + timedelta(minutes=6)
        assert run_due(db_path, now=later)["swept"] == 1 + ADMIN_SWEEP

    def test_a_deactivated_users_account_is_not_swept(
        self, make_user, admin_client, db_path
    ):
        user, as_user = make_user("carol")
        thread = as_user.post("/api/threads", json={"title": "Carol's work"}).json()
        attach(db_path, thread["id"], user["id"], count=1)
        configure(admin_client, auto_backfill_enabled=True)
        admin_client.delete(f"/api/users/{user['id']}")

        assert run_due(db_path)["swept"] == ADMIN_SWEEP

    def test_the_per_cycle_cap_defers_rather_than_drops(
        self, make_user, admin_client, db_path
    ):
        """Oldest-swept-first, so nobody is starved by the cap."""
        for name in ("dave", "erin", "frank"):
            user, as_user = make_user(name)
            thread = as_user.post("/api/threads", json={"title": name}).json()
            attach(db_path, thread["id"], user["id"], count=1)
        configure(
            admin_client, auto_backfill_enabled=True, auto_backfill_max_users_per_cycle=2
        )

        # 4 due candidates in total (admin + dave/erin/frank), capped at 2 per
        # cycle: two full cycles, then nothing left.
        assert run_due(db_path)["swept"] == 2
        assert run_due(db_path)["swept"] == 2
        assert run_due(db_path)["swept"] == 0

    def test_one_broken_user_does_not_stop_the_cycle(
        self, make_user, admin_client, db_path, monkeypatch
    ):
        user1, as_user1 = make_user("gail")
        thread1 = as_user1.post("/api/threads", json={"title": "First"}).json()
        attach(db_path, thread1["id"], user1["id"], count=1)
        user2, as_user2 = make_user("hank")
        thread2 = as_user2.post("/api/threads", json={"title": "Second"}).json()
        attach(db_path, thread2["id"], user2["id"], count=1)
        configure(admin_client, auto_backfill_enabled=True)

        calls: list[int] = []
        real = eb.next_thread_needing_bodies

        # Targeted at gail specifically -- due_users also sweeps the bootstrap
        # admin every cycle (see ADMIN_SWEEP above), and which candidate is
        # swept first must not change what this test is asserting.
        def explode_for_gail(conn, user_id):
            calls.append(user_id)
            if user_id == user1["id"]:
                raise RuntimeError("provider exploded")
            return real(conn, user_id)

        monkeypatch.setattr(
            "app.jobs.email_backfill_scheduler.email_bodies_svc.next_thread_needing_bodies",
            explode_for_gail,
        )

        result = run_due(db_path)

        assert user1["id"] in calls
        assert user2["id"] in calls, "the other user was still swept"
        assert result["swept"] == 2 + ADMIN_SWEEP
        # Stamped even though gail's sweep crashed -- no retry storm.
        assert auto_backfill_at(db_path, user1["id"]) is not None
        assert auto_backfill_at(db_path, user2["id"]) is not None
