"""Admin user management."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import USER_PASSWORD

ADMIN_ROUTES = [
    ("get", "/api/users", None),
    ("post", "/api/users", {"username": "x", "password": "long-enough-pw"}),
    ("patch", "/api/users/1", {"display_name": "hacked"}),
    ("post", "/api/users/1/reset-password", {}),
    ("delete", "/api/users/1", None),
]


@pytest.mark.parametrize("method,path,body", ADMIN_ROUTES)
def test_non_admin_is_forbidden_everywhere(user_client, method, path, body):
    fn = getattr(user_client, method)
    resp = fn(path, json=body) if body is not None else fn(path)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_list_users_is_paginated(admin_client):
    for i in range(5):
        admin_client.post(
            "/api/users",
            json={"username": f"user{i}", "password": "long-enough-pw-1"},
        )

    body = admin_client.get("/api/users", params={"page": 1, "page_size": 2}).json()
    assert len(body["items"]) == 2
    assert body["total"] == 6  # admin + 5
    assert body["total_pages"] == 3
    assert body["page"] == 1


def test_create_user_forces_a_password_change(admin_client):
    resp = admin_client.post(
        "/api/users",
        json={"username": "newbie", "password": USER_PASSWORD, "display_name": "New Bie"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["username"] == "newbie"
    assert body["must_change_password"] is True
    assert body["is_admin"] is False
    assert body["display_name"] == "New Bie"


def test_create_user_rejects_a_duplicate_username(admin_client):
    admin_client.post("/api/users", json={"username": "dup", "password": USER_PASSWORD})
    resp = admin_client.post(
        "/api/users", json={"username": "DUP", "password": USER_PASSWORD}
    )
    assert resp.status_code == 409


def test_create_user_enforces_the_password_policy(admin_client):
    resp = admin_client.post("/api/users", json={"username": "weak", "password": "short"})
    assert resp.status_code == 400


def test_create_user_rejects_a_malformed_username(admin_client):
    resp = admin_client.post(
        "/api/users", json={"username": "has spaces", "password": USER_PASSWORD}
    )
    assert resp.status_code == 422


def test_update_user_changes_display_name_and_role(admin_client):
    created = admin_client.post(
        "/api/users", json={"username": "promote", "password": USER_PASSWORD}
    ).json()

    resp = admin_client.patch(
        f"/api/users/{created['id']}", json={"display_name": "Promoted", "is_admin": True}
    )
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Promoted"
    assert resp.json()["is_admin"] is True


def test_update_unknown_user_is_404(admin_client):
    assert admin_client.patch("/api/users/9999", json={"display_name": "x"}).status_code == 404


# --------------------------------------------------------------------------- #
# Last-admin protection
# --------------------------------------------------------------------------- #


def test_cannot_demote_the_last_admin(admin_client):
    me = admin_client.get("/api/auth/me").json()
    resp = admin_client.patch(f"/api/users/{me['id']}", json={"is_admin": False})
    assert resp.status_code == 409
    assert "last active administrator" in resp.json()["error"]["message"]


def test_cannot_deactivate_the_last_admin(admin_client):
    me = admin_client.get("/api/auth/me").json()
    resp = admin_client.patch(f"/api/users/{me['id']}", json={"is_active": False})
    assert resp.status_code == 409


def test_can_demote_an_admin_when_another_remains(admin_client):
    second = admin_client.post(
        "/api/users",
        json={"username": "admin2", "password": USER_PASSWORD, "is_admin": True},
    ).json()

    resp = admin_client.patch(f"/api/users/{second['id']}", json={"is_admin": False})
    assert resp.status_code == 200
    assert resp.json()["is_admin"] is False


def test_cannot_delete_your_own_account(admin_client):
    me = admin_client.get("/api/auth/me").json()
    resp = admin_client.delete(f"/api/users/{me['id']}")
    assert resp.status_code == 409


# --------------------------------------------------------------------------- #
# Deactivation
# --------------------------------------------------------------------------- #


def test_deactivated_user_cannot_log_in(admin_client, app):
    created = admin_client.post(
        "/api/users", json={"username": "goner", "password": USER_PASSWORD}
    ).json()

    admin_client.patch(f"/api/users/{created['id']}", json={"is_active": False})

    c = TestClient(app)
    resp = c.post("/api/auth/login", json={"username": "goner", "password": USER_PASSWORD})
    assert resp.status_code == 401


def test_deactivating_a_user_kills_their_live_session(admin_client, make_user):
    user, client = make_user("livewire")
    assert client.get("/api/auth/me").status_code == 200

    admin_client.patch(f"/api/users/{user['id']}", json={"is_active": False})

    assert client.get("/api/auth/me").status_code == 401


def test_delete_is_a_soft_deactivation(admin_client, isolated_settings):
    created = admin_client.post(
        "/api/users", json={"username": "softy", "password": USER_PASSWORD}
    ).json()

    resp = admin_client.delete(f"/api/users/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["deactivated"] is True

    # The row survives -- threads reference owner_id, and a hard delete would
    # either orphan them or destroy someone's recordings.
    from app.db import get_conn

    with get_conn(isolated_settings.db_path) as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (created["id"],)).fetchone()
    assert row is not None
    assert row["is_active"] == 0


def test_delete_reports_how_many_threads_would_be_affected(admin_client, make_user):
    user, client = make_user("owner")
    body = admin_client.delete(f"/api/users/{user['id']}").json()
    assert body["owned_threads"] == 0


# --------------------------------------------------------------------------- #
# Password reset
# --------------------------------------------------------------------------- #


def test_reset_password_generates_and_returns_a_temporary_password_once(admin_client, app):
    created = admin_client.post(
        "/api/users", json={"username": "forgot", "password": USER_PASSWORD}
    ).json()

    resp = admin_client.post(f"/api/users/{created['id']}/reset-password", json={})
    assert resp.status_code == 200
    body = resp.json()
    temp = body["temporary_password"]
    assert temp
    assert body["user"]["must_change_password"] is True

    c = TestClient(app)
    login = c.post("/api/auth/login", json={"username": "forgot", "password": temp})
    assert login.status_code == 200
    assert login.json()["must_change_password"] is True


def test_reset_password_accepts_an_explicit_password(admin_client, app):
    created = admin_client.post(
        "/api/users", json={"username": "explicit", "password": USER_PASSWORD}
    ).json()

    resp = admin_client.post(
        f"/api/users/{created['id']}/reset-password",
        json={"new_password": "chosen-by-admin-1"},
    )
    assert resp.status_code == 200
    # Nothing to reveal when the admin picked it themselves.
    assert resp.json()["temporary_password"] is None

    c = TestClient(app)
    assert (
        c.post(
            "/api/auth/login",
            json={"username": "explicit", "password": "chosen-by-admin-1"},
        ).status_code
        == 200
    )


def test_reset_password_enforces_the_policy(admin_client):
    created = admin_client.post(
        "/api/users", json={"username": "weakreset", "password": USER_PASSWORD}
    ).json()
    resp = admin_client.post(
        f"/api/users/{created['id']}/reset-password", json={"new_password": "tiny"}
    )
    assert resp.status_code == 400


def test_reset_password_revokes_existing_sessions(admin_client, make_user):
    user, client = make_user("compromised")
    assert client.get("/api/auth/me").status_code == 200

    admin_client.post(f"/api/users/{user['id']}/reset-password", json={})

    assert client.get("/api/auth/me").status_code == 401
