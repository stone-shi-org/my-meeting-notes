"""Login, session cookies, and the forced password-change gate."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.db import get_conn
from app.security import hash_token
from app.services.users import SLIDING_REFRESH_SECONDS


# --------------------------------------------------------------------------- #
# Bootstrap
# --------------------------------------------------------------------------- #


def test_first_boot_seeds_admin_forced_to_change_password(client, isolated_settings):
    resp = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": isolated_settings.bootstrap_admin_password},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["username"] == "admin"
    assert body["user"]["is_admin"] is True
    assert body["must_change_password"] is True


def test_seed_admin_does_not_reset_an_existing_admin(client, isolated_settings, app):
    """A restart must not undo a password change or re-raise the forced flag."""
    from app.services import users as users_svc

    client.post(
        "/api/auth/login",
        json={"username": "admin", "password": isolated_settings.bootstrap_admin_password},
    )
    client.post(
        "/api/auth/change-password",
        json={
            "current_password": isolated_settings.bootstrap_admin_password,
            "new_password": "brand-new-password",
        },
    )

    with get_conn(isolated_settings.db_path) as conn:
        assert users_svc.seed_admin(conn) is False

    fresh = TestClient(app)
    resp = fresh.post(
        "/api/auth/login",
        json={"username": "admin", "password": isolated_settings.bootstrap_admin_password},
    )
    assert resp.status_code == 401

    resp = fresh.post(
        "/api/auth/login", json={"username": "admin", "password": "brand-new-password"}
    )
    assert resp.status_code == 200
    assert resp.json()["must_change_password"] is False


# --------------------------------------------------------------------------- #
# Login / logout
# --------------------------------------------------------------------------- #


def test_login_sets_an_httponly_cookie(client, isolated_settings):
    resp = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": isolated_settings.bootstrap_admin_password},
    )
    cookie_header = resp.headers.get("set-cookie", "")
    assert "mmn_session=" in cookie_header
    assert "HttpOnly" in cookie_header
    assert "SameSite=lax" in cookie_header.replace("samesite", "SameSite")
    # Plain HTTP on the LAN: Secure would stop the browser sending it back.
    assert "Secure" not in cookie_header


def test_secure_cookie_flag_follows_configuration(app, monkeypatch, isolated_settings):
    monkeypatch.setenv("MMN_SESSION_COOKIE_SECURE", "true")
    from app.config import reset_settings_cache
    from app.main import create_app

    reset_settings_cache()
    with TestClient(create_app()) as c:
        resp = c.post(
            "/api/auth/login",
            json={"username": "admin", "password": isolated_settings.bootstrap_admin_password},
        )
        assert "Secure" in resp.headers.get("set-cookie", "")


def test_login_rejects_a_bad_password(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "auth_required"


def test_login_rejects_an_unknown_user(client):
    resp = client.post("/api/auth/login", json={"username": "ghost", "password": "nope"})
    assert resp.status_code == 401


def test_login_is_case_insensitive_on_username(client, isolated_settings):
    resp = client.post(
        "/api/auth/login",
        json={"username": "ADMIN", "password": isolated_settings.bootstrap_admin_password},
    )
    assert resp.status_code == 200


def test_logout_deletes_the_session_row(admin_client, isolated_settings):
    with get_conn(isolated_settings.db_path) as conn:
        before = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert before == 1

    assert admin_client.post("/api/auth/logout").status_code == 200

    with get_conn(isolated_settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0

    assert admin_client.get("/api/auth/me").status_code == 401


def test_me_requires_authentication(client):
    assert client.get("/api/auth/me").status_code == 401


def test_me_returns_the_current_user(admin_client):
    body = admin_client.get("/api/auth/me").json()
    assert body["username"] == "admin"
    assert body["is_admin"] is True
    assert body["must_change_password"] is False


# --------------------------------------------------------------------------- #
# The forced password-change gate
# --------------------------------------------------------------------------- #


GATED_ROUTES = [
    ("get", "/api/users"),
    ("get", "/api/auth/sessions"),
]


@pytest.fixture
def forced_client(client, isolated_settings):
    """Logged in as admin with the change still pending."""
    client.post(
        "/api/auth/login",
        json={"username": "admin", "password": isolated_settings.bootstrap_admin_password},
    )
    return client


@pytest.mark.parametrize("method,path", GATED_ROUTES)
def test_gated_routes_409_until_the_password_is_changed(forced_client, method, path):
    resp = getattr(forced_client, method)(path)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "password_change_required"


def test_the_three_escape_hatch_routes_stay_open(forced_client, isolated_settings):
    """A forced user must be able to see who they are, change, or leave."""
    assert forced_client.get("/api/auth/me").status_code == 200

    resp = forced_client.post(
        "/api/auth/change-password",
        json={
            "current_password": isolated_settings.bootstrap_admin_password,
            "new_password": "a-fresh-password",
        },
    )
    assert resp.status_code == 200
    assert forced_client.post("/api/auth/logout").status_code == 200


def test_changing_the_password_clears_the_gate(forced_client, isolated_settings):
    assert forced_client.get("/api/users").status_code == 409

    forced_client.post(
        "/api/auth/change-password",
        json={
            "current_password": isolated_settings.bootstrap_admin_password,
            "new_password": "a-fresh-password",
        },
    )
    assert forced_client.get("/api/users").status_code == 200


# --------------------------------------------------------------------------- #
# Change password
# --------------------------------------------------------------------------- #


def test_change_password_requires_the_correct_current_password(admin_client):
    resp = admin_client.post(
        "/api/auth/change-password",
        json={"current_password": "wrong", "new_password": "another-password"},
    )
    assert resp.status_code == 400
    assert "incorrect" in resp.json()["error"]["message"].lower()


def test_change_password_enforces_the_minimum_length(admin_client):
    from tests.conftest import ADMIN_PASSWORD

    resp = admin_client.post(
        "/api/auth/change-password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "short"},
    )
    assert resp.status_code == 400
    assert "at least" in resp.json()["error"]["message"]


def test_change_password_rejects_reusing_the_same_password(admin_client):
    from tests.conftest import ADMIN_PASSWORD

    resp = admin_client.post(
        "/api/auth/change-password",
        json={"current_password": ADMIN_PASSWORD, "new_password": ADMIN_PASSWORD},
    )
    assert resp.status_code == 400
    assert "different" in resp.json()["error"]["message"]


def test_change_password_revokes_other_sessions_but_keeps_this_one(
    admin_client, app, isolated_settings
):
    from tests.conftest import ADMIN_PASSWORD

    # A second browser for the same account.
    second = TestClient(app)
    second.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD})
    assert second.get("/api/auth/me").status_code == 200

    resp = admin_client.post(
        "/api/auth/change-password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "yet-another-password"},
    )
    assert resp.status_code == 200
    assert resp.json()["revoked_sessions"] == 1

    assert admin_client.get("/api/auth/me").status_code == 200
    assert second.get("/api/auth/me").status_code == 401


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #


def test_session_id_is_the_hash_not_the_raw_token(admin_client, isolated_settings):
    raw = admin_client.cookies.get("mmn_session")
    assert raw

    with get_conn(isolated_settings.db_path) as conn:
        stored = conn.execute("SELECT id FROM sessions").fetchone()[0]

    assert stored != raw
    assert stored == hash_token(raw)


def test_bearer_token_is_accepted_so_curl_works(admin_client, app):
    raw = admin_client.cookies.get("mmn_session")

    bare = TestClient(app)
    resp = bare.get("/api/auth/me", headers={"Authorization": f"Bearer {raw}"})
    assert resp.status_code == 200
    assert resp.json()["username"] == "admin"


def test_a_garbage_token_is_rejected(client, app):
    resp = client.get("/api/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401


def test_an_expired_session_is_rejected_and_cleaned_up(admin_client, isolated_settings):
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with get_conn(isolated_settings.db_path) as conn:
        conn.execute("UPDATE sessions SET expires_at = ?", (past,))

    assert admin_client.get("/api/auth/me").status_code == 401

    with get_conn(isolated_settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_sliding_expiry_extends_a_stale_session(admin_client, isolated_settings):
    stale = (
        datetime.now(timezone.utc) - timedelta(seconds=SLIDING_REFRESH_SECONDS + 60)
    ).isoformat()
    with get_conn(isolated_settings.db_path) as conn:
        conn.execute("UPDATE sessions SET last_seen_at = ?", (stale,))
        before = conn.execute("SELECT expires_at FROM sessions").fetchone()[0]

    admin_client.get("/api/auth/me")

    with get_conn(isolated_settings.db_path) as conn:
        after = conn.execute("SELECT expires_at FROM sessions").fetchone()[0]
    assert after > before


def test_sliding_expiry_does_not_write_on_every_request(admin_client, isolated_settings):
    """A write per request would be pointless load for a 14-day window."""
    admin_client.get("/api/auth/me")
    with get_conn(isolated_settings.db_path) as conn:
        first = conn.execute("SELECT last_seen_at, expires_at FROM sessions").fetchone()

    admin_client.get("/api/auth/me")
    with get_conn(isolated_settings.db_path) as conn:
        second = conn.execute("SELECT last_seen_at, expires_at FROM sessions").fetchone()

    assert first["last_seen_at"] == second["last_seen_at"]
    assert first["expires_at"] == second["expires_at"]


def test_listing_sessions_marks_the_current_one(admin_client, app):
    from tests.conftest import ADMIN_PASSWORD

    second = TestClient(app)
    second.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD})

    sessions = admin_client.get("/api/auth/sessions").json()
    assert len(sessions) == 2
    assert sum(1 for s in sessions if s["current"]) == 1


def test_revoking_another_session_logs_it_out(admin_client, app):
    from tests.conftest import ADMIN_PASSWORD

    second = TestClient(app)
    second.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD})

    sessions = admin_client.get("/api/auth/sessions").json()
    other = next(s for s in sessions if not s["current"])

    assert admin_client.delete(f"/api/auth/sessions/{other['id']}").status_code == 200
    assert second.get("/api/auth/me").status_code == 401
    assert admin_client.get("/api/auth/me").status_code == 200


def test_cannot_revoke_someone_elses_session(user_client, other_user_client):
    victim = other_user_client.get("/api/auth/sessions").json()[0]
    resp = user_client.delete(f"/api/auth/sessions/{victim['id']}")
    assert resp.status_code == 404
    assert other_user_client.get("/api/auth/me").status_code == 200
