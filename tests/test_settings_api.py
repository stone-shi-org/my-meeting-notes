"""Settings, prompt editing and MCP configuration endpoints."""

from __future__ import annotations

import pytest

from app.db import get_conn


# --------------------------------------------------------------------------- #
# App settings
# --------------------------------------------------------------------------- #


def test_settings_list_every_runtime_key(user_client):
    body = user_client.get("/api/settings").json()["settings"]
    for key in (
        "llm_base_url", "llm_model", "diarization_url", "match_max_candidates",
        "web_search_base_url", "web_search_api_key", "web_search_timeout_sec",
    ):
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


def test_a_masked_web_search_key_sent_back_unchanged_is_ignored(admin_client, isolated_settings):
    admin_client.put("/api/settings", json={"values": {"web_search_api_key": "sk-real-search-key"}})

    shown = admin_client.get("/api/settings").json()["settings"]["web_search_api_key"]["value"]
    admin_client.put("/api/settings", json={"values": {"web_search_api_key": shown}})

    with get_conn(isolated_settings.db_path) as conn:
        stored = conn.execute(
            "SELECT value FROM app_settings WHERE key = 'web_search_api_key'"
        ).fetchone()[0]
    assert stored == "sk-real-search-key"


def test_clearing_a_setting_falls_back_to_the_env_default(admin_client, isolated_settings):
    admin_client.put("/api/settings", json={"values": {"llm_model": "temp/model"}})
    admin_client.put("/api/settings", json={"values": {"llm_model": ""}})

    body = admin_client.get("/api/settings").json()["settings"]
    assert body["llm_model"]["value"] == isolated_settings.llm_model


def test_a_json_list_setting_round_trips(admin_client):
    resp = admin_client.put(
        "/api/settings",
        json={"values": {"llm_chat_models": ["model/a", "model/b"]}},
    )
    assert resp.status_code == 200, resp.text

    body = admin_client.get("/api/settings").json()["settings"]
    assert body["llm_chat_models"]["value"] == ["model/a", "model/b"]
    assert body["llm_chat_models"]["overridden"] is True


def test_a_json_setting_rejects_a_non_list_value(admin_client):
    resp = admin_client.put(
        "/api/settings", json={"values": {"llm_chat_models": "not-a-list"}}
    )
    assert resp.status_code == 400
    assert "llm_chat_models" in resp.json()["error"]["message"]


def test_clearing_a_json_setting_falls_back_to_the_empty_default(admin_client):
    admin_client.put("/api/settings", json={"values": {"llm_chat_models": ["a"]}})
    admin_client.put("/api/settings", json={"values": {"llm_chat_models": None}})

    body = admin_client.get("/api/settings").json()["settings"]
    assert body["llm_chat_models"]["value"] == []


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
# Enabled chat models
# --------------------------------------------------------------------------- #


def test_chat_models_endpoint_always_includes_the_default(user_client):
    body = user_client.get("/api/llm/chat-models").json()
    settings = user_client.get("/api/settings").json()["settings"]
    assert body["models"][0] == settings["llm_model"]["value"]


def test_chat_models_endpoint_includes_configured_extras(admin_client):
    admin_client.put(
        "/api/settings", json={"values": {"llm_chat_models": ["extra/model"]}}
    )
    body = admin_client.get("/api/llm/chat-models").json()
    assert "extra/model" in body["models"]


def test_chat_models_endpoint_requires_login(client):
    assert client.get("/api/llm/chat-models").status_code == 401


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


# MCP server configuration moved to per-user integrations; those endpoints
# are covered by tests/test_integrations_api.py.
