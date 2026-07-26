"""Health, version and the SPA catch-all."""

from __future__ import annotations

import pytest
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


def test_lifespan_seeds_mcp_servers(client, isolated_settings):
    from app.db import get_conn

    with get_conn(isolated_settings.db_path) as conn:
        names = {r["name"] for r in conn.execute("SELECT name FROM mcp_servers")}
    assert names == {"calendar", "email"}


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
