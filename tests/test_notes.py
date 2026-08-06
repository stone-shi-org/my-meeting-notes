"""Notes: the document type this app authors rather than fetches.

Wired like test_next_step.py -- respx at the transport boundary, env vars
driving LLMConfig.from_db -- because the only external call a note makes is the
one that titles it.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.db import get_conn
from app.services import matching as matching_svc
from app.services import notes as notes_svc

LLM_URL = "https://llm.test/v1/chat/completions"
GENERATED_TITLE = "Cutover rollback window for Oracle billing"

ANSWER = (
    "The rollback window is two hours, agreed with Priya on the 28th.\n\n"
    "- Cut over at 22:00 UTC\n"
    "- Abort by 00:00 UTC if reconciliation fails\n"
)


@pytest.fixture(autouse=True)
def wiring(monkeypatch):
    monkeypatch.setenv("MMN_LLM_BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("MMN_LLM_MODEL", "test/model")
    from app.config import reset_settings_cache

    reset_settings_cache()


@pytest.fixture
def mock_llm():
    """One reply for every completion this module provokes.

    It carries ``next_step`` as well as ``title`` because writing a note makes
    the thread's cached suggestion stale, and the staleness tests refresh it --
    a title-only payload would fail that call for the wrong reason. Each caller
    reads its own key and ignores the rest.
    """
    payload = {"title": GENERATED_TITLE, "next_step": "Confirm the window with Priya."}
    with respx.mock(assert_all_called=False) as router:
        router.post(LLM_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": json.dumps(payload)}}],
                    "usage": {"prompt_tokens": 300, "completion_tokens": 12},
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


def create(client, path: str, **body):
    resp = client.post(path, json={"body": ANSWER, **body})
    assert resp.status_code == 201, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# derive_title -- the fallback, and the only titling that never calls out
# --------------------------------------------------------------------------- #


class TestDeriveTitle:
    def test_uses_the_first_line_with_markdown_stripped(self):
        assert notes_svc.derive_title("## **Rollback** plan\n\nrest") == "Rollback plan"

    def test_skips_leading_blank_lines(self):
        assert notes_svc.derive_title("\n\n\nReal content here") == "Real content here"

    def test_skips_a_bullet_marker(self):
        assert notes_svc.derive_title("- Cut over at 22:00") == "Cut over at 22:00"

    def test_truncates_a_long_first_line(self):
        title = notes_svc.derive_title("x" * 400)
        assert len(title) == notes_svc.TITLE_MAX
        assert title.endswith("…")

    def test_never_returns_empty(self):
        assert notes_svc.derive_title("") == "Untitled note"
        assert notes_svc.derive_title("\n  \n##\n") == "Untitled note"


# --------------------------------------------------------------------------- #
# Creating
# --------------------------------------------------------------------------- #


class TestCreate:
    def test_a_note_without_a_title_is_named_by_the_model(self, user_client, meeting, mock_llm):
        note = create(user_client, f"/api/meetings/{meeting['id']}/notes", source="ai_chat")

        assert note["title"] == GENERATED_TITLE
        assert note["title_model"] == "test/model"
        assert note["source"] == "ai_chat"
        assert note["meeting_id"] == meeting["id"]
        assert note["thread_id"] == meeting["thread_id"]
        assert note["body"] == ANSWER

    def test_a_supplied_title_is_kept_and_costs_no_llm_call(self, user_client, meeting, mock_llm):
        note = create(user_client, f"/api/meetings/{meeting['id']}/notes", title="My own title")

        assert note["title"] == "My own title"
        assert note["title_model"] is None, "nothing generated it"
        assert not mock_llm.calls, "a titled note must not spend an LLM call"

    def test_an_unreachable_model_still_saves_the_note(self, user_client, meeting):
        """The body is the part worth keeping. Losing it because nothing could
        name it would be the worst possible outcome of pressing save."""
        with respx.mock(assert_all_called=False) as router:
            router.post(LLM_URL).mock(return_value=httpx.Response(500, text="llm down"))
            note = create(user_client, f"/api/meetings/{meeting['id']}/notes", source="ai_chat")

        assert note["title"] == "The rollback window is two hours, agreed with Priya on the 28th."
        assert note["title_model"] is None
        assert note["body"] == ANSWER

    def test_an_empty_title_from_the_model_falls_back_too(self, user_client, meeting):
        with respx.mock(assert_all_called=False) as router:
            router.post(LLM_URL).mock(
                return_value=httpx.Response(
                    200, json={"choices": [{"message": {"content": json.dumps({"title": "  "})}}]}
                )
            )
            note = create(user_client, f"/api/meetings/{meeting['id']}/notes")

        assert note["title"].startswith("The rollback window")
        assert note["title_model"] is None

    def test_a_thread_note_has_no_meeting(self, user_client, meeting, mock_llm):
        note = create(user_client, f"/api/threads/{meeting['thread_id']}/notes")
        assert note["meeting_id"] is None

    def test_a_thread_note_can_name_a_meeting_on_that_thread(self, user_client, meeting, mock_llm):
        note = create(
            user_client, f"/api/threads/{meeting['thread_id']}/notes", meeting_id=meeting["id"]
        )
        assert note["meeting_id"] == meeting["id"]

    def test_a_meeting_from_another_thread_is_refused(self, user_client, meeting, mock_llm):
        other = user_client.post(
            "/api/meetings", json={"new_thread_title": "Unrelated", "title": "Standup"}
        ).json()

        resp = user_client.post(
            f"/api/threads/{meeting['thread_id']}/notes",
            json={"body": ANSWER, "meeting_id": other["id"]},
        )
        assert resp.status_code == 404

    def test_an_empty_body_is_rejected(self, user_client, meeting):
        resp = user_client.post(f"/api/meetings/{meeting['id']}/notes", json={"body": ""})
        assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Reading, editing, appending, deleting
# --------------------------------------------------------------------------- #


class TestLifecycle:
    def test_meeting_notes_exclude_the_threads_own(self, user_client, meeting, mock_llm):
        create(user_client, f"/api/meetings/{meeting['id']}/notes", title="On the meeting")
        create(user_client, f"/api/threads/{meeting['thread_id']}/notes", title="On the thread")

        on_meeting = user_client.get(f"/api/meetings/{meeting['id']}/notes").json()
        assert [n["title"] for n in on_meeting] == ["On the meeting"]

        on_thread = user_client.get(f"/api/threads/{meeting['thread_id']}/notes").json()
        assert {n["title"] for n in on_thread} == {"On the meeting", "On the thread"}

    def test_the_thread_list_filters_by_meeting(self, user_client, meeting, mock_llm):
        create(user_client, f"/api/meetings/{meeting['id']}/notes", title="On the meeting")
        create(user_client, f"/api/threads/{meeting['thread_id']}/notes", title="On the thread")

        filtered = user_client.get(
            f"/api/threads/{meeting['thread_id']}/notes", params={"meeting_id": meeting["id"]}
        ).json()
        assert [n["title"] for n in filtered] == ["On the meeting"]

    def test_editing_rewrites_the_note_and_stamps_updated_at(self, user_client, meeting, mock_llm):
        note = create(user_client, f"/api/meetings/{meeting['id']}/notes", title="Before")

        resp = user_client.patch(
            f"/api/threads/{note['thread_id']}/notes/{note['id']}",
            json={"title": "After", "body": "Rewritten."},
        )
        assert resp.status_code == 200, resp.text
        updated = resp.json()
        assert updated["title"] == "After"
        assert updated["body"] == "Rewritten."
        assert updated["updated_at"] >= note["updated_at"]

    def test_appending_joins_with_a_rule_and_leaves_the_title_alone(
        self, user_client, meeting, mock_llm
    ):
        note = create(user_client, f"/api/meetings/{meeting['id']}/notes", title="Kept")

        resp = user_client.post(
            f"/api/threads/{note['thread_id']}/notes/{note['id']}/append",
            json={"body": "A second answer."},
        )
        assert resp.status_code == 200, resp.text
        appended = resp.json()

        assert appended["title"] == "Kept", "the user picked this note by name"
        assert appended["body"] == ANSWER + notes_svc.APPEND_SEPARATOR + "A second answer."

    def test_deleting_removes_it(self, user_client, meeting, mock_llm):
        note = create(user_client, f"/api/meetings/{meeting['id']}/notes", title="Doomed")

        assert (
            user_client.delete(
                f"/api/threads/{note['thread_id']}/notes/{note['id']}"
            ).status_code
            == 200
        )
        assert user_client.get(f"/api/meetings/{meeting['id']}/notes").json() == []
        # And again, on a row that is already gone.
        assert (
            user_client.delete(
                f"/api/threads/{note['thread_id']}/notes/{note['id']}"
            ).status_code
            == 404
        )

    def test_a_note_outlives_the_meeting_it_was_written_on(self, user_client, meeting, mock_llm):
        """ON DELETE SET NULL, same as an attached email or event: the note is
        still the user's, it just no longer hangs off a recording."""
        note = create(user_client, f"/api/meetings/{meeting['id']}/notes", title="Survivor")

        assert user_client.delete(f"/api/meetings/{meeting['id']}").status_code == 200

        remaining = user_client.get(f"/api/threads/{note['thread_id']}/notes").json()
        assert [n["title"] for n in remaining] == ["Survivor"]
        assert remaining[0]["meeting_id"] is None


