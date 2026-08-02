"""The cached "next step" suggestion: generation, caching, and staleness.

Mirrors test_followups.py's wiring for an LLM-backed endpoint -- respx at the
transport boundary, env vars driving LLMConfig.from_db -- but there is no
provider to fake here, just one chat completion.
"""

from __future__ import annotations

import json

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
