"""Thread chat: digest content, transcript tool-hop, history, ownership."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.db import get_conn, utcnow
from app.errors import NotFoundError
from app.services import chat as chat_svc
from app.services.providers.base import EmailCandidate, EventCandidate, IntegrationRef

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
    monkeypatch.setenv("MMN_WEB_SEARCH_BASE_URL", "https://search.test")
    monkeypatch.setenv("MMN_WEB_SEARCH_API_KEY", "sk-search-test")
    from app.config import reset_settings_cache

    reset_settings_cache()


def _diarization_payload(text: str) -> dict:
    return {
        "task": "transcribe",
        "duration": 5.0,
        "num_speakers": 1,
        "speakers": [
            {"id": "SPEAKER_00", "label": "Speaker 1",
             "total_speech_duration": 5.0, "segment_count": 1}
        ],
        "segments": [
            {"id": 0, "speaker": "SPEAKER_00", "label": "Speaker 1",
             "start": 0.0, "end": 5.0, "text": text}
        ],
    }


# --------------------------------------------------------------------------- #
# Unit: digest and transcript-tool building blocks
# --------------------------------------------------------------------------- #


@pytest.fixture
def seeded(conn):
    """One user, one thread, a summarized meeting and a transcript-only meeting,
    one calendar event and one email -- all direct SQL, matching the fixture
    style in test_db.py rather than driving the full upload/diarize pipeline.
    """
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
        "VALUES (1, 1, 1, 'Kickoff', '2026-07-01T10:00:00Z', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO meetings (id, thread_id, owner_id, title, meeting_at, created_at, updated_at) "
        "VALUES (2, 1, 1, 'Budget review', '2026-07-15T10:00:00Z', ?, ?)",
        (now, now),
    )

    conn.execute(
        "INSERT INTO diarizations (id, meeting_id, provider_url, model, raw_json, created_at) "
        "VALUES (1, 1, 'http://x', 'm', ?, ?)",
        (json.dumps(_diarization_payload("Let's kick off the quarter.")), now),
    )
    conn.execute("UPDATE meetings SET active_diarization_id = 1 WHERE id = 1")
    conn.execute(
        "INSERT INTO summaries (id, meeting_id, version, is_current, model, prompt_name, "
        "prompt_sha256, prompt_text, tldr, key_decisions_json, open_questions_json, "
        "participants_json, topics_json, created_at) "
        "VALUES (1, 1, 1, 1, 'm', 'p', 'sha', 't', 'Kicked off Q3 planning.', ?, '[]', '[]', '[]', ?)",
        (json.dumps([{"decision": "Ship by September", "made_by": "Dana"}]), now),
    )
    conn.execute("UPDATE meetings SET active_summary_id = 1 WHERE id = 1")
    conn.execute(
        "INSERT INTO action_items (summary_id, meeting_id, idx, text, owner_label, status, created_at) "
        "VALUES (1, 1, 0, 'Draft the roadmap', 'Dana', 'open', ?)",
        (now,),
    )

    conn.execute(
        "INSERT INTO diarizations (id, meeting_id, provider_url, model, raw_json, created_at) "
        "VALUES (2, 2, 'http://x', 'm', ?, ?)",
        (json.dumps(_diarization_payload("The exact budget number is forty two thousand dollars.")), now),
    )
    conn.execute("UPDATE meetings SET active_diarization_id = 2 WHERE id = 2")

    conn.execute(
        "INSERT INTO thread_calendar_events (thread_id, uid, summary, description, start_at, "
        "calendar_name, raw_json, attached_at) "
        "VALUES (1, 'evt-1', 'Q3 Planning Sync', 'Discuss the roadmap for Q3 in detail.', "
        "'2026-07-01T10:00:00Z', 'Work', '{}', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO thread_emails (thread_id, message_id, sender, subject, date, snippet, "
        "raw_json, attached_at) "
        "VALUES (1, '<m1@x>', 'dana@x.com', 'Re: Q3 budget', '2026-06-30T09:00:00Z', "
        "'Sending over the numbers ahead of the sync.', '{}', ?)",
        (now,),
    )
    return conn


def test_digest_includes_summary_fields_and_full_event_description(seeded):
    digest, truncated = chat_svc.build_thread_digest(seeded, thread_id=1)

    assert not truncated
    assert "Kicked off Q3 planning." in digest
    assert "Ship by September" in digest
    assert "Draft the roadmap" in digest
    # Full description, not the 240-char snippet clamp summarize.py uses for prompts.
    assert "Discuss the roadmap for Q3 in detail." in digest
    assert "Sending over the numbers ahead of the sync." in digest
    # A transcript-only meeting is flagged, not silently omitted.
    assert "no summary yet" in digest
    assert "meeting_id=2" in digest


def test_digest_truncates_older_meetings_within_budget(seeded, monkeypatch):
    monkeypatch.setenv("MMN_SUMMARY_MAX_INPUT_TOKENS", "5")
    from app.config import reset_settings_cache

    reset_settings_cache()

    digest, truncated = chat_svc.build_thread_digest(seeded, thread_id=1)

    assert truncated
    assert "not shown here due to the context limit" in digest
    # The budget is tiny, but at least the most recent meeting is always included.
    assert "Meeting 2" in digest


def test_fetch_meeting_transcript_text(seeded):
    text = chat_svc.fetch_meeting_transcript_text(seeded, thread_id=1, meeting_id=2)
    assert "forty two thousand dollars" in text


def test_fetch_meeting_transcript_text_rejects_foreign_meeting(seeded):
    seeded.execute(
        "INSERT INTO threads (id, owner_id, title, created_at, updated_at) "
        "VALUES (2, 1, 'Other thread', ?, ?)",
        (utcnow(), utcnow()),
    )
    with pytest.raises(NotFoundError):
        chat_svc.fetch_meeting_transcript_text(seeded, thread_id=2, meeting_id=2)


# --------------------------------------------------------------------------- #
# HTTP: tool-hop round trip, history, ownership
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
            (meeting["id"], json.dumps(_diarization_payload("The budget is forty two thousand dollars.")), now),
        )
        conn.execute(
            "UPDATE meetings SET active_diarization_id = 1 WHERE id = ?", (meeting["id"],)
        )
    return thread["id"], meeting["id"]


@respx.mock
def test_tool_hop_fetches_transcript_before_answering(user_client, isolated_settings):
    thread_id, meeting_id = _seed_via_api(user_client, isolated_settings)
    route = respx.post(LLM_URL).mock(
        side_effect=[
            stream_response([f"TOOL: get_transcript {meeting_id}"]),
            stream_response(["The exact figure is forty two thousand dollars."]),
        ]
    )

    resp = user_client.post(
        f"/api/threads/{thread_id}/chat",
        json={"message": "What exact number was mentioned in the budget review?"},
    )
    assert resp.status_code == 200, resp.text
    assert route.call_count == 2

    second_request = json.loads(route.calls[1].request.content)
    tool_turn = second_request["messages"][-1]
    assert tool_turn["role"] == "user"
    assert "forty two thousand dollars" in tool_turn["content"]

    frames = parse_sse_frames(resp.text)
    events = [e for e, _ in frames]
    assert "done" in events
    # The tool-call line itself must never reach the client as a token.
    assert not any(e == "token" and "TOOL:" in d.get("text", "") for e, d in frames)

    tool_call = next(d for e, d in frames if e == "tool_call")
    assert tool_call == {"tool": "get_transcript", "arg": str(meeting_id)}
    tool_result = next(d for e, d in frames if e == "tool_result")
    assert tool_result["tool"] == "get_transcript"
    assert "forty two thousand dollars" in tool_result["result"]
    # In order: announce the call, then its result, before the round is done.
    assert events.index("tool_call") < events.index("tool_result") < events.index("done")

    done_data = next(d for e, d in frames if e == "done")
    assert done_data["role"] == "assistant"
    assert "forty two thousand dollars" in done_data["content"]
    assert "TOOL:" not in done_data["content"]

    # And it streamed, not just returned one lump at the end.
    token_events = [d for e, d in frames if e == "token"]
    assert len(token_events) >= 1


@respx.mock
def test_history_round_trips(user_client, isolated_settings):
    thread_id, _ = _seed_via_api(user_client, isolated_settings)
    respx.post(LLM_URL).mock(return_value=stream_response(["Sure, here's an answer."]))

    user_client.post(f"/api/threads/{thread_id}/chat", json={"message": "First question"})
    user_client.post(f"/api/threads/{thread_id}/chat", json={"message": "Second question"})

    history = user_client.get(f"/api/threads/{thread_id}/chat").json()
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]
    assert history[0]["content"] == "First question"
    assert history[2]["content"] == "Second question"
    # Usage is captured on the assistant turn only -- a user message has no
    # completion to have spent tokens on.
    assert history[1]["prompt_tokens"] == 100
    assert history[1]["completion_tokens"] == 20
    assert history[0]["prompt_tokens"] is None


@respx.mock
def test_clear_removes_all_messages(user_client, isolated_settings):
    thread_id, _ = _seed_via_api(user_client, isolated_settings)
    respx.post(LLM_URL).mock(return_value=stream_response(["Sure, here's an answer."]))
    user_client.post(f"/api/threads/{thread_id}/chat", json={"message": "First question"})

    resp = user_client.delete(f"/api/threads/{thread_id}/chat")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "removed": 2}

    assert user_client.get(f"/api/threads/{thread_id}/chat").json() == []


@respx.mock
def test_other_users_cannot_clear_a_thread_they_dont_own(user_client, other_user_client, isolated_settings):
    thread_id, _ = _seed_via_api(user_client, isolated_settings)
    respx.post(LLM_URL).mock(return_value=stream_response(["answer"]))
    user_client.post(f"/api/threads/{thread_id}/chat", json={"message": "First question"})

    resp = other_user_client.delete(f"/api/threads/{thread_id}/chat")
    assert resp.status_code == 404

    # Untouched.
    assert len(user_client.get(f"/api/threads/{thread_id}/chat").json()) == 2


@respx.mock
def test_llm_failure_streams_an_error_frame_and_persists_nothing(user_client, isolated_settings):
    thread_id, _ = _seed_via_api(user_client, isolated_settings)
    respx.post(LLM_URL).mock(return_value=httpx.Response(401, json={"error": "bad key"}))

    resp = user_client.post(f"/api/threads/{thread_id}/chat", json={"message": "hello?"})
    assert resp.status_code == 200, resp.text

    frames = parse_sse_frames(resp.text)
    events = [e for e, _ in frames]
    assert "error" in events
    assert "done" not in events
    error_data = next(d for e, d in frames if e == "error")
    assert error_data["code"] == "LLM_AUTH_FAILED"

    history = user_client.get(f"/api/threads/{thread_id}/chat").json()
    assert history == []


@respx.mock
def test_other_users_thread_404s(user_client, other_user_client, isolated_settings):
    thread_id, _ = _seed_via_api(user_client, isolated_settings)
    respx.post(LLM_URL).mock(return_value=stream_response(["answer"]))

    resp = other_user_client.post(
        f"/api/threads/{thread_id}/chat", json={"message": "peeking"}
    )
    assert resp.status_code == 404

    resp = other_user_client.get(f"/api/threads/{thread_id}/chat")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Follow-up suggestions
# --------------------------------------------------------------------------- #


@respx.mock
def test_a_completed_answer_gets_a_suggestions_event(user_client, isolated_settings):
    thread_id, _ = _seed_via_api(user_client, isolated_settings)
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

    resp = user_client.post(f"/api/threads/{thread_id}/chat", json={"message": "hi"})
    assert resp.status_code == 200, resp.text

    events = [e for e, _ in parse_sse_frames(resp.text)]
    # After, not before -- the chips are for the round that just finished, not
    # a replacement for the answer itself.
    assert events.index("suggestions") > events.index("done")
    suggestions_data = next(d for e, d in parse_sse_frames(resp.text) if e == "suggestions")
    assert suggestions_data["suggestions"] == ["A", "B", "C"]


@respx.mock
def test_a_failed_suggestions_call_does_not_break_the_answer(user_client, isolated_settings):
    thread_id, _ = _seed_via_api(user_client, isolated_settings)
    respx.post(LLM_URL).mock(
        side_effect=[
            stream_response(["Sure, here's an answer."]),
            httpx.Response(500, json={"error": "boom"}),
        ]
    )

    resp = user_client.post(f"/api/threads/{thread_id}/chat", json={"message": "hi"})
    assert resp.status_code == 200, resp.text

    events = [e for e, _ in parse_sse_frames(resp.text)]
    assert "done" in events
    assert "suggestions" not in events


# --------------------------------------------------------------------------- #
# Model selection
# --------------------------------------------------------------------------- #


@respx.mock
def test_an_enabled_model_is_used_for_the_turn(user_client, admin_client, isolated_settings):
    thread_id, _ = _seed_via_api(user_client, isolated_settings)
    admin_client.put("/api/settings", json={"values": {"llm_chat_models": ["extra/model"]}})
    route = respx.post(LLM_URL).mock(return_value=stream_response(["answer"]))

    resp = user_client.post(
        f"/api/threads/{thread_id}/chat",
        json={"message": "hi", "model": "extra/model"},
    )
    assert resp.status_code == 200, resp.text
    assert json.loads(route.calls[0].request.content)["model"] == "extra/model"

    done = next(d for e, d in parse_sse_frames(resp.text) if e == "done")
    assert done["model"] == "extra/model"


@respx.mock
def test_a_model_outside_the_enabled_set_is_rejected(user_client, isolated_settings):
    thread_id, _ = _seed_via_api(user_client, isolated_settings)
    route = respx.post(LLM_URL).mock(return_value=stream_response(["answer"]))

    resp = user_client.post(
        f"/api/threads/{thread_id}/chat",
        json={"message": "hi", "model": "not/allowed"},
    )
    assert resp.status_code == 400
    assert route.call_count == 0


# --------------------------------------------------------------------------- #
# search_context / get_email / attach_email / attach_event tool hops
# --------------------------------------------------------------------------- #

SEARCH_INTEGRATION_ID = 42


class FakeSearchProvider:
    """Stands in for one connected account, faked a level above the transport
    -- same seam test_matching.py's FakeProvider uses at
    ``app.services.matching.providers_svc.load_for_user``.
    """

    def __init__(self, kind: str, *, label: str):
        self.ref = IntegrationRef(
            id=SEARCH_INTEGRATION_ID, provider="fake", account_label=label,
            calendar_enabled=kind == "calendar", email_enabled=kind == "email",
        )

    async def search_events(self, **kwargs):
        return [
            EventCandidate(
                uid="evt-cutover", summary="Cutover planning",
                description="Discuss the rollback window.", location="",
                start="2026-03-18T09:00:00+00:00", end="2026-03-18T09:30:00+00:00",
                calendar_name="work@x", account="work@x", type="google",
                provider="fake", integration_id=SEARCH_INTEGRATION_ID,
            )
        ]

    async def search_emails(self, **kwargs):
        return [
            EmailCandidate(
                id="native-1", message_id="fake:42:native-1", sender="priya@acme.com",
                subject="Re: cutover window", date="2026-03-17T17:42:00+00:00",
                snippet="rollback rehearsal is booked", account="work@x",
                provider="fake", integration_id=SEARCH_INTEGRATION_ID,
            )
        ]


def _fake_load_for_user(conn, user_id, *, kind=None):
    if user_id is None:
        return []
    sources = []
    if kind in (None, "calendar"):
        sources.append(FakeSearchProvider("calendar", label="cal@x"))
    if kind in (None, "email"):
        sources.append(FakeSearchProvider("email", label="mail@x"))
    return sources


@pytest.fixture
def fake_search(monkeypatch):
    monkeypatch.setattr(
        "app.services.matching.providers_svc.load_for_user", _fake_load_for_user
    )


def _user_id(isolated_settings, username: str = "alice") -> int:
    with get_conn(isolated_settings.db_path) as conn:
        return conn.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()["id"]


@respx.mock
def test_search_context_tool_hop_surfaces_new_candidates(
    user_client, isolated_settings, fake_search
):
    thread_id, _ = _seed_via_api(user_client, isolated_settings)
    route = respx.post(LLM_URL).mock(
        side_effect=[
            stream_response(["TOOL: search_context cutover"]),
            stream_response(["Found a matching email about the cutover window."]),
        ]
    )

    resp = user_client.post(
        f"/api/threads/{thread_id}/chat",
        json={"message": "Did anyone email me about the cutover?"},
    )
    assert resp.status_code == 200, resp.text
    assert route.call_count == 2

    tool_turn = json.loads(route.calls[1].request.content)["messages"][-1]
    assert tool_turn["role"] == "user"
    assert "rollback rehearsal is booked" in tool_turn["content"]
    assert "email_id: fake:42:native-1" in tool_turn["content"]
    assert "event_id: evt-cutover" in tool_turn["content"]

    # Ephemeral: a search alone must never write anything.
    with get_conn(isolated_settings.db_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM thread_emails WHERE thread_id = ?", (thread_id,)
            ).fetchone()[0]
            == 0
        )


@respx.mock
def test_long_tool_command_never_leaks_as_visible_tokens(
    user_client, isolated_settings, fake_search
):
    thread_id, _ = _seed_via_api(user_client, isolated_settings)
    query = "cutover rollback rehearsal finance approval schedule"
    respx.post(LLM_URL).mock(
        side_effect=[
            stream_response([f"TOOL: search_context {query}"]),
            stream_response(["I found the relevant cutover context."]),
        ]
    )

    resp = user_client.post(
        f"/api/threads/{thread_id}/chat",
        json={"message": "Find the detailed cutover discussion."},
    )
    assert resp.status_code == 200, resp.text
    frames = parse_sse_frames(resp.text)
    assert not any(
        event == "token" and "TOOL:" in data.get("text", "")
        for event, data in frames
    )
    assert any(event == "tool_call" for event, _ in frames)


@respx.mock
def test_get_email_tool_hop_fetches_the_full_body_after_a_search(
    user_client, isolated_settings, fake_search, monkeypatch
):
    thread_id, _ = _seed_via_api(user_client, isolated_settings)
    user_id = _user_id(isolated_settings)
    now = utcnow()
    with get_conn(isolated_settings.db_path) as conn:
        conn.execute(
            "INSERT INTO integrations (id, user_id, provider, account_key, account_label, "
            "calendar_enabled, email_enabled, auth_type, config_json, created_at, updated_at) "
            "VALUES (?, ?, 'fake', 'fake-key', 'Fake', 0, 1, 'none', '{}', ?, ?)",
            (SEARCH_INTEGRATION_ID, user_id, now, now),
        )

    class FakeBodyProvider:
        async def get_email_body(self, *, native_id, folder_id=None):
            return "The rollback rehearsal is booked for Friday at noon, per finance."

    monkeypatch.setattr(
        chat_svc.providers_svc, "build_provider", lambda conn, row: FakeBodyProvider()
    )

    route = respx.post(LLM_URL).mock(
        side_effect=[
            stream_response(["TOOL: search_context cutover"]),
            stream_response(["TOOL: get_email fake:42:native-1"]),
            stream_response(["The rehearsal is Friday at noon."]),
        ]
    )

    resp = user_client.post(
        f"/api/threads/{thread_id}/chat",
        json={"message": "What day exactly is the rollback rehearsal?"},
    )
    assert resp.status_code == 200, resp.text
    assert route.call_count == 3

    tool_turn = json.loads(route.calls[2].request.content)["messages"][-1]
    assert tool_turn["role"] == "user"
    assert "Friday at noon, per finance" in tool_turn["content"]


@respx.mock
def test_attach_email_tool_hop_writes_a_thread_email(
    user_client, isolated_settings, fake_search
):
    thread_id, _ = _seed_via_api(user_client, isolated_settings)
    route = respx.post(LLM_URL).mock(
        side_effect=[
            stream_response(["TOOL: search_context cutover"]),
            stream_response(["TOOL: attach_email fake:42:native-1"]),
            stream_response(["Attached that email to this thread."]),
        ]
    )

    resp = user_client.post(
        f"/api/threads/{thread_id}/chat",
        json={"message": "Attach the email you found about the cutover."},
    )
    assert resp.status_code == 200, resp.text
    assert route.call_count == 3

    with get_conn(isolated_settings.db_path) as conn:
        row = conn.execute(
            "SELECT * FROM thread_emails WHERE thread_id = ? AND message_id = ?",
            (thread_id, "fake:42:native-1"),
        ).fetchone()
    assert row is not None
    assert row["subject"] == "Re: cutover window"
    # A direct, human-directed action taken through chat -- not the unattended
    # sweep, so it must not create an unread badge.
    assert row["auto_attached"] == 0
    assert row["seen_at"] is not None


@respx.mock
def test_attach_event_tool_hop_writes_a_thread_event(
    user_client, isolated_settings, fake_search
):
    thread_id, _ = _seed_via_api(user_client, isolated_settings)
    route = respx.post(LLM_URL).mock(
        side_effect=[
            stream_response(["TOOL: search_context cutover"]),
            stream_response(["TOOL: attach_event evt-cutover"]),
            stream_response(["Attached the cutover planning meeting."]),
        ]
    )

    resp = user_client.post(
        f"/api/threads/{thread_id}/chat",
        json={"message": "Attach that cutover planning meeting too."},
    )
    assert resp.status_code == 200, resp.text
    assert route.call_count == 3

    with get_conn(isolated_settings.db_path) as conn:
        row = conn.execute(
            "SELECT * FROM thread_calendar_events WHERE thread_id = ? AND uid = ?",
            (thread_id, "evt-cutover"),
        ).fetchone()
    assert row is not None
    assert row["summary"] == "Cutover planning"


@respx.mock
def test_attach_without_a_prior_search_is_rejected(
    user_client, isolated_settings, fake_search
):
    thread_id, _ = _seed_via_api(user_client, isolated_settings)
    route = respx.post(LLM_URL).mock(
        side_effect=[
            stream_response(["TOOL: attach_email fake:42:native-1"]),
            stream_response(["I wasn't able to attach that."]),
        ]
    )

    resp = user_client.post(
        f"/api/threads/{thread_id}/chat",
        json={"message": "Attach the cutover email."},
    )
    assert resp.status_code == 200, resp.text
    assert route.call_count == 2

    tool_turn = json.loads(route.calls[1].request.content)["messages"][-1]
    assert "search_context first" in tool_turn["content"]

    with get_conn(isolated_settings.db_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM thread_emails WHERE thread_id = ?", (thread_id,)
            ).fetchone()[0]
            == 0
        )


# --------------------------------------------------------------------------- #
# web_search tool hop
# --------------------------------------------------------------------------- #


@respx.mock
def test_web_search_tool_hop_answers_from_real_results(user_client, isolated_settings):
    thread_id, _ = _seed_via_api(user_client, isolated_settings)
    search_route = respx.post("https://search.test/v1/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Q3 planning best practices",
                        "url": "https://example.com/q3",
                        "snippet": "Ship in September for the smoothest rollout.",
                    }
                ]
            },
        )
    )
    llm_route = respx.post(LLM_URL).mock(
        side_effect=[
            stream_response(["TOOL: web_search Q3 planning best practices"]),
            stream_response(["Most guides suggest shipping in September."]),
        ]
    )

    resp = user_client.post(
        f"/api/threads/{thread_id}/chat",
        json={"message": "What's a good practice for Q3 planning in general?"},
    )
    assert resp.status_code == 200, resp.text
    assert llm_route.call_count == 2
    assert search_route.called

    tool_turn = json.loads(llm_route.calls[1].request.content)["messages"][-1]
    assert tool_turn["role"] == "user"
    assert "Ship in September for the smoothest rollout." in tool_turn["content"]

    frames = parse_sse_frames(resp.text)
    events = [e for e, _ in frames]
    assert not any(e == "token" and "TOOL:" in d.get("text", "") for e, d in frames)
    assert events.index("tool_call") < events.index("tool_result") < events.index("done")

    tool_call = next(d for e, d in frames if e == "tool_call")
    assert tool_call["tool"] == "web_search"
