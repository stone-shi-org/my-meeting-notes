"""Home chat: cross-thread digest, its five tool hops, history, isolation."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.db import get_conn, utcnow
from app.services import home_chat as home_chat_svc
from app.services.providers.base import EmailCandidate, EventCandidate, IntegrationRef
from tests.test_chat import parse_sse_frames, stream_response

LLM_URL = "https://llm.test/v1/chat/completions"


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
# Unit: digest building
# --------------------------------------------------------------------------- #


@pytest.fixture
def seeded(conn):
    """Two threads for one user: one in a group with a next step, one bare."""
    now = utcnow()
    conn.execute(
        "INSERT INTO users (id, username, password_hash, password_salt, created_at, updated_at) "
        "VALUES (1, 'u', 'h', 's', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO thread_groups (id, owner_id, name, created_at, updated_at) "
        "VALUES (1, 1, 'Work', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO threads (id, owner_id, title, description, group_id, next_step, "
        "updated_at, created_at) "
        "VALUES (1, 1, 'Q3 planning', 'Ship the roadmap by September.', 1, "
        "'Send the roadmap to Dana', '2026-07-20T00:00:00Z', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO threads (id, owner_id, title, updated_at, created_at) "
        "VALUES (2, 1, 'Loose end', '2026-01-01T00:00:00Z', ?)",
        (now,),
    )
    return conn


def test_digest_lists_every_thread_with_group_and_next_step(seeded):
    digest, truncated = home_chat_svc.build_home_digest(seeded, user_id=1)

    assert not truncated
    assert "Thread 1: Q3 planning" in digest
    assert "Group: Work" in digest
    assert "Send the roadmap to Dana" in digest
    assert "Thread 2: Loose end" in digest
    assert "Group: Ungrouped" in digest
    assert "(not generated yet)" in digest


def test_digest_marks_archived_threads(seeded):
    seeded.execute("UPDATE threads SET archived = 1 WHERE id = 2")
    digest, _truncated = home_chat_svc.build_home_digest(seeded, user_id=1)
    assert "Thread 2: Loose end (Archived)" in digest
    assert "1 active, 1 archived" in digest


def test_digest_truncates_oldest_threads_within_budget(seeded, monkeypatch):
    monkeypatch.setenv("MMN_SUMMARY_MAX_INPUT_TOKENS", "5")
    from app.config import reset_settings_cache

    reset_settings_cache()

    digest, truncated = home_chat_svc.build_home_digest(seeded, user_id=1)

    assert truncated
    assert "not shown here due to the context limit" in digest
    # Most recently updated thread always survives.
    assert "Thread 1: Q3 planning" in digest


def test_digest_says_so_when_there_are_no_threads(conn):
    conn.execute(
        "INSERT INTO users (id, username, password_hash, password_salt, created_at, updated_at) "
        "VALUES (1, 'u', 'h', 's', ?, ?)",
        (utcnow(), utcnow()),
    )
    digest, truncated = home_chat_svc.build_home_digest(conn, user_id=1)
    assert not truncated
    assert "no threads yet" in digest


# --------------------------------------------------------------------------- #
# HTTP: tool hops, history, isolation
# --------------------------------------------------------------------------- #


def _seed_via_api(client, isolated_settings):
    """Two threads: one with a meeting (+ transcript + next step), one bare."""
    thread1 = client.post(
        "/api/threads", json={"title": "Q3 planning", "description": "Ship by Sept"}
    ).json()
    meeting = client.post(
        "/api/meetings",
        json={"thread_id": thread1["id"], "title": "Budget review",
              "meeting_at": "2026-07-15T10:00:00Z"},
    ).json()
    thread2 = client.post("/api/threads", json={"title": "Loose end"}).json()

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
        conn.execute(
            "UPDATE threads SET next_step = 'Send the roadmap to Dana' WHERE id = ?",
            (thread1["id"],),
        )
    return thread1["id"], thread2["id"], meeting["id"]


@respx.mock
def test_get_thread_detail_tool_hop_returns_that_threads_own_digest(user_client, isolated_settings):
    thread_id, _other_id, _meeting_id = _seed_via_api(user_client, isolated_settings)
    route = respx.post(LLM_URL).mock(
        side_effect=[
            stream_response([f"TOOL: get_thread_detail {thread_id}"]),
            stream_response(["The Q3 planning thread has one meeting so far."]),
        ]
    )

    resp = user_client.post(
        "/api/home/chat", json={"message": "What's going on with Q3 planning?"}
    )
    assert resp.status_code == 200, resp.text
    assert route.call_count == 2

    tool_turn = json.loads(route.calls[1].request.content)["messages"][-1]
    assert tool_turn["role"] == "user"
    assert "Meeting" in tool_turn["content"]
    assert "Budget review" in tool_turn["content"]

    frames = parse_sse_frames(resp.text)
    tool_call = next(d for e, d in frames if e == "tool_call")
    assert tool_call == {"tool": "get_thread_detail", "arg": str(thread_id)}
    assert "done" in [e for e, _ in frames]


@respx.mock
def test_get_thread_detail_rejects_a_thread_owned_by_someone_else(
    user_client, other_user_client, isolated_settings
):
    thread_id, _other_id, _meeting_id = _seed_via_api(user_client, isolated_settings)
    route = respx.post(LLM_URL).mock(
        side_effect=[
            stream_response([f"TOOL: get_thread_detail {thread_id}"]),
            stream_response(["I couldn't find that thread."]),
        ]
    )

    resp = other_user_client.post(
        "/api/home/chat", json={"message": "What's in thread 1?"}
    )
    assert resp.status_code == 200, resp.text
    tool_turn = json.loads(route.calls[1].request.content)["messages"][-1]
    assert f"No thread {thread_id}" in tool_turn["content"]


@respx.mock
def test_get_transcript_tool_hop_fetches_verbatim_wording(user_client, isolated_settings):
    thread_id, _other_id, meeting_id = _seed_via_api(user_client, isolated_settings)
    route = respx.post(LLM_URL).mock(
        side_effect=[
            stream_response([f"TOOL: get_transcript {thread_id} {meeting_id}"]),
            stream_response(["The exact figure is forty two thousand dollars."]),
        ]
    )

    resp = user_client.post(
        "/api/home/chat",
        json={"message": "What exact number was mentioned in the budget review?"},
    )
    assert resp.status_code == 200, resp.text
    tool_turn = json.loads(route.calls[1].request.content)["messages"][-1]
    assert "forty two thousand dollars" in tool_turn["content"]


@respx.mock
def test_get_transcript_rejects_a_meeting_from_a_foreign_thread(user_client, isolated_settings):
    thread_id, other_thread_id, meeting_id = _seed_via_api(user_client, isolated_settings)
    route = respx.post(LLM_URL).mock(
        side_effect=[
            stream_response([f"TOOL: get_transcript {other_thread_id} {meeting_id}"]),
            stream_response(["I couldn't find that meeting there."]),
        ]
    )

    resp = user_client.post(
        "/api/home/chat", json={"message": "Show me that transcript."}
    )
    assert resp.status_code == 200, resp.text
    tool_turn = json.loads(route.calls[1].request.content)["messages"][-1]
    assert "No transcript available" in tool_turn["content"]


@respx.mock
def test_get_upcoming_tool_hop_formats_events(user_client, isolated_settings, monkeypatch):
    _seed_via_api(user_client, isolated_settings)

    async def fake_collect(conn_factory, *, user_id, days):
        return {
            "connected": 1,
            "start": "2026-08-10T00:00:00+00:00",
            "end": "2026-08-24T00:00:00+00:00",
            "events": [
                {"uid": "evt-1", "summary": "Board sync", "start": "2026-08-12T15:00:00+00:00",
                 "location": "Zoom", "attached": None}
            ],
            "error": None,
            "source_errors": [],
        }

    monkeypatch.setattr(home_chat_svc.upcoming_svc, "collect", fake_collect)
    route = respx.post(LLM_URL).mock(
        side_effect=[
            stream_response(["TOOL: get_upcoming 14"]),
            stream_response(["You have Board sync coming up."]),
        ]
    )

    resp = user_client.post(
        "/api/home/chat", json={"message": "What's on my calendar?"}
    )
    assert resp.status_code == 200, resp.text
    tool_turn = json.loads(route.calls[1].request.content)["messages"][-1]
    assert "Board sync" in tool_turn["content"]
    assert "Zoom" in tool_turn["content"]


# --------------------------------------------------------------------------- #
# search_context -- read-only, no attach tool exists for home chat
# --------------------------------------------------------------------------- #

SEARCH_INTEGRATION_ID = 42


class FakeSearchProvider:
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


@respx.mock
def test_search_context_surfaces_candidates_without_writing_anything(
    user_client, isolated_settings, fake_search
):
    thread_id, _other_id, _meeting_id = _seed_via_api(user_client, isolated_settings)
    route = respx.post(LLM_URL).mock(
        side_effect=[
            stream_response(["TOOL: search_context cutover"]),
            stream_response(["Found a matching email about the cutover window."]),
        ]
    )

    resp = user_client.post(
        "/api/home/chat", json={"message": "Did anyone email me about the cutover?"}
    )
    assert resp.status_code == 200, resp.text
    assert route.call_count == 2

    tool_turn = json.loads(route.calls[1].request.content)["messages"][-1]
    assert "rollback rehearsal is booked" in tool_turn["content"]
    assert "not attached to any thread" in tool_turn["content"]

    with get_conn(isolated_settings.db_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM thread_emails WHERE thread_id = ?", (thread_id,)
            ).fetchone()[0]
            == 0
        )


@respx.mock
def test_an_attach_line_from_the_model_is_not_a_recognised_tool(
    user_client, isolated_settings, fake_search
):
    """Home chat has no attach tool -- a `TOOL: attach_email ...` line never
    matches TOOL_RE, so it just falls through as ordinary (odd) prose rather
    than writing to any thread."""
    _seed_via_api(user_client, isolated_settings)
    respx.post(LLM_URL).mock(
        return_value=stream_response(["TOOL: attach_email fake:42:native-1"])
    )

    resp = user_client.post(
        "/api/home/chat", json={"message": "Attach that email somewhere."}
    )
    assert resp.status_code == 200, resp.text
    frames = parse_sse_frames(resp.text)
    assert not any(e == "tool_call" for e, _ in frames)
    done = next(d for e, d in frames if e == "done")
    assert "TOOL: attach_email" in done["content"]


# --------------------------------------------------------------------------- #
# History, clearing, isolation between users
# --------------------------------------------------------------------------- #


@respx.mock
def test_history_round_trips(user_client, isolated_settings):
    _seed_via_api(user_client, isolated_settings)
    respx.post(LLM_URL).mock(return_value=stream_response(["Sure, here's an answer."]))

    user_client.post("/api/home/chat", json={"message": "First question"})
    user_client.post("/api/home/chat", json={"message": "Second question"})

    history = user_client.get("/api/home/chat").json()
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]
    assert history[0]["content"] == "First question"
    assert history[2]["content"] == "Second question"
    assert history[1]["prompt_tokens"] == 100
    assert history[1]["completion_tokens"] == 20


@respx.mock
def test_clear_removes_all_messages(user_client, isolated_settings):
    _seed_via_api(user_client, isolated_settings)
    respx.post(LLM_URL).mock(return_value=stream_response(["Sure, here's an answer."]))
    user_client.post("/api/home/chat", json={"message": "First question"})

    resp = user_client.delete("/api/home/chat")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "removed": 2}
    assert user_client.get("/api/home/chat").json() == []


@respx.mock
def test_each_users_home_chat_is_isolated(user_client, other_user_client, isolated_settings):
    _seed_via_api(user_client, isolated_settings)
    respx.post(LLM_URL).mock(return_value=stream_response(["answer"]))

    user_client.post("/api/home/chat", json={"message": "alice's question"})
    other_user_client.post("/api/home/chat", json={"message": "bob's question"})

    alice_history = user_client.get("/api/home/chat").json()
    bob_history = other_user_client.get("/api/home/chat").json()
    assert [m["content"] for m in alice_history if m["role"] == "user"] == ["alice's question"]
    assert [m["content"] for m in bob_history if m["role"] == "user"] == ["bob's question"]

    # Clearing bob's conversation must not touch alice's.
    other_user_client.delete("/api/home/chat")
    assert len(user_client.get("/api/home/chat").json()) == 2
    assert other_user_client.get("/api/home/chat").json() == []


@respx.mock
def test_llm_failure_streams_an_error_frame_and_persists_nothing(user_client, isolated_settings):
    _seed_via_api(user_client, isolated_settings)
    respx.post(LLM_URL).mock(return_value=httpx.Response(401, json={"error": "bad key"}))

    resp = user_client.post("/api/home/chat", json={"message": "hello?"})
    assert resp.status_code == 200, resp.text

    frames = parse_sse_frames(resp.text)
    events = [e for e, _ in frames]
    assert "error" in events
    assert "done" not in events
    error_data = next(d for e, d in frames if e == "error")
    assert error_data["code"] == "LLM_AUTH_FAILED"

    assert user_client.get("/api/home/chat").json() == []


# --------------------------------------------------------------------------- #
# Follow-up suggestions
# --------------------------------------------------------------------------- #


@respx.mock
def test_a_completed_answer_gets_a_suggestions_event(user_client, isolated_settings):
    _seed_via_api(user_client, isolated_settings)
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

    resp = user_client.post("/api/home/chat", json={"message": "hi"})
    assert resp.status_code == 200, resp.text

    frames = parse_sse_frames(resp.text)
    events = [e for e, _ in frames]
    assert events.index("suggestions") > events.index("done")
    suggestions_data = next(d for e, d in frames if e == "suggestions")
    assert suggestions_data["suggestions"] == ["A", "B", "C"]


@respx.mock
def test_a_failed_suggestions_call_does_not_break_the_answer(user_client, isolated_settings):
    _seed_via_api(user_client, isolated_settings)
    respx.post(LLM_URL).mock(
        side_effect=[
            stream_response(["Sure, here's an answer."]),
            httpx.Response(500, json={"error": "boom"}),
        ]
    )

    resp = user_client.post("/api/home/chat", json={"message": "hi"})
    assert resp.status_code == 200, resp.text

    events = [e for e, _ in parse_sse_frames(resp.text)]
    assert "done" in events
    assert "suggestions" not in events


# --------------------------------------------------------------------------- #
# web_search tool hop
# --------------------------------------------------------------------------- #


@respx.mock
def test_web_search_tool_hop_answers_from_real_results(user_client, isolated_settings):
    _seed_via_api(user_client, isolated_settings)
    search_route = respx.post("https://search.test/v1/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Hybrid work equipment guides",
                        "url": "https://example.com/hybrid",
                        "snippet": "Most companies issue a laptop and a docking station.",
                    }
                ]
            },
        )
    )
    llm_route = respx.post(LLM_URL).mock(
        side_effect=[
            stream_response(["TOOL: web_search hybrid work equipment best practices"]),
            stream_response(["Most companies issue a laptop plus a dock."]),
        ]
    )

    resp = user_client.post(
        "/api/home/chat",
        json={"message": "In general, what do most companies do for hybrid work equipment?"},
    )
    assert resp.status_code == 200, resp.text
    assert llm_route.call_count == 2
    assert search_route.called

    tool_turn = json.loads(llm_route.calls[1].request.content)["messages"][-1]
    assert "Most companies issue a laptop and a docking station." in tool_turn["content"]

    frames = parse_sse_frames(resp.text)
    events = [e for e, _ in frames]
    assert not any(e == "token" and "TOOL:" in d.get("text", "") for e, d in frames)
    assert events.index("tool_call") < events.index("tool_result") < events.index("done")

    tool_call = next(d for e, d in frames if e == "tool_call")
    assert tool_call["tool"] == "web_search"


# --------------------------------------------------------------------------- #
# run_telegram_turn -- the non-streaming reply the Telegram poller uses
# --------------------------------------------------------------------------- #


@respx.mock
async def test_run_telegram_turn_persists_into_its_own_table(user_client, isolated_settings):
    _seed_via_api(user_client, isolated_settings)
    user_id = user_client.get("/api/auth/me").json()["id"]
    respx.post(LLM_URL).mock(return_value=stream_response(["Nothing urgent right now."]))

    reply = await home_chat_svc.run_telegram_turn(
        isolated_settings.db_path, user_id, "What needs my attention?"
    )

    assert reply == "Nothing urgent right now."
    with get_conn(isolated_settings.db_path) as conn:
        telegram_rows = conn.execute(
            "SELECT role, content FROM telegram_chat_messages WHERE owner_id = ? ORDER BY id",
            (user_id,),
        ).fetchall()
        home_rows = conn.execute(
            "SELECT COUNT(*) FROM home_chat_messages WHERE owner_id = ?", (user_id,)
        ).fetchone()[0]
    assert [r["role"] for r in telegram_rows] == ["user", "assistant"]
    assert telegram_rows[1]["content"] == "Nothing urgent right now."
    # A Telegram exchange must never show up in the web home chat's own panel.
    assert home_rows == 0


@respx.mock
async def test_run_telegram_turn_still_runs_tool_hops(user_client, isolated_settings):
    thread_id, _other_id, _meeting_id = _seed_via_api(user_client, isolated_settings)
    user_id = user_client.get("/api/auth/me").json()["id"]
    route = respx.post(LLM_URL).mock(
        side_effect=[
            stream_response([f"TOOL: get_thread_detail {thread_id}"]),
            stream_response(["Here's the detail."]),
        ]
    )

    reply = await home_chat_svc.run_telegram_turn(
        isolated_settings.db_path, user_id, "Tell me about Q3 planning"
    )

    assert reply == "Here's the detail."
    assert route.call_count == 2
    tool_turn = json.loads(route.calls[1].request.content)["messages"][-1]
    assert "Meeting" in tool_turn["content"]


@respx.mock
async def test_run_telegram_turn_on_failure_apologizes_and_persists_nothing(
    user_client, isolated_settings
):
    _seed_via_api(user_client, isolated_settings)
    user_id = user_client.get("/api/auth/me").json()["id"]
    respx.post(LLM_URL).mock(return_value=httpx.Response(401, json={"error": "bad key"}))

    reply = await home_chat_svc.run_telegram_turn(isolated_settings.db_path, user_id, "hello?")

    assert "sorry" in reply.lower()
    with get_conn(isolated_settings.db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM telegram_chat_messages WHERE owner_id = ?", (user_id,)
        ).fetchone()[0]
    assert count == 0
