"""The /api/integrations endpoints.

These replace tests/test_user_mcp_profiles.py. The assertions that mattered there
-- per-user isolation and never leaking a credential -- matter just as much here,
so they are carried over rather than dropped.
"""

from __future__ import annotations

import pytest

from app.db import get_conn


def connect_mcp(client, *, profile="stone", base_url="http://mcp.test:4006", **over):
    body = {
        "provider": "mcp_calendar",
        "account_label": over.pop("label", None) or f"calendar ({profile})",
        "config": {
            "transport": "sse",
            "base_url": base_url,
            "tool_name": "search_events",
            "profile": profile,
        },
        "secret": {"auth_token": "a-real-looking-token-1234"},
    }
    body.update(over)
    return client.post("/api/integrations", json=body)


class TestProviderCatalogue:
    def test_it_lists_what_can_be_connected(self, user_client):
        specs = {s["id"]: s for s in user_client.get("/api/integrations/providers").json()}
        assert {"mcp_calendar", "mcp_email"} <= set(specs)
        assert specs["mcp_calendar"]["kinds"] == ["calendar"]
        assert specs["mcp_email"]["kinds"] == ["email"]
        assert specs["mcp_calendar"]["auth_type"] == "token"

    def test_it_needs_a_login(self, client):
        assert client.get("/api/integrations/providers").status_code == 401


class TestCreate:
    def test_connecting_an_account(self, user_client):
        resp = connect_mcp(user_client)
        assert resp.status_code == 201, resp.text

        body = resp.json()
        assert body["provider"] == "mcp_calendar"
        assert body["calendar_enabled"] is True
        assert body["email_enabled"] is False
        assert body["status"] == "unverified"
        assert body["account_key"] == "http://mcp.test:4006:stone"

    def test_the_credential_never_comes_back(self, user_client):
        connect_mcp(user_client)
        listed = user_client.get("/api/integrations").json()[0]

        assert listed["has_secret"] is True
        assert listed["secret_preview"] == "••••1234"
        assert "a-real-looking-token-1234" not in str(listed)
        assert "secret_json" not in listed

    def test_the_same_account_twice_is_a_conflict(self, user_client):
        assert connect_mcp(user_client).status_code == 201
        assert connect_mcp(user_client).status_code == 409

    def test_two_profiles_on_one_server_are_distinct_accounts(self, user_client):
        assert connect_mcp(user_client, profile="stone").status_code == 201
        assert connect_mcp(user_client, profile="jenny").status_code == 201
        assert len(user_client.get("/api/integrations").json()) == 2

    def test_a_server_url_is_required(self, user_client):
        resp = user_client.post(
            "/api/integrations",
            json={"provider": "mcp_calendar", "config": {"profile": "stone"}},
        )
        assert resp.status_code == 400
        assert "URL" in resp.json()["error"]["message"]

    def test_an_unknown_provider_is_rejected(self, user_client):
        resp = user_client.post("/api/integrations", json={"provider": "carrier_pigeon"})
        assert resp.status_code == 404

    def test_a_calendar_provider_cannot_be_asked_for_email(self, user_client):
        resp = connect_mcp(user_client, email_enabled=True)
        assert resp.status_code == 400
        assert "email" in resp.json()["error"]["message"]


class TestIsolation:
    """The core promise: an integration belongs to one user."""

    def test_another_users_integrations_are_invisible(self, user_client, other_user_client):
        connect_mcp(user_client)
        assert user_client.get("/api/integrations").json() != []
        assert other_user_client.get("/api/integrations").json() == []

    def test_another_users_integration_is_404_not_403(self, user_client, other_user_client):
        """A 403 would confirm the row exists."""
        made = connect_mcp(user_client).json()["id"]

        assert other_user_client.patch(
            f"/api/integrations/{made}", json={"account_label": "hijacked"}
        ).status_code == 404
        assert other_user_client.delete(f"/api/integrations/{made}").status_code == 404
        assert other_user_client.post(f"/api/integrations/{made}/test").status_code == 404

    def test_each_user_can_connect_the_same_shared_mailbox(
        self, user_client, other_user_client
    ):
        assert connect_mcp(user_client).status_code == 201
        assert connect_mcp(other_user_client).status_code == 201


