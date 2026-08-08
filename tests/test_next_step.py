"""The cached "next step" suggestion: generation, caching, and staleness.

Mirrors test_followups.py's wiring for an LLM-backed endpoint -- respx at the
transport boundary, env vars driving LLMConfig.from_db -- but there is no
provider to fake here, just one chat completion.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from app.db import get_conn
from app.services import matching as matching_svc
from app.services import threads as threads_svc

LLM_URL = "https://llm.test/v1/chat/completions"
NEXT_STEP = "Send the cutover recap to Priya before Thursday's review."


@pytest.fixture(autouse=True)
def wiring(monkeypatch):
    monkeypatch.setenv("MMN_LLM_BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("MMN_LLM_MODEL", "test/model")
    from app.config import reset_settings_cache

    reset_settings_cache()


@pytest.fixture
def mock_llm():
    with respx.mock(assert_all_called=False) as router:
        router.post(LLM_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": json.dumps({"next_step": NEXT_STEP})}}],
                    "usage": {"prompt_tokens": 120, "completion_tokens": 20},
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


def thread_of(client, thread_id: int) -> dict:
    return client.get(f"/api/threads/{thread_id}").json()


def refresh(client, thread_id: int) -> dict:
    resp = client.post(f"/api/threads/{thread_id}/next-step")
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestRefresh:
    def test_a_fresh_thread_has_no_suggestion_and_is_stale(self, user_client, meeting):
        thread = thread_of(user_client, meeting["thread_id"])
        assert thread["next_step"] is None
        assert thread["next_step_generated_at"] is None
        assert thread["next_step_stale"] is True

    def test_refresh_generates_and_caches_a_suggestion(self, user_client, meeting, mock_llm):
        body = refresh(user_client, meeting["thread_id"])
        assert body["next_step"] == NEXT_STEP
        assert body["next_step_stale"] is False
        assert body["error"] is None

        thread = thread_of(user_client, meeting["thread_id"])
        assert thread["next_step"] == NEXT_STEP
        assert thread["next_step_stale"] is False
        assert thread["next_step_generated_at"]

    def test_llm_failure_leaves_the_previous_suggestion_in_place(
        self, user_client, meeting, mock_llm
    ):
        thread_id = meeting["thread_id"]
        refresh(user_client, thread_id)

        mock_llm.post(LLM_URL).mock(return_value=httpx.Response(500, text="llm down"))
        body = refresh(user_client, thread_id)
        assert body["error"]
        assert body["next_step"] is None

        thread = thread_of(user_client, thread_id)
        assert thread["next_step"] == NEXT_STEP, "a failed regen must not lose what was cached"

    def test_a_second_user_cannot_refresh_someone_elses_thread(
        self, user_client, other_user_client, meeting
    ):
        resp = other_user_client.post(f"/api/threads/{meeting['thread_id']}/next-step")
        assert resp.status_code == 404


class TestStaleness:
    """Staleness is derived from a fingerprint, not stamped at attach time --
    see threads_svc.compute_next_step_fingerprint. These exercise that it
    actually reacts to new meetings/emails/events without any extra wiring."""

    def test_attaching_a_new_email_makes_a_cached_suggestion_stale(
        self, user_client, meeting, mock_llm
    ):
        thread_id = meeting["thread_id"]
        refresh(user_client, thread_id)
        assert thread_of(user_client, thread_id)["next_step_stale"] is False

        with get_conn() as conn:
            matching_svc.attach_email(
                conn,
                thread_id=thread_id,
                meeting_id=None,
                email={
                    "id": "g9",
                    "message_id": "<new@x>",
                    "subject": "New info",
                    "sender": "a@b.com",
                    "date": "2026-07-29T00:00:00+00:00",
                    "snippet": "",
                    "account": "work@x",
                },
                user_id=meeting["owner_id"],
                auto=True,
            )

        assert thread_of(user_client, thread_id)["next_step_stale"] is True

    def test_a_second_meeting_makes_a_cached_suggestion_stale(self, user_client, meeting, mock_llm):
        thread_id = meeting["thread_id"]
        refresh(user_client, thread_id)
        assert thread_of(user_client, thread_id)["next_step_stale"] is False

        user_client.post(
            "/api/meetings",
            json={"thread_id": thread_id, "title": "Follow-up sync"},
        )

        assert thread_of(user_client, thread_id)["next_step_stale"] is True

    def test_fingerprint_is_stable_when_nothing_changed(self, conn):
        from app.db import utcnow

        conn.execute(
            "INSERT INTO users (id, username, password_hash, password_salt, created_at, updated_at) "
            "VALUES (1, 'one', 'h', 's', ?, ?)",
            (utcnow(), utcnow()),
        )
        thread = threads_svc.create_thread(conn, owner_id=1, title="Atlas Migration")

        first = threads_svc.compute_next_step_fingerprint(conn, thread["id"])
        second = threads_svc.compute_next_step_fingerprint(conn, thread["id"])
        assert first == second

    def test_no_stored_fingerprint_is_always_stale(self, conn):
        assert threads_svc.is_next_step_stale(conn, 999, None) is True


class TestListAutoRefresh:
    """GET /api/threads doubles as "generate what's missing" for the page it
    returns -- see threads_svc.next_step_needs_refresh and
    next_step_svc.refresh_many."""

    def test_list_generates_a_missing_next_step(self, user_client, meeting, mock_llm):
        thread_id = meeting["thread_id"]
        assert thread_of(user_client, thread_id)["next_step"] is None

        items = user_client.get("/api/threads").json()["items"]
        listed = next(t for t in items if t["id"] == thread_id)
        assert listed["next_step"] == NEXT_STEP
        assert listed["next_step_generated_at"]
        assert len(mock_llm.calls) == 1

        # Cached now -- a second page load must not spend another LLM call.
        user_client.get("/api/threads")
        assert len(mock_llm.calls) == 1

    def test_a_fresh_cached_suggestion_is_left_alone(self, user_client, meeting, mock_llm):
        thread_id = meeting["thread_id"]
        refresh(user_client, thread_id)
        assert len(mock_llm.calls) == 1

        user_client.get("/api/threads")
        assert len(mock_llm.calls) == 1, "a recent, unchanged suggestion must not regenerate"

    def test_new_content_after_success_bypasses_the_failure_cooldown(
        self, user_client, meeting, mock_llm
    ):
        thread_id = meeting["thread_id"]
        refresh(user_client, thread_id)
        assert len(mock_llm.calls) == 1

        user_client.post(
            "/api/meetings",
            json={"thread_id": thread_id, "title": "Immediate follow-up"},
        )
        user_client.get("/api/threads")
        assert len(mock_llm.calls) == 2, "known-stale content must refresh immediately"

    def test_an_old_suggestion_is_regenerated_even_if_nothing_changed(
        self, user_client, meeting, mock_llm
    ):
        thread_id = meeting["thread_id"]
        refresh(user_client, thread_id)
        assert len(mock_llm.calls) == 1

        old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        with get_conn() as conn:
            conn.execute(
                "UPDATE threads SET next_step_generated_at = ?, next_step_checked_at = ? "
                "WHERE id = ?",
                (old, old, thread_id),
            )

        user_client.get("/api/threads")
        assert len(mock_llm.calls) == 2

    def test_a_failed_attempt_is_not_retried_within_the_cooldown(
        self, user_client, meeting, mock_llm
    ):
        thread_id = meeting["thread_id"]
        mock_llm.post(LLM_URL).mock(return_value=httpx.Response(500, text="llm down"))

        user_client.get("/api/threads")
        assert len(mock_llm.calls) == 1
        assert thread_of(user_client, thread_id)["next_step"] is None

        # Still within the cooldown window -- must back off, not retry.
        user_client.get("/api/threads")
        assert len(mock_llm.calls) == 1

    def test_a_second_users_thread_is_unaffected(
        self, user_client, other_user_client, meeting, mock_llm
    ):
        other_user_client.post("/api/threads", json={"title": "Someone else's thread"})

        user_client.get("/api/threads")
        assert len(mock_llm.calls) == 1  # only this user's own thread was generated


@pytest.mark.asyncio
async def test_list_refresh_concurrency_is_shared_across_requests(monkeypatch):
    from app.services import next_step as next_step_svc

    active = 0
    maximum = 0
    lock = threading.Lock()

    def fake_generate(_db_path, thread_id):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return {"next_step": str(thread_id), "error": None}

    monkeypatch.setattr(next_step_svc, "generate_sync", fake_generate)
    await asyncio.gather(
        next_step_svc.refresh_many(None, list(range(10))),
        next_step_svc.refresh_many(None, list(range(10, 20))),
    )
    assert maximum == next_step_svc.LIST_REFRESH_CONCURRENCY
