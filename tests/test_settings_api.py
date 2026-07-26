"""Settings, prompt editing and MCP configuration endpoints."""

from __future__ import annotations

import pytest

from app.db import get_conn


# --------------------------------------------------------------------------- #
# App settings
# --------------------------------------------------------------------------- #


def test_settings_list_every_runtime_key(user_client):
    body = user_client.get("/api/settings").json()["settings"]
    for key in ("llm_base_url", "llm_model", "diarization_url", "match_max_candidates"):
        assert key in body


def test_secrets_are_masked_on_read(user_client):
    body = user_client.get("/api/settings").json()["settings"]
    assert body["llm_api_key"]["is_secret"] is True
    value = body["llm_api_key"]["value"]
    assert value is None or value.startswith("••••")
    assert "test-llm-key" not in (value or "")


def test_non_secrets_are_shown_in_full(user_client):
    body = user_client.get("/api/settings").json()["settings"]
    assert body["llm_model"]["value"]
    assert body["llm_model"]["is_secret"] is False


def test_updating_a_setting_takes_effect(admin_client):
    resp = admin_client.put(
        "/api/settings", json={"values": {"llm_model": "changed/model"}}
    )
    assert resp.status_code == 200
    assert resp.json()["updated"] == ["llm_model"]

    body = admin_client.get("/api/settings").json()["settings"]
    assert body["llm_model"]["value"] == "changed/model"
    assert body["llm_model"]["overridden"] is True


def test_types_survive_the_round_trip(admin_client):
    admin_client.put(
        "/api/settings",
        json={
            "values": {
                "llm_timeout_sec": 120,
                "llm_temperature": 0.7,
                "llm_ssl_verify": False,
            }
        },
    )
    body = admin_client.get("/api/settings").json()["settings"]
    assert body["llm_timeout_sec"]["value"] == 120
    assert body["llm_temperature"]["value"] == 0.7
    assert body["llm_ssl_verify"]["value"] is False


def test_a_masked_secret_sent_back_unchanged_is_ignored(admin_client, isolated_settings):
    """The UI round-trips what it was shown; that must not overwrite the key."""
    admin_client.put("/api/settings", json={"values": {"llm_api_key": "sk-real-key"}})

    shown = admin_client.get("/api/settings").json()["settings"]["llm_api_key"]["value"]
    admin_client.put("/api/settings", json={"values": {"llm_api_key": shown}})

    with get_conn(isolated_settings.db_path) as conn:
        stored = conn.execute(
            "SELECT value FROM app_settings WHERE key = 'llm_api_key'"
        ).fetchone()[0]
    assert stored == "sk-real-key"


def test_clearing_a_setting_falls_back_to_the_env_default(admin_client, isolated_settings):
    admin_client.put("/api/settings", json={"values": {"llm_model": "temp/model"}})
    admin_client.put("/api/settings", json={"values": {"llm_model": ""}})

    body = admin_client.get("/api/settings").json()["settings"]
    assert body["llm_model"]["value"] == isolated_settings.llm_model


def test_unknown_keys_are_rejected(admin_client):
    resp = admin_client.put("/api/settings", json={"values": {"rm_rf_slash": "yes"}})
    assert resp.status_code == 400
    assert "Unknown setting" in resp.json()["error"]["message"]


def test_only_admins_can_write_settings(user_client):
    assert user_client.put(
        "/api/settings", json={"values": {"llm_model": "x"}}
    ).status_code == 403


def test_any_user_can_read_settings(user_client):
    assert user_client.get("/api/settings").status_code == 200


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #


def test_prompts_are_listed(user_client):
    names = [p["name"] for p in user_client.get("/api/prompts").json()]
    assert "summary_prompt" in names


def test_a_prompt_can_be_read_in_full(user_client):
    body = user_client.get("/api/prompts/summary_prompt").json()
    assert "{{transcript}}" in body["body"]
    assert body["system"]
    assert body["required_placeholders"] == ["transcript"]
    assert len(body["sha256"]) == 64


def test_an_unknown_prompt_is_404(user_client):
    assert user_client.get("/api/prompts/nope").status_code == 404


def test_only_admins_can_edit_a_prompt(user_client):
    body = user_client.get("/api/prompts/summary_prompt").json()["body"]
    assert user_client.put(
        "/api/prompts/summary_prompt", json={"body": body}
    ).status_code == 403


@pytest.fixture
def restore_prompt():
    """Edit the real shipped file, then put it back."""
    from app.services import prompts as prompts_svc

    original = prompts_svc.load("summary_prompt").body
    yield
    path = prompts_svc.PROMPT_DIR / "summary_prompt.md"
    path.write_text(original, encoding="utf-8")
    backup = prompts_svc.PROMPT_DIR / "summary_prompt.md.bak"
    backup.unlink(missing_ok=True)


def test_editing_a_prompt_changes_its_hash(admin_client, restore_prompt):
    before = admin_client.get("/api/prompts/summary_prompt").json()
    edited = before["body"].replace("version: 1", "version: 2")

    resp = admin_client.put("/api/prompts/summary_prompt", json={"body": edited})
    assert resp.status_code == 200
    assert resp.json()["sha256"] != before["sha256"]
    assert resp.json()["version"] == "2"


def test_removing_the_transcript_placeholder_is_refused(admin_client, restore_prompt):
    before = admin_client.get("/api/prompts/summary_prompt").json()
    broken = before["body"].replace("{{transcript}}", "")

    resp = admin_client.put("/api/prompts/summary_prompt", json={"body": broken})
    assert resp.status_code == 400
    assert "transcript" in resp.json()["error"]["message"]

    # The good version must survive the rejected save.
    after = admin_client.get("/api/prompts/summary_prompt").json()
    assert after["sha256"] == before["sha256"]


