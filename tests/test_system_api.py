"""Health, version, the SPA catch-all, and the model-dropdown endpoints."""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(isolated_settings):
    with TestClient(create_app()) as c:
        yield c


def test_health_reports_db_and_ffmpeg(client):
    body = client.get("/api/health").json()

    assert body["db"]["ok"] is True
    assert body["status"] in {"ok", "degraded"}
    # ffmpeg is present on this host and in the runtime image; None means a broken
    # image, which is exactly what this field exists to surface.
    assert "ffmpeg" in body
    assert "workers" in body


def test_health_is_public(client):
    assert client.get("/api/health").status_code == 200


def test_version_endpoint(client):
    body = client.get("/api/version").json()
    assert "hash" in body
    assert "timestamp" in body


def test_lifespan_creates_data_directories(client, isolated_settings):
    assert isolated_settings.data_dir.is_dir()
    assert isolated_settings.audio_dir.is_dir()
    assert isolated_settings.db_path.exists()


def test_lifespan_seeds_no_shared_server_config(client, isolated_settings):
    """Nothing is connected on a fresh install any more.

    Calendars and inboxes are per-user, so a new deployment starts empty and each
    person connects their own in Settings -- rather than everyone inheriting one
    account seeded from the environment.
    """
    from app.db import get_conn

    with get_conn(isolated_settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM mcp_servers").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM integrations").fetchone()[0] == 0


def test_unknown_api_route_returns_json_not_the_spa(client):
    resp = client.get("/api/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_spa_catchall_serves_index_when_built(client, isolated_settings, monkeypatch):
    dist = isolated_settings.data_dir / "fake-dist"
    (dist).mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text("<!doctype html><title>app</title>")

    # rebuild the app so the catch-all closes over the populated dist
    monkeypatch.setenv("MMN_WEB_DIST", str(dist))
    from app.config import reset_settings_cache

    reset_settings_cache()

    with TestClient(create_app()) as c:
        resp = c.get("/threads/123")
        assert resp.status_code == 200
        assert "<!doctype html>" in resp.text
        assert resp.headers["cache-control"] == "no-store"


def test_spa_catchall_explains_itself_when_not_built(client, isolated_settings, monkeypatch):
    monkeypatch.setenv("MMN_WEB_DIST", str(isolated_settings.data_dir / "nothing-here"))
    from app.config import reset_settings_cache

    reset_settings_cache()

    with TestClient(create_app()) as c:
        resp = c.get("/")
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "spa_not_built"


# --------------------------------------------------------------------------- #
# Model dropdowns -- /api/diarization/models and, since each live-caption
# backend got its own url/api_key (see config.RUNTIME_KEYS), the per-backend
# /api/live-caption/models/{backend}.
# --------------------------------------------------------------------------- #


@respx.mock
def test_diarization_models_no_longer_carries_the_live_stt_synthetic_entry(admin_client):
    """Used to always append a synthetic realtime_eou_120m-v1 option for the
    Live Caption panel's benefit, back when it shared this dropdown -- Live
    Captions has its own per-backend dropdown now, so this must return
    exactly what the diarization service itself reports."""
    admin_client.put("/api/settings", json={"values": {"diarization_url": "http://diarizer.test/v1/audio/diarization"}})
    respx.get("http://diarizer.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "vibevoice-cpp-asr"}]})
    )

    body = admin_client.get("/api/diarization/models").json()

    assert [m["id"] for m in body["models"]] == ["vibevoice-cpp-asr"]


@respx.mock
def test_live_caption_models_uses_the_realtime_backends_own_url(admin_client):
    admin_client.put(
        "/api/settings",
        json={"values": {"live_caption_realtime_url": "http://realtime.test/v1/audio/diarization"}},
    )
    respx.get("http://realtime.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "lfm2.5-audio-1.5b-realtime"}]})
    )

    body = admin_client.get("/api/live-caption/models/realtime").json()

    assert [m["id"] for m in body["models"]] == ["lfm2.5-audio-1.5b-realtime"]


@respx.mock
def test_live_caption_models_uses_the_transcriptions_backends_own_url(admin_client):
    admin_client.put(
        "/api/settings",
        json={
            "values": {
                "live_caption_transcriptions_url": "http://transcriptions.test/v1/audio/transcriptions"
            }
        },
    )
    respx.get("http://transcriptions.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "whisper-large-turbo-q8_0"}]})
    )

    body = admin_client.get("/api/live-caption/models/transcriptions").json()

    assert [m["id"] for m in body["models"]] == ["whisper-large-turbo-q8_0"]


def test_live_caption_models_for_live_stt_is_a_static_suggestion_list_not_a_network_call(admin_client):
    """live_stt is a gRPC target with no HTTP /v1/models to hit -- this must
    not attempt one, even with respx not active to catch a stray request."""
    body = admin_client.get("/api/live-caption/models/live_stt").json()

    assert {m["id"] for m in body["models"]} == {"realtime_eou_120m-v1", "nemotron-3.5-asr-streaming-0.6b"}
    assert body["error"] is None


def test_live_caption_models_rejects_an_unknown_backend(admin_client):
    resp = admin_client.get("/api/live-caption/models/bogus")
    assert resp.status_code == 400


def test_live_caption_models_requires_login(client):
    assert client.get("/api/live-caption/models/realtime").status_code == 401
