"""Best of 2: discarding the answer the user closed (home + meeting + thread)."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.db import get_conn, utcnow
from app.services import chat_compare
from tests.test_chat import stream_response
from tests.test_home_chat import LLM_URL, llm_settings  # noqa: F401  (autouse fixture)


@respx.mock
def test_home_discard_removes_the_loser_pair_and_keeps_the_winner(user_client):
    respx.post(LLM_URL).mock(return_value=stream_response(["answer"]))
    # Two requests for one prompt, as Best of 2 sends them.
    user_client.post("/api/home/chat", json={"message": "Q"})
    user_client.post("/api/home/chat", json={"message": "Q"})
    history = user_client.get("/api/home/chat").json()
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]

    loser = history[1]["id"]
    resp = user_client.delete(f"/api/home/chat/messages/{loser}")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "removed": 2}

    left = user_client.get("/api/home/chat").json()
    assert [m["id"] for m in left] == [history[2]["id"], history[3]["id"]]


@respx.mock
def test_discard_refuses_user_rows_and_other_peoples_rows(user_client, other_user_client):
    respx.post(LLM_URL).mock(return_value=stream_response(["answer"]))
    user_client.post("/api/home/chat", json={"message": "Q"})
    user_msg, assistant = user_client.get("/api/home/chat").json()

    # A user's own words can never be discarded through this route.
    assert user_client.delete(f"/api/home/chat/messages/{user_msg['id']}").status_code == 404
    # Someone else's id is a 404, exactly like a missing one.
    assert other_user_client.delete(f"/api/home/chat/messages/{assistant['id']}").status_code == 404
    assert len(user_client.get("/api/home/chat").json()) == 2


def test_discard_takes_only_the_user_row_directly_before(conn):
    now = utcnow()
    conn.execute(
        "INSERT INTO users (id, username, password_hash, password_salt, created_at, updated_at) "
        "VALUES (1, 'u', 'h', 's', ?, ?)",
        (now, now),
    )
    # An assistant row that follows another assistant row has no question of its own.
    for role in ("assistant", "assistant"):
        conn.execute(
            "INSERT INTO home_chat_messages (owner_id, role, content, created_at) "
            "VALUES (1, ?, 'x', ?)",
            (role, now),
        )
    ids = [r["id"] for r in conn.execute("SELECT id FROM home_chat_messages ORDER BY id")]
    removed = chat_compare.discard_reply(
        conn, "home_chat_messages", "owner_id = ?", (1,), ids[1]
    )
    assert removed == 1
    assert [r["id"] for r in conn.execute("SELECT id FROM home_chat_messages")] == [ids[0]]


def test_discard_rejects_an_unknown_table(conn):
    with pytest.raises(ValueError):
        chat_compare.discard_reply(conn, "users", "id = ?", (1,), 1)
