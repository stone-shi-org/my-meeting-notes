"""Settings resolution: env layer, DB override layer, and type coercion."""

from __future__ import annotations

import pytest

from app.config import RUNTIME_KEYS, Settings, effective, get_settings, reset_settings_cache
from app.db import utcnow


def test_env_vars_map_onto_settings(monkeypatch):
    monkeypatch.setenv("MMN_LLM_MODEL", "some/other-model")
    monkeypatch.setenv("MMN_JOB_CONCURRENCY", "7")
    reset_settings_cache()

    settings = get_settings()
    assert settings.llm_model == "some/other-model"
    assert settings.job_concurrency == 7


def test_derived_paths_hang_off_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MMN_DATA_DIR", str(tmp_path / "d"))
    reset_settings_cache()

    settings = get_settings()
    assert settings.db_path == tmp_path / "d" / "app.db"
    assert settings.audio_dir == tmp_path / "d" / "audio"


def test_effective_falls_back_to_env_when_no_db_row(conn):
    assert effective(conn, "llm_model") == get_settings().llm_model


def test_effective_prefers_db_row(conn):
    conn.execute(
        "INSERT INTO app_settings (key, value, value_type, is_secret, updated_at) "
        "VALUES (?, ?, ?, 0, ?)",
        ("llm_model", "db/override-model", "str", utcnow()),
    )
    assert effective(conn, "llm_model") == "db/override-model"


def test_empty_db_value_falls_back_rather_than_disabling(conn):
    """A cleared Settings field must not silently blank out a required URL."""
    conn.execute(
        "INSERT INTO app_settings (key, value, value_type, is_secret, updated_at) "
        "VALUES (?, ?, ?, 0, ?)",
        ("llm_base_url", "", "str", utcnow()),
    )
    assert effective(conn, "llm_base_url") == get_settings().llm_base_url


@pytest.mark.parametrize(
    "key,stored,expected",
    [
        ("llm_timeout_sec", "120", 120),
        ("llm_temperature", "0.7", 0.7),
        ("llm_ssl_verify", "false", False),
        ("llm_ssl_verify", "TRUE", True),
        ("match_max_candidates", "5", 5),
    ],
)
def test_db_values_are_coerced_to_their_declared_type(conn, key, stored, expected):
    value_type, _ = RUNTIME_KEYS[key]
    conn.execute(
        "INSERT INTO app_settings (key, value, value_type, is_secret, updated_at) "
        "VALUES (?, ?, ?, 0, ?)",
        (key, stored, value_type, utcnow()),
    )
    assert effective(conn, key) == expected


def test_effective_rejects_unknown_key(conn):
    with pytest.raises(KeyError):
        effective(conn, "not_a_real_setting")


def test_effective_without_a_connection_uses_env_only():
    assert effective(None, "llm_model") == get_settings().llm_model


def test_every_runtime_key_exists_on_settings():
    """Guards against a RUNTIME_KEYS entry with no env-layer default behind it."""
    settings = Settings()
    for key in RUNTIME_KEYS:
        assert hasattr(settings, key), f"RUNTIME_KEYS has {key!r} but Settings does not"


def test_secret_keys_are_marked_secret():
    for key in ("llm_api_key", "diarization_api_key"):
        _, is_secret = RUNTIME_KEYS[key]
        assert is_secret is True
