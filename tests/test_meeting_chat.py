"""Meeting chat: transcript digest, history, ownership."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.db import get_conn, utcnow
from app.services import meeting_chat as meeting_chat_svc

LLM_URL = "https://llm.test/v1/chat/completions"


def stream_body(pieces: list[str], *, usage: dict | None = None) -> bytes:
    """An SSE body matching the real omniroute chunk shape observed live:
    a role-announcement chunk, one or more content deltas, a terminal chunk
    carrying `finish_reason` + `usage`, then the literal `data: [DONE]`.
    """
    frames = [{"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}]
    frames += [{"choices": [{"index": 0, "delta": {"content": p}, "finish_reason": None}]} for p in pieces]
    final = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    if usage:
        final["usage"] = usage
    frames.append(final)

    body = "".join(f"data: {json.dumps(f)}\n\n" for f in frames)
    body += "data: [DONE]\n\n"
    return body.encode()


def stream_response(pieces: list[str], *, usage: dict | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        content=stream_body(pieces, usage=usage or {"prompt_tokens": 100, "completion_tokens": 20}),
        headers={"content-type": "text/event-stream"},
    )


def parse_sse_frames(text: str) -> list[tuple[str, dict]]:
    frames = []
    for block in text.strip("\n").split("\n\n"):
        if not block or block.startswith(":"):
            continue
        event, data = "message", None
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = line[len("data:"):].strip()
        if data is not None:
            frames.append((event, json.loads(data)))
    return frames


@pytest.fixture(autouse=True)
def llm_settings(monkeypatch):
    monkeypatch.setenv("MMN_LLM_BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("MMN_LLM_MODEL", "test/model")
    monkeypatch.setenv("MMN_LLM_API_KEY", "sk-test")
    from app.config import reset_settings_cache

    reset_settings_cache()


def _diarization_payload(*texts: str) -> dict:
    return {
        "task": "transcribe",
        "duration": 5.0,
        "num_speakers": 1,
        "speakers": [
            {"id": "SPEAKER_00", "label": "Speaker 1",
             "total_speech_duration": 5.0, "segment_count": len(texts)}
        ],
        "segments": [
            {"id": i, "speaker": "SPEAKER_00", "label": "Speaker 1",
             "start": float(i), "end": float(i + 1), "text": text}
            for i, text in enumerate(texts)
        ],
    }


# --------------------------------------------------------------------------- #
# Unit: digest building
# --------------------------------------------------------------------------- #


@pytest.fixture
def seeded(conn):
    now = utcnow()
    conn.execute(
        "INSERT INTO users (id, username, password_hash, password_salt, created_at, updated_at) "
        "VALUES (1, 'u', 'h', 's', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO threads (id, owner_id, title, created_at, updated_at) "
        "VALUES (1, 1, 'Q3 planning', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO meetings (id, thread_id, owner_id, title, meeting_at, created_at, updated_at) "
        "VALUES (1, 1, 1, 'Budget review', '2026-07-15T10:00:00Z', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO diarizations (id, meeting_id, provider_url, model, raw_json, created_at) "
        "VALUES (1, 1, 'http://x', 'm', ?, ?)",
        (
            json.dumps(_diarization_payload(
                "Let's kick off the budget review.",
                "The exact figure is forty two thousand dollars.",
            )),
            now,
        ),
    )
    conn.execute("UPDATE meetings SET active_diarization_id = 1 WHERE id = 1")
    return conn


def test_digest_includes_full_transcript_text(seeded):
    digest, truncated = meeting_chat_svc.build_meeting_digest(seeded, meeting_id=1)

    assert not truncated
    assert "Budget review" in digest
    assert "Let's kick off the budget review." in digest
    assert "forty two thousand dollars" in digest


def test_digest_truncates_when_over_budget(seeded, monkeypatch):
    monkeypatch.setenv("MMN_SUMMARY_MAX_INPUT_TOKENS", "5")
    from app.config import reset_settings_cache

    reset_settings_cache()

    digest, truncated = meeting_chat_svc.build_meeting_digest(seeded, meeting_id=1)

    assert truncated
    assert "not shown due to the context limit" in digest
    # The budget is tiny, but the first line always makes it in.
    assert "Let's kick off the budget review." in digest


# --------------------------------------------------------------------------- #
# HTTP: round trip, history, ownership
# --------------------------------------------------------------------------- #


def _seed_via_api(client, isolated_settings):
    thread = client.post("/api/threads", json={"title": "Q3 planning"}).json()
    meeting = client.post(
        "/api/meetings",
        json={"thread_id": thread["id"], "title": "Budget review",
              "meeting_at": "2026-07-15T10:00:00Z"},
    ).json()

    now = utcnow()
    with get_conn(isolated_settings.db_path) as conn:
        conn.execute(
            "INSERT INTO diarizations (id, meeting_id, provider_url, model, raw_json, created_at) "
            "VALUES (1, ?, 'http://x', 'm', ?, ?)",
            (meeting["id"], json.dumps(_diarization_payload(
                "The budget is forty two thousand dollars.",
            )), now),
        )
        conn.execute(
            "UPDATE meetings SET active_diarization_id = 1 WHERE id = ?", (meeting["id"],)
        )
    return meeting["id"]


@respx.mock
def test_reply_uses_transcript_as_context(user_client, isolated_settings):
    meeting_id = _seed_via_api(user_client, isolated_settings)
    route = respx.post(LLM_URL).mock(
        return_value=stream_response(["The exact figure is forty two thousand dollars."])
    )

    resp = user_client.post(
        f"/api/meetings/{meeting_id}/chat",
        json={"message": "What was the exact figure mentioned?"},
    )
    assert resp.status_code == 200, resp.text
    # 1 for the answer, 1 for the follow-up-suggestions call that fires after
    # it -- this route always streams, so that second call fails harmlessly
    # (stream=false expected) and just means no suggestions this round.
    assert route.call_count == 2

    request_body = json.loads(route.calls[0].request.content)
    system_message = request_body["messages"][0]["content"]
    assert "forty two thousand dollars" in system_message

    frames = parse_sse_frames(resp.text)
    done_data = next(d for e, d in frames if e == "done")
    assert done_data["role"] == "assistant"
    assert done_data["meeting_id"] == meeting_id
    assert "forty two thousand dollars" in done_data["content"]


@respx.mock
def test_history_round_trips(user_client, isolated_settings):
    meeting_id = _seed_via_api(user_client, isolated_settings)
    respx.post(LLM_URL).mock(return_value=stream_response(["Sure, here's an answer."]))

    user_client.post(f"/api/meetings/{meeting_id}/chat", json={"message": "First question"})
    user_client.post(f"/api/meetings/{meeting_id}/chat", json={"message": "Second question"})

    history = user_client.get(f"/api/meetings/{meeting_id}/chat").json()
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]
    assert history[0]["content"] == "First question"
    assert history[2]["content"] == "Second question"
    assert history[1]["prompt_tokens"] == 100
    assert history[1]["completion_tokens"] == 20


@respx.mock
def test_clear_removes_all_messages(user_client, isolated_settings):
    meeting_id = _seed_via_api(user_client, isolated_settings)
    respx.post(LLM_URL).mock(return_value=stream_response(["Sure, here's an answer."]))
    user_client.post(f"/api/meetings/{meeting_id}/chat", json={"message": "First question"})

    resp = user_client.delete(f"/api/meetings/{meeting_id}/chat")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "removed": 2}

    assert user_client.get(f"/api/meetings/{meeting_id}/chat").json() == []


@respx.mock
def test_other_users_meeting_404s(user_client, other_user_client, isolated_settings):
    meeting_id = _seed_via_api(user_client, isolated_settings)
    respx.post(LLM_URL).mock(return_value=stream_response(["answer"]))

    resp = other_user_client.post(
        f"/api/meetings/{meeting_id}/chat", json={"message": "peeking"}
    )
    assert resp.status_code == 404

    resp = other_user_client.get(f"/api/meetings/{meeting_id}/chat")
    assert resp.status_code == 404


@respx.mock
def test_llm_failure_streams_an_error_frame_and_persists_nothing(user_client, isolated_settings):
    meeting_id = _seed_via_api(user_client, isolated_settings)
    respx.post(LLM_URL).mock(return_value=httpx.Response(401, json={"error": "bad key"}))

    resp = user_client.post(f"/api/meetings/{meeting_id}/chat", json={"message": "hello?"})
    assert resp.status_code == 200, resp.text

    frames = parse_sse_frames(resp.text)
    events = [e for e, _ in frames]
    assert "error" in events
    assert "done" not in events
    error_data = next(d for e, d in frames if e == "error")
    assert error_data["code"] == "LLM_AUTH_FAILED"

    history = user_client.get(f"/api/meetings/{meeting_id}/chat").json()
    assert history == []


# --------------------------------------------------------------------------- #
# Follow-up suggestions
# --------------------------------------------------------------------------- #


@respx.mock
def test_a_completed_answer_gets_a_suggestions_event(user_client, isolated_settings):
    meeting_id = _seed_via_api(user_client, isolated_settings)
    respx.post(LLM_URL).mock(
        side_effect=[
            stream_response(["Sure, here's an answer."]),
            httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": json.dumps({"suggestions": ["A", "B", "C"]})}}
                    ],
                    "usage": {"prompt_tokens": 40, "completion_tokens": 10},
                },
            ),
        ]
    )

    resp = user_client.post(f"/api/meetings/{meeting_id}/chat", json={"message": "hi"})
    assert resp.status_code == 200, resp.text

    frames = parse_sse_frames(resp.text)
    events = [e for e, _ in frames]
    assert events.index("suggestions") > events.index("done")
    suggestions_data = next(d for e, d in frames if e == "suggestions")
    assert suggestions_data["suggestions"] == ["A", "B", "C"]


@respx.mock
def test_a_failed_suggestions_call_does_not_break_the_answer(user_client, isolated_settings):
    meeting_id = _seed_via_api(user_client, isolated_settings)
    respx.post(LLM_URL).mock(
        side_effect=[
            stream_response(["Sure, here's an answer."]),
            httpx.Response(500, json={"error": "boom"}),
        ]
    )

    resp = user_client.post(f"/api/meetings/{meeting_id}/chat", json={"message": "hi"})
    assert resp.status_code == 200, resp.text

    events = [e for e, _ in parse_sse_frames(resp.text)]
    assert "done" in events
    assert "suggestions" not in events


# --------------------------------------------------------------------------- #
# Model selection
# --------------------------------------------------------------------------- #


@respx.mock
def test_an_enabled_model_is_used_for_the_turn(user_client, admin_client, isolated_settings):
    meeting_id = _seed_via_api(user_client, isolated_settings)
    admin_client.put("/api/settings", json={"values": {"llm_chat_models": ["extra/model"]}})
    route = respx.post(LLM_URL).mock(return_value=stream_response(["answer"]))

    resp = user_client.post(
        f"/api/meetings/{meeting_id}/chat",
        json={"message": "hi", "model": "extra/model"},
    )
    assert resp.status_code == 200, resp.text
    assert json.loads(route.calls[0].request.content)["model"] == "extra/model"

    done = next(d for e, d in parse_sse_frames(resp.text) if e == "done")
    assert done["model"] == "extra/model"


@respx.mock
def test_a_model_outside_the_enabled_set_is_rejected(user_client, isolated_settings):
    meeting_id = _seed_via_api(user_client, isolated_settings)
    route = respx.post(LLM_URL).mock(return_value=stream_response(["answer"]))

    resp = user_client.post(
        f"/api/meetings/{meeting_id}/chat",
        json={"message": "hi", "model": "not/allowed"},
    )
    assert resp.status_code == 400
    assert route.call_count == 0