# --------------------------------------------------------------------------- #
# Moving a note to another thread
# --------------------------------------------------------------------------- #


class TestMove:
    def test_moving_clears_the_meeting_id(self, user_client, meeting, mock_llm):
        note = create(user_client, f"/api/meetings/{meeting['id']}/notes", title="Kickoff notes")
        other = user_client.post(
            "/api/threads", json={"title": "Other thread"}
        ).json()

        resp = user_client.post(
            f"/api/threads/{note['thread_id']}/notes/{note['id']}/move",
            json={"target_thread_id": other["id"]},
        )
        assert resp.status_code == 200, resp.text
        moved = resp.json()
        assert moved["thread_id"] == other["id"]
        assert moved["meeting_id"] is None, "the meeting it was filed on belongs to the old thread"

        assert user_client.get(f"/api/threads/{note['thread_id']}/notes").json() == []
        assert [n["id"] for n in user_client.get(f"/api/threads/{other['id']}/notes").json()] == [
            note["id"]
        ]

    def test_moving_something_not_attached_is_404(self, user_client):
        a = user_client.post("/api/threads", json={"title": "A"}).json()
        b = user_client.post("/api/threads", json={"title": "B"}).json()
        resp = user_client.post(
            f"/api/threads/{a['id']}/notes/999/move", json={"target_thread_id": b["id"]}
        )
        assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Ownership -- someone else's row is 404, never 403