# --------------------------------------------------------------------------- #
# MCP servers
# --------------------------------------------------------------------------- #


def test_both_servers_are_listed(user_client):
    servers = {s["name"]: s for s in user_client.get("/api/mcp/servers").json()}
    assert set(servers) == {"calendar", "email"}
    assert servers["calendar"]["transport"] == "sse"
    assert servers["calendar"]["tool_name"] == "search_events"
    assert servers["email"]["tool_name"] == "search_emails"


def test_tokens_are_masked_but_presence_is_reported(user_client):
    servers = {s["name"]: s for s in user_client.get("/api/mcp/servers").json()}
    calendar = servers["calendar"]

    assert calendar["has_token"] is True
    assert calendar["auth_token"].startswith("••••")
    assert "test-calendar-token" not in calendar["auth_token"]


def test_updating_a_server(admin_client):
    resp = admin_client.put(
        "/api/mcp/servers/calendar",
        json={"base_url": "http://test-host.internal.example:4099", "default_profile": "work"},
    )
    assert resp.status_code == 200
    assert resp.json()["base_url"] == "http://test-host.internal.example:4099"
    assert resp.json()["default_profile"] == "work"


def test_switching_a_server_to_stdio(admin_client):
    resp = admin_client.put(
        "/api/mcp/servers/calendar",
        json={
            "transport": "stdio",
            "command": "/path/to/venv/bin/python",
            "args": ["mcp_server.py"],
            "cwd": "/path/to/calendarmcp",
            "env": {"CALENDAR_MCP_TRANSPORT": "stdio"},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["transport"] == "stdio"
    assert body["args"] == ["mcp_server.py"]
    assert body["env"] == {"CALENDAR_MCP_TRANSPORT": "stdio"}


def test_a_masked_token_sent_back_does_not_clobber_the_real_one(
    admin_client, isolated_settings
):
    shown = next(
        s for s in admin_client.get("/api/mcp/servers").json() if s["name"] == "calendar"
    )["auth_token"]

    admin_client.put("/api/mcp/servers/calendar", json={"auth_token": shown})

    with get_conn(isolated_settings.db_path) as conn:
        stored = conn.execute(
            "SELECT auth_token FROM mcp_servers WHERE name = 'calendar'"
        ).fetchone()[0]
    assert stored == "test-calendar-token"


def test_a_new_token_is_saved(admin_client, isolated_settings):
    admin_client.put("/api/mcp/servers/email", json={"auth_token": "brand-new-token"})

    with get_conn(isolated_settings.db_path) as conn:
        stored = conn.execute(
            "SELECT auth_token FROM mcp_servers WHERE name = 'email'"
        ).fetchone()[0]
    assert stored == "brand-new-token"


def test_an_invalid_transport_is_rejected(admin_client):
    resp = admin_client.put("/api/mcp/servers/calendar", json={"transport": "smoke-signal"})
    assert resp.status_code == 422


def test_updating_an_unknown_server_is_404(admin_client):
    assert admin_client.put("/api/mcp/servers/slack", json={"enabled": False}).status_code == 404


def test_only_admins_can_change_mcp_config(user_client):
    assert user_client.put(
        "/api/mcp/servers/calendar", json={"enabled": False}
    ).status_code == 403
    assert user_client.post("/api/mcp/servers/calendar/test").status_code == 403


def test_test_connection_records_its_result(admin_client, isolated_settings, monkeypatch):
    async def fake_test(self):
        return {"ok": True, "latency_ms": 42, "tools": ["search_events"], "error": None}

    from app.services import mcpclient as mcp_svc

    monkeypatch.setattr(mcp_svc.MCPClient, "test", fake_test)

    resp = admin_client.post("/api/mcp/servers/calendar/test")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    listed = next(
        s for s in admin_client.get("/api/mcp/servers").json() if s["name"] == "calendar"
    )
    assert listed["last_test"]["ok"] is True
    assert listed["last_test"]["tools"] == ["search_events"]
    assert listed["last_test"]["at"]


def test_a_failed_test_is_recorded_with_its_error(admin_client, monkeypatch):
    async def fake_test(self):
        return {"ok": False, "latency_ms": 8, "tools": [], "error": "rejected the token (401)"}

    from app.services import mcpclient as mcp_svc

    monkeypatch.setattr(mcp_svc.MCPClient, "test", fake_test)

    admin_client.post("/api/mcp/servers/email/test")

    listed = next(
        s for s in admin_client.get("/api/mcp/servers").json() if s["name"] == "email"
    )
    assert listed["last_test"]["ok"] is False
    assert "401" in listed["last_test"]["error"]


def test_test_can_use_unsaved_edits(admin_client, monkeypatch):
    """So the form can verify a token before committing it."""
    seen = {}

    async def fake_test(self):
        seen["base_url"] = self.config.base_url
        seen["token"] = self.config.auth_token
        return {"ok": True, "latency_ms": 1, "tools": ["search_events"], "error": None}

    from app.services import mcpclient as mcp_svc

    monkeypatch.setattr(mcp_svc.MCPClient, "test", fake_test)

    admin_client.post(
        "/api/mcp/servers/calendar/test",
        json={"base_url": "http://unsaved.test:9999", "auth_token": "unsaved-token"},
    )

    assert seen["base_url"] == "http://unsaved.test:9999"
    assert seen["token"] == "unsaved-token"

    # ...and the unsaved values must not have been persisted.
    listed = next(
        s for s in admin_client.get("/api/mcp/servers").json() if s["name"] == "calendar"
    )
    assert listed["base_url"] != "http://unsaved.test:9999"
