"""MMN-14: personal API tokens -- the credential an MCP client presents."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db import get_conn
from app.security import hash_token
from app.services import api_tokens as tokens_svc


def create(client, name="Claude Code", **extra):
    resp = client.post("/api/tokens", json={"name": name, **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_returns_the_raw_token_exactly_once(user_client, isolated_settings):
    made = create(user_client)
    assert made["token"].startswith("mmn_")
    assert made["scope"] == "read"
    assert made["state"] == "active"
    assert made["prefix"] == made["token"][: tokens_svc.DISPLAY_PREFIX_LEN]

    listed = user_client.get("/api/tokens").json()
    assert [t["id"] for t in listed["tokens"]] == [made["id"]]
    assert "token" not in listed["tokens"][0]
    assert "token_hash" not in listed["tokens"][0]

    # Stored hashed, like sessions: nothing in the row can be replayed.
    with get_conn(isolated_settings.db_path) as conn:
        row = conn.execute("SELECT * FROM api_tokens WHERE id = ?", (made["id"],)).fetchone()
    assert row["token_hash"] == hash_token(made["token"])
    assert made["token"] not in dict(row).values()


def test_listing_describes_the_endpoint_and_tools(user_client):
    listed = user_client.get("/api/tokens").json()
    assert listed["mcp_enabled"] is True
    assert listed["endpoint_path"] == "/mcp"
    by_name = {t["name"]: t for t in listed["tools"]}
    assert by_name["get_meeting_transcript"]["write"] is False
    assert by_name["create_note"]["write"] is True


def test_scope_and_expiry(user_client):
    made = create(user_client, scope="read_write", expires_in_days=30)
    assert made["scope"] == "read_write"
    expires = datetime.fromisoformat(made["expires_at"])
    assert timedelta(days=29) < expires - datetime.now(timezone.utc) <= timedelta(days=30)


@pytest.mark.parametrize(
    "payload",
    [
        {"name": ""},
        {"name": "x" * 101},
        {"name": "ok", "scope": "admin"},
        {"name": "ok", "expires_in_days": 0},
    ],
)
def test_validation(user_client, payload):
    assert user_client.post("/api/tokens", json=payload).status_code in (400, 422)


def test_revoke_keeps_the_row_and_is_owner_only(user_client, other_user_client):
    made = create(user_client)
    # Someone else's token is a 404, not a 403.
    assert other_user_client.delete(f"/api/tokens/{made['id']}").status_code == 404
    resp = user_client.delete(f"/api/tokens/{made['id']}")
    assert resp.status_code == 200
    assert resp.json()["state"] == "revoked"
    # Revoking twice is harmless and keeps the first timestamp.
    again = user_client.delete(f"/api/tokens/{made['id']}").json()
    assert again["revoked_at"] == resp.json()["revoked_at"]
    assert user_client.get("/api/tokens").json()["tokens"][0]["state"] == "revoked"


def test_tokens_are_private_even_from_admins(user_client, admin_client):
    create(user_client)
    assert admin_client.get("/api/tokens").json()["tokens"] == []


def test_the_rest_api_never_accepts_an_api_token(user_client, client):
    """REST routes know nothing about scope, so a read token there would be a
    write token. And a token must not be able to mint another token."""
    raw = create(user_client)["token"]
    bearer = {"Authorization": f"Bearer {raw}"}
    assert client.get("/api/threads", headers=bearer).status_code == 401
    assert client.post("/api/tokens", json={"name": "child"}, headers=bearer).status_code == 401


def test_resolve_rejects_revoked_expired_and_inactive(isolated_settings, user_client, admin_client):
    raw = create(user_client)["token"]
    with get_conn(isolated_settings.db_path) as conn:
        assert tokens_svc.resolve_token(conn, raw) is not None
        assert tokens_svc.resolve_token(conn, raw + "x") is None
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        conn.execute("UPDATE api_tokens SET expires_at = ?", (past,))
        assert tokens_svc.resolve_token(conn, raw) is None
        conn.execute("UPDATE api_tokens SET expires_at = NULL, revoked_at = ?", (past,))
        assert tokens_svc.resolve_token(conn, raw) is None
        conn.execute("UPDATE api_tokens SET revoked_at = NULL")
        conn.execute("UPDATE users SET is_active = 0 WHERE username = 'alice'")
        assert tokens_svc.resolve_token(conn, raw) is None


def test_touch_is_throttled(isolated_settings, user_client):
    raw = create(user_client)["token"]
    with get_conn(isolated_settings.db_path) as conn:
        token, _ = tokens_svc.resolve_token(conn, raw)
        tokens_svc.touch_token(conn, token)
        first = conn.execute("SELECT last_used_at FROM api_tokens").fetchone()[0]
        assert first is not None
        token, _ = tokens_svc.resolve_token(conn, raw)
        tokens_svc.touch_token(conn, token)
        assert conn.execute("SELECT last_used_at FROM api_tokens").fetchone()[0] == first