# --------------------------------------------------------------------------- #


class TestOwnership:
    def test_another_user_cannot_list_or_write(
        self, user_client, other_user_client, meeting, mock_llm
    ):
        note = create(user_client, f"/api/meetings/{meeting['id']}/notes", title="Private")
        thread_id = note["thread_id"]

        assert other_user_client.get(f"/api/threads/{thread_id}/notes").status_code == 404
        assert other_user_client.get(f"/api/meetings/{meeting['id']}/notes").status_code == 404
        assert (
            other_user_client.post(
                f"/api/threads/{thread_id}/notes", json={"body": "mine now"}
            ).status_code
            == 404
        )
        assert (
            other_user_client.patch(
                f"/api/threads/{thread_id}/notes/{note['id']}", json={"title": "Hijacked"}
            ).status_code
            == 404
        )
        assert (
            other_user_client.delete(
                f"/api/threads/{thread_id}/notes/{note['id']}"
            ).status_code
            == 404
        )


# --------------------------------------------------------------------------- #
# What a note is plugged into
# --------------------------------------------------------------------------- #


class TestIntegration:
    def test_it_lands_on_the_timeline_and_in_the_count(self, user_client, meeting, mock_llm):
        note = create(user_client, f"/api/meetings/{meeting['id']}/notes", title="On the timeline")

        timeline = user_client.get(f"/api/threads/{note['thread_id']}/timeline").json()
        entry = next(i for i in timeline if i["kind"] == "note")
        assert entry["id"] == note["id"]
        assert entry["at"] == note["created_at"]
        assert entry["payload"]["title"] == "On the timeline"

        thread = user_client.get(f"/api/threads/{note['thread_id']}").json()
        assert thread["note_count"] == 1

    def test_it_makes_a_cached_next_step_stale(self, user_client, meeting, mock_llm):
        thread_id = meeting["thread_id"]
        assert user_client.post(f"/api/threads/{thread_id}/next-step").status_code == 200
        assert user_client.get(f"/api/threads/{thread_id}").json()["next_step_stale"] is False

        create(user_client, f"/api/threads/{thread_id}/notes", title="Something new")

        assert user_client.get(f"/api/threads/{thread_id}").json()["next_step_stale"] is True

    def test_editing_one_makes_it_stale_too(self, user_client, meeting, mock_llm):
        """Emails and events are immutable snapshots keyed on id alone; a note
        is rewritten in place, so the fingerprint has to carry updated_at."""
        thread_id = meeting["thread_id"]
        note = create(user_client, f"/api/threads/{thread_id}/notes", title="Draft")
        assert user_client.post(f"/api/threads/{thread_id}/next-step").status_code == 200
        assert user_client.get(f"/api/threads/{thread_id}").json()["next_step_stale"] is False

        user_client.patch(
            f"/api/threads/{thread_id}/notes/{note['id']}", json={"body": "Completely rewritten."}
        )

        assert user_client.get(f"/api/threads/{thread_id}").json()["next_step_stale"] is True

    def test_it_reaches_the_thread_chat_digest(self, user_client, meeting, mock_llm):
        from app.services import chat as chat_svc

        thread_id = meeting["thread_id"]
        create(user_client, f"/api/threads/{thread_id}/notes", title="Rollback window")

        with get_conn() as conn:
            digest, _ = chat_svc.build_thread_digest(conn, thread_id)

        assert "### Notes" in digest
        assert "Rollback window" in digest
        assert "Cut over at 22:00 UTC" in digest, "notes go in whole, not as a one-line snippet"

    def test_the_digest_says_whose_words_a_note_is(self, user_client, meeting, mock_llm):
        from app.services import chat as chat_svc

        thread_id = meeting["thread_id"]
        create(user_client, f"/api/threads/{thread_id}/notes", title="From a reply", source="ai_chat")
        create(user_client, f"/api/threads/{thread_id}/notes", title="Typed", source="manual")

        with get_conn() as conn:
            digest, _ = chat_svc.build_thread_digest(conn, thread_id)

        assert "saved from an AI answer" in digest
        assert "written by the user" in digest

    def test_it_does_not_reach_the_summarizer(self, user_client, meeting, mock_llm):
        """An AI-written note feeding the next summary of the meeting it was
        written from would put the model's own prose back into its input.
        `attached_context` is deliberately blind to this table."""
        create(user_client, f"/api/meetings/{meeting['id']}/notes", title="Not summarizer input")

        with get_conn() as conn:
            context = matching_svc.attached_context(conn, meeting["id"])

        assert set(context) == {"events", "emails"}
