"""Shared fixtures.

Every test gets its own tmp data dir and database. The settings cache is cleared
around each test so environment monkeypatching actually takes effect.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import get_settings, reset_settings_cache
from app.db import get_conn, init_db
from app.services import secretstore

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Point the app at a throwaway data dir and reset the Settings cache."""
    monkeypatch.setenv("MMN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MMN_JOB_CONCURRENCY", "0")
    monkeypatch.setenv("MMN_MCP_CALENDAR_TOKEN", "test-calendar-token")
    monkeypatch.setenv("MMN_MCP_EMAIL_TOKEN", "test-email-token")
    monkeypatch.setenv("MMN_LLM_API_KEY", "test-llm-key")
    # Pinned so no test generates (and leaves behind) a data/secret.key, and so
    # the key does not change between tests that share encrypted fixtures.
    monkeypatch.setenv("MMN_SECRET_KEY", "test-suite-credential-encryption-key")
    reset_settings_cache()
    secretstore.reset_key_cache()
    yield get_settings()
    secretstore.reset_key_cache()
    reset_settings_cache()


@pytest.fixture
def db_path(isolated_settings) -> Path:
    return isolated_settings.db_path


@pytest.fixture
def initialised_db(db_path) -> Path:
    init_db(db_path)
    return db_path


@pytest.fixture
def conn(initialised_db):
    with get_conn(initialised_db) as c:
        yield c


@pytest.fixture
def sample_diarization() -> dict:
    return json.loads((FIXTURES / "diarization_sample.json").read_text())


# --------------------------------------------------------------------------- #
# App + authenticated clients
# --------------------------------------------------------------------------- #

# Long enough to satisfy the 10-char minimum without being noise in assertions.
ADMIN_PASSWORD = "admin-password-1"
USER_PASSWORD = "user-password-1"


@pytest.fixture
def app(isolated_settings):
    from app.main import create_app

    return create_app()


@pytest.fixture
def client(app):
    """Unauthenticated client. Running the lifespan seeds admin/password."""
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


def _login(client, username: str, password: str):
    resp = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return resp


@pytest.fixture
def admin_client(client, isolated_settings):
    """Admin whose forced password change is already done."""
    _login(client, "admin", isolated_settings.bootstrap_admin_password)
    resp = client.post(
        "/api/auth/change-password",
        json={
            "current_password": isolated_settings.bootstrap_admin_password,
            "new_password": ADMIN_PASSWORD,
        },
    )
    assert resp.status_code == 200, resp.text
    ADMIN_TOKEN_HOLDER["token"] = client.cookies.get("mmn_session")
    return client


class BearerClient:
    """A view of the shared TestClient authenticated as a different user.

    Deliberately not a second TestClient: one created outside a `with` block
    spins up its own anyio portal, so its requests run on a different event
    loop from the background job workers. An `asyncio.Queue.put` from that loop
    never wakes a worker awaiting on the other one, and jobs sit queued forever.
    """

    def __init__(self, client, token: str):
        self._client = client
        self._auth = {"Authorization": f"Bearer {token}"}

    def _kw(self, kwargs: dict) -> dict:
        headers = dict(kwargs.pop("headers", None) or {})
        headers.update(self._auth)
        kwargs["headers"] = headers
        # The shared jar still holds the admin's cookie, but the Authorization
        # header takes precedence in deps._token_from_request, so identity here
        # is unambiguous.
        return kwargs

    def get(self, url, **kw):
        return self._client.get(url, **self._kw(kw))

    def post(self, url, **kw):
        return self._client.post(url, **self._kw(kw))

    def patch(self, url, **kw):
        return self._client.patch(url, **self._kw(kw))

    def put(self, url, **kw):
        return self._client.put(url, **self._kw(kw))

    def delete(self, url, **kw):
        return self._client.delete(url, **self._kw(kw))

    @property
    def app(self):
        return self._client.app


@pytest.fixture
def make_user(client, admin_client):
    """Create a user and return ``(user, client_authenticated_as_them)``."""

    def _make(username: str, *, is_admin: bool = False):
        resp = admin_client.post(
            "/api/users",
            json={
                "username": username,
                "password": USER_PASSWORD,
                "display_name": username.title(),
                "is_admin": is_admin,
            },
        )
        assert resp.status_code == 201, resp.text
        user = resp.json()

        login = client.post(
            "/api/auth/login",
            json={"username": username, "password": USER_PASSWORD},
        )
        assert login.status_code == 200, login.text
        token = login.cookies.get("mmn_session")
        # Logging in on the shared client overwrote the admin's cookie.
        client.cookies.clear()
        client.cookies.set("mmn_session", ADMIN_TOKEN_HOLDER["token"])

        as_user = BearerClient(client, token)
        as_user.post(
            "/api/auth/change-password",
            json={
                "current_password": USER_PASSWORD,
                "new_password": f"{username}-pw-12345",
            },
        )
        return user, as_user

    return _make


# Remembers the admin's raw token so a user login on the shared client can
# restore it afterwards.
ADMIN_TOKEN_HOLDER: dict[str, str] = {}


@pytest.fixture
def user_client(make_user):
    _, c = make_user("alice")
    return c


@pytest.fixture
def other_user_client(make_user):
    _, c = make_user("bob")
    return c
