"""Summary generation, versioning and action items."""

from __future__ import annotations

import json
import time

import httpx
import pytest
import respx

from app.db import get_conn
from tests.conftest import FIXTURES

LLM_URL = "https://llm.test/v1/chat/completions"

GOOD_SUMMARY = {
    "title_suggestion": "Recruiter screen with Donna",
    "tldr": "Donna screened Stan for the role and agreed to send details.",
    "summary_md": "### Context\n- Initial recruiter screen\n- Salary discussed",
    "topics": ["recruiting", "compensation"],
    "key_decisions": [
        {"decision": "Proceed to a technical round", "context": "Fit looked good",
         "made_by": "Donna"}
    ],
    "action_items": [
        {"text": "Send the role description", "owner": "Donna",
         "owner_speaker": "SPEAKER_01", "due_text": "today", "due_date": "2026-03-18",
         "priority": "high", "confidence": 0.9},
        {"text": "Reply with availability", "owner": "Stan",
         "owner_speaker": "SPEAKER_00", "due_text": "", "due_date": "",
         "priority": "medium", "confidence": 0.7},
    ],
    "open_questions": ["What is the salary band?"],
    "participants": [
        {"speaker": "SPEAKER_00", "inferred_name": "Stan",
         "evidence": "this is Stan speaking"},
        {"speaker": "SPEAKER_01", "inferred_name": "Donna",
         "evidence": "This is Donna from Reddit"},
    ],
}


def completion(payload) -> dict:
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 300},
    }


