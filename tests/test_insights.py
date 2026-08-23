"""Live insights over an in-progress recording -- see app/services/insights.py.

Wired like test_notes.py: respx at the transport boundary, env vars driving
LLMConfig.from_db. insights_model is empty by default (RUNTIME_KEYS/.env), so
every test here sets it explicitly -- that's also what exercises the "not
configured" error path.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

LLM_URL = "https://llm.test/v1/chat/completions"


@pytest.fixture(autouse=True)
def wiring(monkeypatch):
    monkeypatch.setenv("MMN_LLM_BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("MMN_LLM_MODEL", "test/summary-model")
    monkeypatch.setenv("MMN_INSIGHTS_MODEL", "test/insights-model")
    from app.config import reset_settings_cache

    reset_settings_cache()


def mock_reply(payload: dict):
    return respx.post(LLM_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(payload)}}],
                "usage": {"prompt_tokens": 200, "completion_tokens": 40},
            },
        )
    )


@respx.mock
def test_interview_carries_forward_and_appends_a_new_question(user_client):
    previous = {
        "questions": [
            {
                "question": "What's your notice period?",
                "ai_answer_points": ["Two weeks"],
                "discussion": "",
            }
        ],
        "topics": [],
        "action_items": [],
    }
    reply = {
        "topics": [],
        "questions": [
            {
                "question": "What's your notice period?",
                "ai_answer_points": ["Two weeks"],
                "discussion": "",
            },
            {
                "question": "Why are you leaving your current role?",
                "ai_answer_points": ["Growth", "Scope"],
                "discussion": "Said they want more scope.",
            },
        ],
        "action_items": [],
    }
    mock_reply(reply)

    resp = user_client.post(
        "/api/insights/analyze",
        json={
            "meeting_type": "interview",
            "transcript": "Room: Why are you leaving your current role?\nMe: Looking for more scope.",
            "previous": previous,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["questions"]) == 2
    assert body["questions"][0] == previous["questions"][0]
    assert body["questions"][1]["question"] == "Why are you leaving your current role?"
    assert body["questions"][1]["discussion"] == "Said they want more scope."


@respx.mock
def test_general_marks_exactly_the_new_topic_current(user_client):
    reply = {
        "topics": [
            {"title": "Q3 roadmap", "summary": "Reviewed and agreed.", "current": False},
            {"title": "Hiring plan", "summary": "Discussing headcount for platform team.", "current": True},
        ],
        "questions": [],
        "action_items": [],
    }
    mock_reply(reply)

    resp = user_client.post(
        "/api/insights/analyze",
        json={
            "meeting_type": "general",
            "transcript": "Room: Let's move to hiring. Me: We need two more engineers.",
            "previous": {
                "topics": [{"title": "Q3 roadmap", "summary": "Reviewed.", "current": True}],
                "questions": [],
                "action_items": [],
            },
        },
    )
    assert resp.status_code == 200, resp.text
    topics = resp.json()["topics"]
    assert sum(1 for t in topics if t["current"]) == 1
    assert topics[-1]["current"] is True


@respx.mock
def test_general_appends_a_new_action_item(user_client):
    reply = {
        "topics": [],
        "questions": [],
        "action_items": [{"text": "Send the deck to the client", "owner": "Me"}],
    }
    mock_reply(reply)

    resp = user_client.post(
        "/api/insights/analyze",
        json={
            "meeting_type": "general",
            "transcript": "Me: I'll send the deck to the client tonight.",
            "previous": {"topics": [], "questions": [], "action_items": []},
        },
    )
    assert resp.status_code == 200, resp.text
    action_items = resp.json()["action_items"]
    assert action_items == [{"text": "Send the deck to the client", "owner": "Me"}]


@respx.mock
def test_falls_back_to_previous_per_key_when_the_model_returns_a_malformed_shape(user_client):
    # None of the three expected keys present -- chat_json still returns *a*
    # dict, just not the shape this endpoint expects back.
    mock_reply({"unexpected": True})
    previous = {
        "topics": [{"title": "Q3 roadmap", "summary": "Reviewed.", "current": True}],
        "questions": [
            {"question": "Tell me about yourself.", "ai_answer_points": ["Background"], "discussion": ""}
        ],
        "action_items": [{"text": "Follow up next week", "owner": None}],
    }

    resp = user_client.post(
        "/api/insights/analyze",
        json={"meeting_type": "interview", "transcript": "Room: ...", "previous": previous},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == previous


def test_requires_login(client):
    resp = client.post(
        "/api/insights/analyze",
        json={"meeting_type": "general", "transcript": "Room: hello"},
    )
    assert resp.status_code == 401


def test_rejects_an_unconfigured_model(user_client, monkeypatch):
    monkeypatch.setenv("MMN_INSIGHTS_MODEL", "")
    from app.config import reset_settings_cache

    reset_settings_cache()

    resp = user_client.post(
        "/api/insights/analyze",
        json={"meeting_type": "general", "transcript": "Room: hello"},
    )
    assert resp.status_code == 400
    assert "Insights model" in resp.json()["error"]["message"]


def test_rejects_an_unknown_meeting_type(user_client):
    resp = user_client.post(
        "/api/insights/analyze",
        json={"meeting_type": "standup", "transcript": "Room: hello"},
    )
    # meeting_type is just a slug now (see insight_types.py) -- no fixed
    # Literal to reject it at the Pydantic layer, so this 404s out of
    # insights_svc.analyze's insight_types_svc.get_type lookup instead.
    assert resp.status_code == 404
    assert "standup" in resp.json()["error"]["message"]
