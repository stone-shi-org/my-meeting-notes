"""The periodic sweep: what it attaches, what it refuses to, and the unread mark.

The provider is faked one level above the transport, as in test_matching.py, so
these tests describe "a confident calendar match arrived" rather than an SSE
handshake.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx

from app.db import get_conn
from app.errors import MCPError
from app.jobs.scheduler import AutoMatchScheduler
from app.services.providers.base import EmailCandidate, EventCandidate, IntegrationRef

LLM_URL = "https://llm.test/v1/chat/completions"

EVENTS = [
    {
        "uid": "uid-review", "summary": "Atlas Migration — Cutover review",
        "description": "", "location": "Room 4B",
        "start": "2026-07-30T09:00:00+00:00", "end": "2026-07-30T09:30:00+00:00",
        "calendar_name": "work@x", "account": "work@x", "type": "google",
    },
    {
        "uid": "uid-dentist", "summary": "Dentist", "description": "", "location": "",
        "start": "2026-07-31T14:00:00+00:00", "end": "2026-07-31T15:00:00+00:00",
        "calendar_name": "personal@x", "account": "personal@x", "type": "caldav",
    },
]

EMAILS = [
    {
        "id": "g1", "message_id": "<followup@x>", "sender": "priya@acme.com",
        "subject": "Re: cutover window", "date": "Tue, 28 Jul 2026 17:42:00 +0000",
        "snippet": "notes from the rehearsal", "account": "work@x",
        "triage_level": 1, "tag": "deploy", "reason": "", "summary": "", "score": 0.8,
    },
    {
        "id": "g2", "message_id": "<spam@x>", "sender": "deals@shop.com",
        "subject": "50% off everything", "date": "2026-07-27T08:00:00+00:00",
        "snippet": "shop now", "account": "work@x",
        "triage_level": 4, "tag": None, "reason": "", "summary": "", "score": 0.1,
    },
]

# One calendar item and one email over the 0.8 default, one of each well under it.
RANKING = {
    "calendar": [
        {"ref": "c0", "score": 0.94, "reason": "Same work, two days on.", "suggested": True},
        {"ref": "c1", "score": 0.05, "reason": "Personal.", "suggested": False},
    ],
    "email": [
        {"ref": "e0", "score": 0.85, "reason": "Follow-up on the cutover.", "suggested": True},
        {"ref": "e1", "score": 0.02, "reason": "Marketing.", "suggested": False},
    ],
    "notes": "",
}


class FakeProvider:
    calendar_mode = "ok"
    email_mode = "ok"

    def __init__(self, kind: str, *, integration_id: int, label: str):
        self.ref = IntegrationRef(
            id=integration_id,
            provider="fake",
            account_label=label,
            calendar_enabled=kind == "calendar",
            email_enabled=kind == "email",
        )

    async def search_events(self, **kwargs):
        if FakeProvider.calendar_mode == "down":
            raise MCPError("Could not connect", server="calendar")
        return [EventCandidate(**e) for e in EVENTS]

    async def search_emails(self, **kwargs):
        if FakeProvider.email_mode == "down":
            raise MCPError("rejected the token (401)", server="email")
        return [EmailCandidate(**m) for m in EMAILS]


def fake_load_for_user(conn, user_id, *, kind=None):
    if user_id is None:
        return []
    sources = []
    if kind in (None, "calendar"):
        sources.append(FakeProvider("calendar", integration_id=1, label="calendar@x"))
    if kind in (None, "email"):
        sources.append(FakeProvider("email", integration_id=2, label="email@x"))
    return sources


@pytest.fixture(autouse=True)
def wiring(monkeypatch):
    # One worker: the hand-attach test runs a real match job through the queue.
    monkeypatch.setenv("MMN_JOB_CONCURRENCY", "1")
    monkeypatch.setenv("MMN_LLM_BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("MMN_LLM_MODEL", "test/model")
    from app.config import reset_settings_cache

    reset_settings_cache()

    FakeProvider.calendar_mode = "ok"
    FakeProvider.email_mode = "ok"
    monkeypatch.setattr(
        "app.services.matching.providers_svc.load_for_user", fake_load_for_user
    )


@pytest.fixture
def mock_llm():
    with respx.mock(assert_all_called=False) as router:
        router.post(LLM_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": json.dumps(RANKING)}}],
                    "usage": {"prompt_tokens": 500, "completion_tokens": 200},
                },
            )
        )
        yield router


@pytest.fixture
def meeting(user_client):
    return user_client.post(
        "/api/meetings",
        json={
            "new_thread_title": "Atlas Migration",
            "new_thread_description": "Move billing off Oracle",
            "title": "Cutover go/no-go",
            "meeting_at": "2026-07-28T09:00:00+00:00",
        },
    ).json()


def configure(admin_client, **values):
    resp = admin_client.put("/api/settings", json={"values": values})
    assert resp.status_code == 200, resp.text


def sweep(client, thread_id: int) -> dict:
    resp = client.post(f"/api/threads/{thread_id}/follow-ups")
    assert resp.status_code == 200, resp.text
    return resp.json()


def thread_of(client, thread_id: int) -> dict:
    return client.get(f"/api/threads/{thread_id}").json()


# --------------------------------------------------------------------------- #
# What gets attached
# --------------------------------------------------------------------------- #


class TestSweep:
    def test_confident_matches_are_attached_without_being_asked(
        self, user_client, meeting, mock_llm
    ):
        result = sweep(user_client, meeting["thread_id"])

        assert result["attached_events"] == 1
        assert result["attached_emails"] == 1

        events = user_client.get(
            f"/api/threads/{meeting['thread_id']}/calendar-events"
        ).json()
        emails = user_client.get(f"/api/threads/{meeting['thread_id']}/emails").json()
        assert [e["uid"] for e in events] == ["uid-review"]
        assert [m["message_id"] for m in emails] == ["<followup@x>"]

    def test_anything_under_the_threshold_is_left_alone(self, user_client, meeting, mock_llm):
        """0.05 and 0.02 are candidates, not conclusions."""
        sweep(user_client, meeting["thread_id"])

        events = user_client.get(
            f"/api/threads/{meeting['thread_id']}/calendar-events"
        ).json()
        assert "uid-dentist" not in [e["uid"] for e in events]

    def test_the_threshold_is_configurable(
        self, user_client, admin_client, meeting, mock_llm
    ):
        """At 0.9 the 0.85 email no longer clears the bar; the 0.94 event still does."""
        configure(admin_client, auto_match_threshold=0.9)
        result = sweep(user_client, meeting["thread_id"])

        assert result["attached_events"] == 1
        assert result["attached_emails"] == 0

    def test_attached_items_are_marked_unread(self, user_client, meeting, mock_llm):
        sweep(user_client, meeting["thread_id"])

        events = user_client.get(
            f"/api/threads/{meeting['thread_id']}/calendar-events"
        ).json()
        assert events[0]["auto_attached"] is True
        assert events[0]["unread"] is True
        assert events[0]["seen_at"] is None

        assert thread_of(user_client, meeting["thread_id"])["unread_count"] == 2

    def test_nothing_is_attached_to_the_meeting_itself(self, user_client, meeting, mock_llm):
        """The summarizer reads attached_context, which is scoped to a meeting.

        An item nobody confirmed must not become an input to the next summary of
        a meeting that has already been written up.
        """
        sweep(user_client, meeting["thread_id"])

        events = user_client.get(
            f"/api/threads/{meeting['thread_id']}/calendar-events"
        ).json()
        assert events[0]["meeting_id"] is None

        from app.services import matching as matching_svc

        with get_conn() as conn:
            context = matching_svc.attached_context(conn, meeting["id"])
        assert context == {"events": [], "emails": []}

    def test_an_unrankable_run_attaches_nothing(self, user_client, meeting, mock_llm):
        """No confidence, no attaching. Unranked candidates score None."""
        mock_llm.post(LLM_URL).mock(return_value=httpx.Response(500, text="llm down"))

        result = sweep(user_client, meeting["thread_id"])

        assert result["attached_events"] == 0
        assert result["attached_emails"] == 0
        assert result["error"]
        assert thread_of(user_client, meeting["thread_id"])["unread_count"] == 0

    def test_a_dead_calendar_still_lets_email_through(self, user_client, meeting, mock_llm):
        FakeProvider.calendar_mode = "down"
        result = sweep(user_client, meeting["thread_id"])

        assert result["attached_events"] == 0
        assert result["attached_emails"] == 1

    def test_a_user_with_nothing_connected_is_skipped_not_failed(
        self, user_client, meeting, monkeypatch
    ):
        monkeypatch.setattr(
            "app.services.matching.providers_svc.load_for_user",
            lambda conn, user_id, **kw: [],
        )
        result = sweep(user_client, meeting["thread_id"])

        assert result["skipped"] == "no_integrations"
        assert result["error"] is None

    def test_sweeping_twice_neither_duplicates_nor_re_marks(
        self, user_client, meeting, mock_llm
    ):
        thread_id = meeting["thread_id"]
        sweep(user_client, thread_id)
        emails = user_client.get(f"/api/threads/{thread_id}/emails").json()
        user_client.post(f"/api/threads/{thread_id}/emails/{emails[0]['id']}/read")

        sweep(user_client, thread_id)

        emails = user_client.get(f"/api/threads/{thread_id}/emails").json()
        again = [m for m in emails if m["message_id"] == "<followup@x>"]
        assert len(again) == 1, "already attached, so never a candidate again"
        assert again[0]["unread"] is False, "reading it is not undone by a later sweep"

    def test_the_thread_records_when_it_was_last_swept(self, user_client, meeting, mock_llm):
        assert thread_of(user_client, meeting["thread_id"])["auto_match_at"] is None
        sweep(user_client, meeting["thread_id"])
        assert thread_of(user_client, meeting["thread_id"])["auto_match_at"]

    def test_a_hand_attached_item_is_never_unread(self, user_client, meeting, mock_llm):
        """Confirming a match means looking at it. Only the sweep leaves unread rows."""
        job_id = user_client.post(
            f"/api/meetings/{meeting['id']}/match", json={}
        ).json()["job_id"]
        _wait(user_client, job_id)
        user_client.post(
            f"/api/meetings/{meeting['id']}/match/confirm",
            json={"event_uids": ["uid-review"], "email_message_ids": ["<followup@x>"]},
        )

        emails = user_client.get(f"/api/threads/{meeting['thread_id']}/emails").json()
        assert emails[0]["unread"] is False
        assert emails[0]["auto_attached"] is False
        assert thread_of(user_client, meeting["thread_id"])["unread_count"] == 0


def _wait(client, job_id: str, timeout: float = 20.0):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish")


# --------------------------------------------------------------------------- #
# Clearing the mark
# --------------------------------------------------------------------------- #


class TestUnreadMark:
    def test_reading_one_item_clears_only_that_one(self, user_client, meeting, mock_llm):
        thread_id = meeting["thread_id"]
        sweep(user_client, thread_id)
        events = user_client.get(f"/api/threads/{thread_id}/calendar-events").json()

        resp = user_client.post(
            f"/api/threads/{thread_id}/calendar-events/{events[0]['id']}/read"
        )
        assert resp.status_code == 200
        assert resp.json()["marked"] == 1

        assert thread_of(user_client, thread_id)["unread_count"] == 1
        events = user_client.get(f"/api/threads/{thread_id}/calendar-events").json()
        assert events[0]["unread"] is False
        assert events[0]["seen_at"]

    def test_the_dot_goes_when_the_last_item_is_read(self, user_client, meeting, mock_llm):
        thread_id = meeting["thread_id"]
        sweep(user_client, thread_id)

        for kind in ("calendar-events", "emails"):
            for item in user_client.get(f"/api/threads/{thread_id}/{kind}").json():
                user_client.post(f"/api/threads/{thread_id}/{kind}/{item['id']}/read")

        assert thread_of(user_client, thread_id)["unread_count"] == 0

    def test_mark_all_read(self, user_client, meeting, mock_llm):
        thread_id = meeting["thread_id"]
        sweep(user_client, thread_id)

        assert user_client.post(f"/api/threads/{thread_id}/read").json()["marked"] == 2
        assert thread_of(user_client, thread_id)["unread_count"] == 0

    def test_reading_twice_is_a_no_op_not_an_error(self, user_client, meeting, mock_llm):
        """The SPA fires this on every click of the link, including the second."""
        thread_id = meeting["thread_id"]
        sweep(user_client, thread_id)
        emails = user_client.get(f"/api/threads/{thread_id}/emails").json()
        first = user_client.post(f"/api/threads/{thread_id}/emails/{emails[0]['id']}/read")
        second = user_client.post(f"/api/threads/{thread_id}/emails/{emails[0]['id']}/read")

        assert first.json()["marked"] == 1
        assert second.status_code == 200
        assert second.json()["marked"] == 0

    def test_seen_at_is_not_moved_by_a_second_read(self, user_client, meeting, mock_llm):
        thread_id = meeting["thread_id"]
        sweep(user_client, thread_id)
        emails = user_client.get(f"/api/threads/{thread_id}/emails").json()
        user_client.post(f"/api/threads/{thread_id}/emails/{emails[0]['id']}/read")
        stamped = user_client.get(f"/api/threads/{thread_id}/emails").json()[0]["seen_at"]

        user_client.post(f"/api/threads/{thread_id}/emails/{emails[0]['id']}/read")
        assert user_client.get(f"/api/threads/{thread_id}/emails").json()[0]["seen_at"] == stamped

    def test_an_unknown_item_is_404(self, user_client, meeting):
        assert user_client.post(
            f"/api/threads/{meeting['thread_id']}/emails/9999/read"
        ).status_code == 404

    def test_the_thread_list_carries_the_count(self, user_client, meeting, mock_llm):
        sweep(user_client, meeting["thread_id"])
        page = user_client.get("/api/threads").json()
        card = next(t for t in page["items"] if t["id"] == meeting["thread_id"])
        assert card["unread_count"] == 2


class TestOwnership:
    def test_another_user_can_neither_sweep_nor_read(
        self, user_client, other_user_client, meeting, mock_llm
    ):
        thread_id = meeting["thread_id"]
        sweep(user_client, thread_id)
        email_id = user_client.get(f"/api/threads/{thread_id}/emails").json()[0]["id"]

        assert other_user_client.post(
            f"/api/threads/{thread_id}/follow-ups"
        ).status_code == 404
        assert other_user_client.post(f"/api/threads/{thread_id}/read").status_code == 404
        assert other_user_client.post(
            f"/api/threads/{thread_id}/emails/{email_id}/read"
        ).status_code == 404


# --------------------------------------------------------------------------- #
# The scheduler
#
# Driven with asyncio.run rather than as an async test: the TestClient owns its
# own event loop in a portal thread, and the scheduler shares nothing with it
# but the database file.
# --------------------------------------------------------------------------- #


def run_due(db_path, **kwargs) -> dict:
    return asyncio.run(AutoMatchScheduler(db_path).run_due(**kwargs))


class TestScheduler:
    def test_it_does_nothing_until_an_admin_turns_it_on(
        self, user_client, admin_client, meeting, mock_llm, db_path
    ):
        assert run_due(db_path) == {"enabled": False, "swept": 0, "attached": 0}
        assert thread_of(user_client, meeting["thread_id"])["unread_count"] == 0

    def test_an_enabled_cycle_sweeps_and_attaches(
        self, user_client, admin_client, meeting, mock_llm, db_path
    ):
        configure(admin_client, auto_match_enabled=True)

        result = run_due(db_path)

        assert result == {"enabled": True, "swept": 1, "attached": 2}
        assert thread_of(user_client, meeting["thread_id"])["unread_count"] == 2

    def test_a_thread_is_not_swept_again_until_its_interval_has_passed(
        self, user_client, admin_client, meeting, mock_llm, db_path
    ):
        configure(admin_client, auto_match_enabled=True)
        run_due(db_path)

        assert run_due(db_path)["swept"] == 0

    def test_the_interval_is_configurable(
        self, user_client, admin_client, meeting, mock_llm, db_path
    ):
        from datetime import datetime, timedelta, timezone

        configure(admin_client, auto_match_enabled=True, auto_match_interval_minutes=5)
        run_due(db_path)

        later = datetime.now(timezone.utc) + timedelta(minutes=6)
        assert run_due(db_path, now=later)["swept"] == 1

    def test_archived_threads_are_not_watched(
        self, user_client, admin_client, meeting, mock_llm, db_path
    ):
        configure(admin_client, auto_match_enabled=True)
        user_client.patch(f"/api/threads/{meeting['thread_id']}", json={"archived": True})

        assert run_due(db_path)["swept"] == 0

    def test_unarchiving_puts_a_thread_back_under_watch(
        self, user_client, admin_client, meeting, mock_llm, db_path
    ):
        """Archiving is the off switch for the sweep, so it has to switch back on."""
        configure(admin_client, auto_match_enabled=True)
        thread_id = meeting["thread_id"]
        user_client.patch(f"/api/threads/{thread_id}", json={"archived": True})
        assert run_due(db_path)["swept"] == 0

        user_client.patch(f"/api/threads/{thread_id}", json={"archived": False})
        assert run_due(db_path)["swept"] == 1

    def test_a_thread_nobody_has_touched_in_weeks_is_not_watched(
        self, user_client, admin_client, meeting, mock_llm, db_path
    ):
        configure(admin_client, auto_match_enabled=True)
        with get_conn(db_path) as conn:
            conn.execute(
                "UPDATE threads SET updated_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
                (meeting["thread_id"],),
            )

        assert run_due(db_path)["swept"] == 0

    def test_a_deactivated_users_threads_are_not_watched(
        self, make_user, admin_client, mock_llm, db_path
    ):
        user, as_user = make_user("carol")
        as_user.post(
            "/api/meetings",
            json={"new_thread_title": "Carol's work", "title": "Kickoff"},
        )
        configure(admin_client, auto_match_enabled=True)
        admin_client.delete(f"/api/users/{user['id']}")

        assert run_due(db_path)["swept"] == 0

    def test_the_per_cycle_cap_defers_rather_than_drops(
        self, user_client, admin_client, mock_llm, db_path
    ):
        """Oldest-swept-first, so nothing is starved by the cap."""
        for i in range(3):
            user_client.post("/api/threads", json={"title": f"Thread {i}"})
        configure(
            admin_client, auto_match_enabled=True, auto_match_max_threads_per_cycle=2
        )

        assert run_due(db_path)["swept"] == 2
        assert run_due(db_path)["swept"] == 1
        assert run_due(db_path)["swept"] == 0

    def test_one_broken_thread_does_not_stop_the_cycle(
        self, user_client, admin_client, mock_llm, db_path, monkeypatch
    ):
        user_client.post("/api/threads", json={"title": "First"})
        user_client.post("/api/threads", json={"title": "Second"})
        configure(admin_client, auto_match_enabled=True)

        calls: list[int] = []

        async def explode_once(conn_factory, *, thread_id, user_id, **kwargs):
            calls.append(thread_id)
            if len(calls) == 1:
                raise RuntimeError("provider exploded")
            return {
                "thread_id": thread_id, "skipped": None, "candidates": 0,
                "attached_events": 0, "attached_emails": 0, "error": None,
            }

        monkeypatch.setattr(
            "app.jobs.scheduler.followups_svc.sweep_thread", explode_once
        )
        result = run_due(db_path)

        assert len(calls) == 2, "the second thread was still swept"
        assert result["swept"] == 1