@pytest.fixture(autouse=True)
def llm_settings(monkeypatch):
    monkeypatch.setenv("MMN_DIARIZE_FAKE", "true")
    monkeypatch.setenv("MMN_DIARIZE_FAKE_DELAY_SEC", "0.05")
    monkeypatch.setenv("MMN_JOB_CONCURRENCY", "1")
    monkeypatch.setenv("MMN_LLM_BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("MMN_LLM_MODEL", "test/model")
    monkeypatch.setenv("MMN_LLM_API_KEY", "sk-test")
    from app.config import reset_settings_cache

    reset_settings_cache()


@pytest.fixture
def mock_llm():
    with respx.mock(assert_all_called=False) as router:
        router.post(LLM_URL).mock(
            return_value=httpx.Response(200, json=completion(GOOD_SUMMARY))
        )
        yield router


@pytest.fixture
def meeting(user_client, mock_llm):
    """Uploaded, diarized and summarized."""
    with (FIXTURES / "tiny16k.wav").open("rb") as fh:
        body = user_client.post(
            "/api/meetings/upload",
            data={"title": "Recruiter call", "new_thread_title": "Job Search"},
            files={"file": ("tiny16k.wav", fh, "audio/wav")},
        ).json()

    deadline = time.time() + 20
    while time.time() < deadline:
        job = user_client.get(f"/api/jobs/{body['job_id']}").json()
        if job["status"] in {"succeeded", "failed"}:
            assert job["status"] == "succeeded", job.get("error")
            break
        time.sleep(0.1)
    return body


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #


def test_ingest_produces_a_summary_automatically(user_client, meeting):
    summary = user_client.get(f"/api/meetings/{meeting['meeting_id']}/summary").json()

    assert summary["version"] == 1
    assert summary["is_current"] is True
    assert summary["status"] == "ok"
    assert summary["tldr"] == GOOD_SUMMARY["tldr"]
    assert summary["topics"] == GOOD_SUMMARY["topics"]
    assert len(summary["key_decisions"]) == 1


def test_provenance_is_recorded(user_client, meeting):
    """Which model and which prompt produced this text."""
    summary = user_client.get(f"/api/meetings/{meeting['meeting_id']}/summary").json()

    assert summary["model"] == "test/model"
    assert summary["llm_base_url"] == "https://llm.test/v1"
    assert summary["prompt_name"] == "summary_prompt"
    assert summary["prompt_version"] == "1"
    assert len(summary["prompt_sha256"]) == 64
    assert summary["prompt_tokens"] == 1200
    assert summary["completion_tokens"] == 300


def test_the_full_prompt_text_is_snapshotted(user_client, meeting, isolated_settings):
    """So editing the file later cannot orphan this version's history."""
    with get_conn(isolated_settings.db_path) as conn:
        row = conn.execute(
            "SELECT prompt_text FROM summaries WHERE meeting_id = ?",
            (meeting["meeting_id"],),
        ).fetchone()

    assert "{{transcript}}" in row["prompt_text"]
    assert "## SYSTEM" in row["prompt_text"]


def test_action_items_are_stored_in_order(user_client, meeting):
    items = user_client.get(
        f"/api/meetings/{meeting['meeting_id']}/action-items"
    ).json()

    assert len(items) == 2
    assert items[0]["text"] == "Send the role description"
    assert items[0]["owner_label"] == "Donna"
    assert items[0]["owner_speaker_id"] == "SPEAKER_01"
    assert items[0]["due_date"] == "2026-03-18"
    assert items[0]["priority"] == "high"
    assert items[0]["status"] == "open"


def test_a_hallucinated_owner_speaker_is_blanked(user_client, mock_llm):
    """Rather than storing a dangling id no colour or filter can resolve."""
    payload = json.loads(json.dumps(GOOD_SUMMARY))
    payload["action_items"][0]["owner_speaker"] = "SPEAKER_99"
    mock_llm.post(LLM_URL).mock(
        return_value=httpx.Response(200, json=completion(payload))
    )

    with (FIXTURES / "tiny16k.wav").open("rb") as fh:
        body = user_client.post(
            "/api/meetings/upload",
            data={"title": "T", "new_thread_title": "X"},
            files={"file": ("tiny16k.wav", fh, "audio/wav")},
        ).json()

    deadline = time.time() + 20
    while time.time() < deadline:
        if user_client.get(f"/api/jobs/{body['job_id']}").json()["status"] != "running":
            break
        time.sleep(0.1)

    items = user_client.get(f"/api/meetings/{body['meeting_id']}/action-items").json()
    assert items[0]["owner_speaker_id"] is None
    assert items[0]["owner_label"] == "Donna"  # the human-readable name survives


def test_inferred_names_become_greyed_suggestions(user_client, meeting, isolated_settings):
    with get_conn(isolated_settings.db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM speaker_map WHERE meeting_id = ? AND source = 'llm_suggested'",
            (meeting["meeting_id"],),
        ).fetchall()

    names = {r["speaker_id"]: r["display_name"] for r in rows}
    assert names == {"SPEAKER_00": "Stan", "SPEAKER_01": "Donna"}


def test_a_user_name_is_not_overwritten_by_a_later_suggestion(
    user_client, meeting, isolated_settings, mock_llm
):
    mid = meeting["meeting_id"]
    user_client.put(
        f"/api/meetings/{mid}/speakers",
        json=[{"speaker_id": "SPEAKER_00", "display_name": "Stanley Shi"}],
    )

    resp = user_client.post(f"/api/meetings/{mid}/summary/regenerate", json={})
    _wait(user_client, resp.json()["job_id"])

    with get_conn(isolated_settings.db_path) as conn:
        row = conn.execute(
            "SELECT * FROM speaker_map WHERE meeting_id = ? AND speaker_id = 'SPEAKER_00'",
            (mid,),
        ).fetchone()

    assert row["display_name"] == "Stanley Shi"
    assert row["source"] == "user"


# --------------------------------------------------------------------------- #
# Versioning
# --------------------------------------------------------------------------- #


def _wait(client, job_id, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.1)
    raise AssertionError(f"job did not finish: {job}")


def test_regenerate_creates_v2_and_leaves_v1_intact(user_client, meeting, mock_llm):
    mid = meeting["meeting_id"]

    second = json.loads(json.dumps(GOOD_SUMMARY))
    second["tldr"] = "A different second-pass summary."
    mock_llm.post(LLM_URL).mock(
        return_value=httpx.Response(200, json=completion(second))
    )

    resp = user_client.post(f"/api/meetings/{mid}/summary/regenerate", json={})
    assert resp.status_code == 202
    job = _wait(user_client, resp.json()["job_id"])
    assert job["status"] == "succeeded", job.get("error")

    current = user_client.get(f"/api/meetings/{mid}/summary").json()
    assert current["version"] == 2
    assert current["tldr"] == "A different second-pass summary."

    v1 = user_client.get(f"/api/meetings/{mid}/summaries/1").json()
    assert v1["version"] == 1
    assert v1["is_current"] is False
    assert v1["tldr"] == GOOD_SUMMARY["tldr"]


def test_version_history_lists_both(user_client, meeting, mock_llm):
    mid = meeting["meeting_id"]
    _wait(
        user_client,
        user_client.post(f"/api/meetings/{mid}/summary/regenerate", json={}).json()["job_id"],
    )

    history = user_client.get(f"/api/meetings/{mid}/summaries").json()
    assert [h["version"] for h in history] == [2, 1]
    assert sum(h["is_current"] for h in history) == 1


def test_an_older_version_can_be_reactivated(user_client, meeting, mock_llm):
    mid = meeting["meeting_id"]
    _wait(
        user_client,
        user_client.post(f"/api/meetings/{mid}/summary/regenerate", json={}).json()["job_id"],
    )

    assert user_client.post(f"/api/meetings/{mid}/summaries/1/activate").status_code == 200
    assert user_client.get(f"/api/meetings/{mid}/summary").json()["version"] == 1


def test_regenerate_with_a_prompt_override(user_client, meeting, mock_llm):
    mid = meeting["meeting_id"]
    override = "---\nversion: x\n---\n## SYSTEM\nBe terse.\n## USER\n{{transcript}}"

    job = _wait(
        user_client,
        user_client.post(
            f"/api/meetings/{mid}/summary/regenerate", json={"prompt_override": override}
        ).json()["job_id"],
    )
    assert job["status"] == "succeeded", job.get("error")

    v2 = user_client.get(f"/api/meetings/{mid}/summaries/2").json()
    assert v2["prompt_version"] == "override"
    assert "Be terse." in v2["prompt_text"]


def test_regenerate_with_a_different_model(user_client, meeting, mock_llm):
    mid = meeting["meeting_id"]
    _wait(
        user_client,
        user_client.post(
            f"/api/meetings/{mid}/summary/regenerate", json={"model": "other/model"}
        ).json()["job_id"],
    )
    assert user_client.get(f"/api/meetings/{mid}/summary").json()["model"] == "other/model"


def test_regenerate_needs_a_transcript(user_client):
    m = user_client.post(
        "/api/meetings", json={"new_thread_title": "T", "title": "No audio"}
    ).json()
    resp = user_client.post(f"/api/meetings/{m['id']}/summary/regenerate", json={})
    assert resp.status_code == 400


def test_action_items_of_an_old_version_survive_with_it(user_client, meeting, mock_llm):
    mid = meeting["meeting_id"]
    _wait(
        user_client,
        user_client.post(f"/api/meetings/{mid}/summary/regenerate", json={}).json()["job_id"],
    )

    current = user_client.get(f"/api/meetings/{mid}/action-items").json()
    every = user_client.get(
        f"/api/meetings/{mid}/action-items", params={"all_versions": True}
    ).json()

    assert len(current) == 2
    assert len(every) == 4  # both versions' items are retained


# --------------------------------------------------------------------------- #
# Staleness
# --------------------------------------------------------------------------- #


def test_a_summary_is_not_stale_when_freshly_generated(user_client, meeting):
    summary = user_client.get(f"/api/meetings/{meeting['meeting_id']}/summary").json()
    assert summary["stale"] is False


def test_renaming_a_speaker_marks_the_summary_stale(user_client, meeting):
    """The transcript the LLM saw no longer matches the one on screen."""
    mid = meeting["meeting_id"]
    # Not "Stan": that is what the model itself inferred, so it would be a no-op.
    user_client.put(
        f"/api/meetings/{mid}/speakers",
        json=[{"speaker_id": "SPEAKER_00", "display_name": "Stanley Shi"}],
    )
    assert user_client.get(f"/api/meetings/{mid}/summary").json()["stale"] is True


def test_renaming_to_the_name_the_model_already_inferred_is_not_stale(
    user_client, meeting
):
    """Accepting a suggestion verbatim changes nothing the LLM would see."""
    mid = meeting["meeting_id"]
    user_client.put(
        f"/api/meetings/{mid}/speakers",
        json=[{"speaker_id": "SPEAKER_00", "display_name": "Stan"}],
    )
    assert user_client.get(f"/api/meetings/{mid}/summary").json()["stale"] is False


# --------------------------------------------------------------------------- #
# Failure handling
# --------------------------------------------------------------------------- #


def test_a_failed_summary_does_not_throw_away_the_transcript(user_client, mock_llm):
    """Diarization costs minutes; a bad LLM reply must not discard it."""
    mock_llm.post(LLM_URL).mock(return_value=httpx.Response(500, text="model down"))

    with (FIXTURES / "tiny16k.wav").open("rb") as fh:
        body = user_client.post(
            "/api/meetings/upload",
            data={"title": "T", "new_thread_title": "X"},
            files={"file": ("tiny16k.wav", fh, "audio/wav")},
        ).json()

    job = _wait(user_client, body["job_id"])

    assert job["status"] == "succeeded"
    meeting = user_client.get(f"/api/meetings/{body['meeting_id']}").json()
    assert meeting["has_transcript"] is True
    assert meeting["has_summary"] is False

    events = user_client.get(f"/api/jobs/{body['job_id']}/events").json()["events"]
    assert any(e["level"] == "error" and "Summary failed" in e["message"] for e in events)


def test_auto_summarize_can_be_switched_off(user_client, mock_llm):
    with (FIXTURES / "tiny16k.wav").open("rb") as fh:
        body = user_client.post(
            "/api/meetings/upload",
            data={"title": "T", "new_thread_title": "X", "auto_summarize": "false"},
            files={"file": ("tiny16k.wav", fh, "audio/wav")},
        ).json()

    _wait(user_client, body["job_id"])
    assert user_client.get(f"/api/meetings/{body['meeting_id']}/summary").status_code == 404


# --------------------------------------------------------------------------- #
# Action item editing
# --------------------------------------------------------------------------- #


def test_ticking_an_action_item_off(user_client, meeting):
    items = user_client.get(f"/api/meetings/{meeting['meeting_id']}/action-items").json()

    resp = user_client.patch(f"/api/action-items/{items[0]['id']}", json={"status": "done"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "done"
    assert resp.json()["done_at"] is not None


def test_reopening_clears_the_completion_time(user_client, meeting):
    items = user_client.get(f"/api/meetings/{meeting['meeting_id']}/action-items").json()
    user_client.patch(f"/api/action-items/{items[0]['id']}", json={"status": "done"})

    reopened = user_client.patch(
        f"/api/action-items/{items[0]['id']}", json={"status": "open"}
    ).json()
    assert reopened["done_at"] is None


def test_editing_the_text_and_owner(user_client, meeting):
    items = user_client.get(f"/api/meetings/{meeting['meeting_id']}/action-items").json()
    resp = user_client.patch(
        f"/api/action-items/{items[0]['id']}",
        json={"text": "Corrected wording", "owner_label": "Someone Else"},
    )
    assert resp.json()["text"] == "Corrected wording"
    assert resp.json()["owner_label"] == "Someone Else"


def test_an_invalid_status_is_rejected(user_client, meeting):
    items = user_client.get(f"/api/meetings/{meeting['meeting_id']}/action-items").json()
    resp = user_client.patch(f"/api/action-items/{items[0]['id']}", json={"status": "maybe"})
    assert resp.status_code == 422


def test_another_user_cannot_touch_the_summary_or_items(
    user_client, other_user_client, meeting
):
    mid = meeting["meeting_id"]
    assert other_user_client.get(f"/api/meetings/{mid}/summary").status_code == 404
    assert other_user_client.get(f"/api/meetings/{mid}/summaries").status_code == 404

    items = user_client.get(f"/api/meetings/{mid}/action-items").json()
    resp = other_user_client.patch(
        f"/api/action-items/{items[0]['id']}", json={"status": "done"}
    )
    assert resp.status_code == 404