class TestUpdate:
    def test_renaming_does_not_change_identity(self, user_client):
        made = connect_mcp(user_client).json()
        renamed = user_client.patch(
            f"/api/integrations/{made['id']}", json={"account_label": "Work calendar"}
        ).json()

        assert renamed["account_label"] == "Work calendar"
        assert renamed["account_key"] == made["account_key"]

    def test_a_capability_can_be_turned_off(self, user_client):
        made = connect_mcp(user_client).json()
        updated = user_client.patch(
            f"/api/integrations/{made['id']}", json={"calendar_enabled": False}
        ).json()
        assert updated["calendar_enabled"] is False

        summary = user_client.get("/api/integrations/summary").json()
        assert summary["calendar"] == 0

    def test_echoing_the_masked_secret_leaves_it_alone(self, user_client):
        """The form round-trips ••••1234; that must not become the token."""
        made = connect_mcp(user_client).json()
        user_client.patch(
            f"/api/integrations/{made['id']}", json={"secret": {"auth_token": "••••1234"}}
        )

        with get_conn() as conn:
            from app.services import secretstore

            # Scoped by id: the lifespan migration also gives the admin account
            # integrations of its own, so an unqualified select is ambiguous.
            stored = conn.execute(
                "SELECT secret_json FROM integrations WHERE id = ?", (made["id"],)
            ).fetchone()[0]
        assert secretstore.decrypt(stored)["auth_token"] == "a-real-looking-token-1234"

    def test_a_new_secret_replaces_the_old_one(self, user_client):
        made = connect_mcp(user_client).json()
        user_client.patch(
            f"/api/integrations/{made['id']}", json={"secret": {"auth_token": "rotated-9876"}}
        )
        listed = user_client.get("/api/integrations").json()[0]
        assert listed["secret_preview"] == "••••9876"

    def test_config_is_merged_not_replaced(self, user_client):
        made = connect_mcp(user_client).json()
        updated = user_client.patch(
            f"/api/integrations/{made['id']}",
            json={"config": {"base_url": "http://moved.test:4006"}},
        ).json()

        assert updated["config"]["base_url"] == "http://moved.test:4006"
        assert updated["config"]["tool_name"] == "search_events"


class TestDelete:
    def test_disconnecting_removes_it(self, user_client):
        made = connect_mcp(user_client).json()
        assert user_client.delete(f"/api/integrations/{made['id']}").status_code == 200
        assert user_client.get("/api/integrations").json() == []


class TestSummary:
    def test_nothing_connected_reads_as_zero(self, user_client):
        """What the SPA greys the match button on."""
        assert user_client.get("/api/integrations/summary").json() == {
            "calendar": 0,
            "email": 0,
            "needs_reauth": [],
        }

    def test_it_counts_each_capability(self, user_client):
        connect_mcp(user_client)
        user_client.post(
            "/api/integrations",
            json={
                "provider": "mcp_email",
                "config": {"base_url": "http://mcp.test:4003", "profile": "stone"},
                "secret": {"auth_token": "tok"},
            },
        )
        summary = user_client.get("/api/integrations/summary").json()
        assert (summary["calendar"], summary["email"]) == (1, 1)

    def test_summary_is_not_mistaken_for_an_integration_id(self, user_client):
        """/summary and /providers are declared before /{id}; if that ordering
        ever regresses this returns 422 trying to parse them as ints."""
        assert user_client.get("/api/integrations/summary").status_code == 200
        assert user_client.get("/api/integrations/providers").status_code == 200


class TestConnectionTest:
    @pytest.fixture
    def fake_mcp(self, monkeypatch):
        from app.services import mcpclient as mcp_svc

        async def ok(self):
            return {"ok": True, "latency_ms": 12, "tools": ["search_events"], "error": None}

        monkeypatch.setattr(mcp_svc.MCPClient, "test", ok)

    def test_a_successful_test_reports_per_check_detail(self, user_client, fake_mcp):
        made = connect_mcp(user_client).json()
        result = user_client.post(f"/api/integrations/{made['id']}/test").json()

        assert result["ok"] is True
        assert result["error"] is None
        # Per-check rather than one flag: a provider can be half-working.
        assert [c["name"] for c in result["checks"]] == ["handshake"]

    def test_a_successful_test_marks_the_account_ok(self, user_client, fake_mcp):
        made = connect_mcp(user_client).json()
        assert made["status"] == "unverified"

        user_client.post(f"/api/integrations/{made['id']}/test")
        listed = user_client.get("/api/integrations").json()[0]
        assert listed["status"] == "ok"
        assert listed["last_test"]["ok"] is True

    def test_a_failure_is_reported_not_raised(self, user_client, monkeypatch):
        from app.errors import MCPError
        from app.services import mcpclient as mcp_svc

        async def boom(self):
            raise MCPError("rejected the token (401)", server="calendar")

        monkeypatch.setattr(mcp_svc.MCPClient, "test", boom)
        made = connect_mcp(user_client).json()

        resp = user_client.post(f"/api/integrations/{made['id']}/test")
        assert resp.status_code == 502
        assert "401" in resp.json()["error"]["message"]

    def test_an_unreadable_credential_says_reconnect(self, user_client):
        """A lost encryption key must produce advice, not a stack trace."""
        import json as jsonlib

        made = connect_mcp(user_client).json()
        with get_conn() as conn:
            conn.execute(
                "UPDATE integrations SET secret_json = ? WHERE id = ?",
                (jsonlib.dumps({"key_id": "deadbeef", "ct": "gAAAAAB-nope"}), made["id"]),
            )

        result = user_client.post(f"/api/integrations/{made['id']}/test").json()
        assert result["ok"] is False
        assert "Reconnect" in result["error"]
