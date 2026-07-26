"""Per-user MCP profile overrides: self-service, admin-on-behalf-of, and the
resolution matching actually uses when gathering candidates."""

from __future__ import annotations

import pytest

from app.db import get_conn, seed_mcp_servers
from app.services import mcpclient as mcp_svc


@pytest.fixture
def conn(initialised_db):
    """Shadows the conftest ``conn`` fixture: these tests need the seeded
    calendar/email rows the app lifespan creates, plus two real users to
    satisfy user_mcp_profiles' foreign key."""
    from app.db import utcnow

    seed_mcp_servers(initialised_db)
    with get_conn(initialised_db) as c:
        now = utcnow()
        c.execute(
            "INSERT INTO users (id, username, password_hash, password_salt, created_at, updated_at) "
            "VALUES (1, 'user-one', 'h', 's', ?, ?), (2, 'user-two', 'h', 's', ?, ?)",
            (now, now, now, now),
        )
        yield c


# --------------------------------------------------------------------------- #
# Service layer
# --------------------------------------------------------------------------- #


class TestResolveTokenUpdate:
    def test_none_leaves_it_alone(self):
        assert mcp_svc.resolve_token_update(None) == (None, False)

    def test_empty_string_clears_it(self):
        assert mcp_svc.resolve_token_update("") == (None, True)

    def test_a_masked_echo_is_treated_as_unchanged(self):
        assert mcp_svc.resolve_token_update("••••oken") == (None, False)

    def test_a_real_value_is_passed_through(self):
        assert mcp_svc.resolve_token_update("brand-new-token") == ("brand-new-token", False)


class TestSetGetDeleteOverride:
    def test_no_override_by_default(self, conn):
        assert mcp_svc.get_user_override(conn, 1, "calendar") is None

    def test_set_then_get(self, conn):
        mcp_svc.set_user_override(conn, 1, "calendar", profile="jenny", auth_token="tok-1")
        row = mcp_svc.get_user_override(conn, 1, "calendar")
        assert row["profile"] == "jenny"
        assert row["auth_token"] == "tok-1"

    def test_updating_the_profile_without_a_token_keeps_the_existing_token(self, conn):
        mcp_svc.set_user_override(conn, 1, "calendar", profile="jenny", auth_token="tok-1")
        mcp_svc.set_user_override(conn, 1, "calendar", profile="jenny-v2", auth_token=None)

        row = mcp_svc.get_user_override(conn, 1, "calendar")
        assert row["profile"] == "jenny-v2"
        assert row["auth_token"] == "tok-1"

    def test_clear_token_reverts_to_no_personal_token(self, conn):
        mcp_svc.set_user_override(conn, 1, "calendar", profile="jenny", auth_token="tok-1")
        mcp_svc.set_user_override(
            conn, 1, "calendar", profile="jenny", auth_token=None, clear_token=True
        )
        row = mcp_svc.get_user_override(conn, 1, "calendar")
        assert row["profile"] == "jenny"
        assert row["auth_token"] is None

    def test_delete_removes_the_row(self, conn):
        mcp_svc.set_user_override(conn, 1, "calendar", profile="jenny")
        assert mcp_svc.delete_user_override(conn, 1, "calendar") is True
        assert mcp_svc.get_user_override(conn, 1, "calendar") is None

    def test_deleting_a_nonexistent_override_is_a_harmless_no_op(self, conn):
        assert mcp_svc.delete_user_override(conn, 1, "calendar") is False

    def test_overrides_are_scoped_per_server(self, conn):
        mcp_svc.set_user_override(conn, 1, "calendar", profile="jenny")
        assert mcp_svc.get_user_override(conn, 1, "email") is None

    def test_overrides_are_scoped_per_user(self, conn):
        mcp_svc.set_user_override(conn, 1, "calendar", profile="jenny")
        assert mcp_svc.get_user_override(conn, 2, "calendar") is None

    def test_setting_for_an_unknown_server_is_404(self, conn):
        from app.errors import NotFoundError

        with pytest.raises(NotFoundError):
            mcp_svc.set_user_override(conn, 1, "slack", profile="x")

    def test_deleting_a_users_override_cascades_when_the_user_is_deleted(self, conn):
        from app.db import utcnow

        conn.execute(
            "INSERT INTO users (id, username, password_hash, password_salt, created_at, updated_at) "
            "VALUES (99, 'temp', 'h', 's', ?, ?)",
            (utcnow(), utcnow()),
        )
        mcp_svc.set_user_override(conn, 99, "calendar", profile="temp-profile")
        conn.execute("DELETE FROM users WHERE id = 99")
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM user_mcp_profiles WHERE user_id = 99"
            ).fetchone()[0]
            == 0
        )


class TestResolveEffectiveConfig:
    def test_no_override_returns_the_shared_config(self, conn):
        cfg = mcp_svc.resolve_effective_config(conn, "calendar", user_id=1)
        shared = mcp_svc.load_config(conn, "calendar")
        assert cfg.default_profile == shared.default_profile
        assert cfg.auth_token == shared.auth_token

    def test_user_id_none_always_returns_the_shared_config(self, conn):
        mcp_svc.set_user_override(conn, 1, "calendar", profile="jenny", auth_token="tok-1")
        cfg = mcp_svc.resolve_effective_config(conn, "calendar", user_id=None)
        assert cfg.default_profile == mcp_svc.load_config(conn, "calendar").default_profile

    def test_an_override_replaces_the_profile_and_token(self, conn):
        mcp_svc.set_user_override(conn, 1, "calendar", profile="jenny", auth_token="jenny-tok")
        cfg = mcp_svc.resolve_effective_config(conn, "calendar", user_id=1)
        assert cfg.default_profile == "jenny"
        assert cfg.auth_token == "jenny-tok"

    def test_a_profile_only_override_falls_back_to_the_shared_token(self, conn):
        """A user who only knows their profile name, not a separate token,
        still authenticates through the shared server credential."""
        mcp_svc.set_user_override(conn, 1, "calendar", profile="jenny")
        shared = mcp_svc.load_config(conn, "calendar")
        cfg = mcp_svc.resolve_effective_config(conn, "calendar", user_id=1)
        assert cfg.default_profile == "jenny"
        assert cfg.auth_token == shared.auth_token

    def test_other_fields_are_untouched(self, conn):
        mcp_svc.set_user_override(conn, 1, "calendar", profile="jenny")
        shared = mcp_svc.load_config(conn, "calendar")
        cfg = mcp_svc.resolve_effective_config(conn, "calendar", user_id=1)
        assert cfg.base_url == shared.base_url
        assert cfg.tool_name == shared.tool_name
        assert cfg.transport == shared.transport

    def test_two_users_get_two_different_effective_configs(self, conn):
        mcp_svc.set_user_override(conn, 1, "calendar", profile="jenny", auth_token="jenny-tok")
        mcp_svc.set_user_override(conn, 2, "calendar", profile="stone", auth_token="stone-tok")

        a = mcp_svc.resolve_effective_config(conn, "calendar", user_id=1)
        b = mcp_svc.resolve_effective_config(conn, "calendar", user_id=2)
        assert (a.default_profile, a.auth_token) == ("jenny", "jenny-tok")
        assert (b.default_profile, b.auth_token) == ("stone", "stone-tok")


class TestDescribeUserProfile:
    def test_without_an_override_shows_the_shared_profile(self, conn):
        shared = mcp_svc.load_config(conn, "calendar")
        desc = mcp_svc.describe_user_profile(conn, 1, "calendar")
        assert desc["has_override"] is False
        assert desc["profile"] == shared.default_profile
        assert desc["has_personal_token"] is False
        assert desc["auth_token"] is None

    def test_with_an_override_shows_it_and_masks_the_token(self, conn):
        mcp_svc.set_user_override(conn, 1, "calendar", profile="jenny", auth_token="a-real-secret")
        desc = mcp_svc.describe_user_profile(conn, 1, "calendar")

        assert desc["has_override"] is True
        assert desc["profile"] == "jenny"
        assert desc["has_personal_token"] is True
        assert desc["auth_token"].startswith("••••")
        assert "a-real-secret" not in desc["auth_token"]

    def test_shared_profile_is_reported_even_with_an_override(self, conn):
        """So the UI can say 'yours: jenny (shared default: stone)'."""
        shared = mcp_svc.load_config(conn, "calendar")
        mcp_svc.set_user_override(conn, 1, "calendar", profile="jenny")
        desc = mcp_svc.describe_user_profile(conn, 1, "calendar")
        assert desc["shared_profile"] == shared.default_profile


# --------------------------------------------------------------------------- #
# Self-service API
# --------------------------------------------------------------------------- #


def test_lists_both_servers_with_the_shared_defaults(user_client):
    body = user_client.get("/api/me/mcp-profiles").json()
    names = {s["server_name"] for s in body}
    assert names == {"calendar", "email"}
    assert all(s["has_override"] is False for s in body)


def test_a_user_can_set_their_own_profile(user_client):
    resp = user_client.put(
        "/api/me/mcp-profiles/calendar", json={"profile": "alice", "auth_token": "alice-tok"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["profile"] == "alice"
    assert body["has_override"] is True
    assert body["has_personal_token"] is True
    assert body["auth_token"].startswith("••••")


def test_setting_a_profile_without_a_token_uses_the_shared_token(user_client, isolated_settings):
    user_client.put("/api/me/mcp-profiles/calendar", json={"profile": "alice"})
    body = next(
        s for s in user_client.get("/api/me/mcp-profiles").json() if s["server_name"] == "calendar"
    )
    assert body["profile"] == "alice"
    assert body["has_personal_token"] is False


def test_a_masked_echo_does_not_overwrite_the_stored_token(user_client, isolated_settings):
    user_client.put(
        "/api/me/mcp-profiles/calendar", json={"profile": "alice", "auth_token": "real-token"}
    )
    shown = next(
        s for s in user_client.get("/api/me/mcp-profiles").json() if s["server_name"] == "calendar"
    )["auth_token"]

    user_client.put(
        "/api/me/mcp-profiles/calendar", json={"profile": "alice-renamed", "auth_token": shown}
    )

    with get_conn(isolated_settings.db_path) as conn:
        row = conn.execute(
            "SELECT * FROM user_mcp_profiles WHERE server_name = 'calendar'"
        ).fetchone()
    assert row["profile"] == "alice-renamed"
    assert row["auth_token"] == "real-token"


def test_an_empty_string_token_clears_the_personal_token(user_client):
    user_client.put(
        "/api/me/mcp-profiles/calendar", json={"profile": "alice", "auth_token": "real-token"}
    )
    resp = user_client.put(
        "/api/me/mcp-profiles/calendar", json={"profile": "alice", "auth_token": ""}
    )
    assert resp.json()["has_personal_token"] is False


def test_deleting_reverts_to_the_shared_profile(user_client):
    user_client.put("/api/me/mcp-profiles/calendar", json={"profile": "alice"})
    resp = user_client.delete("/api/me/mcp-profiles/calendar")
    assert resp.status_code == 200
    assert resp.json()["removed"] is True

    body = next(
        s for s in user_client.get("/api/me/mcp-profiles").json() if s["server_name"] == "calendar"
    )
    assert body["has_override"] is False


def test_setting_a_profile_for_an_unknown_server_is_404(user_client):
    resp = user_client.put("/api/me/mcp-profiles/slack", json={"profile": "alice"})
    assert resp.status_code == 404


def test_one_users_override_is_invisible_to_another(user_client, other_user_client):
    user_client.put(
        "/api/me/mcp-profiles/calendar", json={"profile": "alice", "auth_token": "alice-tok"}
    )
    body = next(
        s
        for s in other_user_client.get("/api/me/mcp-profiles").json()
        if s["server_name"] == "calendar"
    )
    assert body["has_override"] is False
    assert body["profile"] != "alice"


def test_setting_your_own_profile_does_not_require_admin(user_client):
    # Deliberately not gated: overriding your own account can't affect anyone else.
    resp = user_client.put("/api/me/mcp-profiles/calendar", json={"profile": "alice"})
    assert resp.status_code == 200


def test_test_connection_uses_the_effective_profile(user_client, monkeypatch):
    seen = {}

    async def fake_test(self):
        seen["profile"] = self.config.default_profile
        seen["token"] = self.config.auth_token
        return {"ok": True, "latency_ms": 5, "tools": ["search_events"], "error": None}

    monkeypatch.setattr(mcp_svc.MCPClient, "test", fake_test)

    user_client.put(
        "/api/me/mcp-profiles/calendar", json={"profile": "alice", "auth_token": "alice-tok"}
    )
    resp = user_client.post("/api/me/mcp-profiles/calendar/test")
    assert resp.status_code == 200
    assert seen["profile"] == "alice"
    assert seen["token"] == "alice-tok"


def test_test_connection_can_try_unsaved_edits(user_client, monkeypatch):
    seen = {}

    async def fake_test(self):
        seen["profile"] = self.config.default_profile
        return {"ok": True, "latency_ms": 5, "tools": [], "error": None}

    monkeypatch.setattr(mcp_svc.MCPClient, "test", fake_test)

    resp = user_client.post(
        "/api/me/mcp-profiles/calendar/test", json={"profile": "not-yet-saved"}
    )
    assert resp.status_code == 200
    assert seen["profile"] == "not-yet-saved"

    # ...and must not have been persisted.
    body = next(
        s for s in user_client.get("/api/me/mcp-profiles").json() if s["server_name"] == "calendar"
    )
    assert body["has_override"] is False


# --------------------------------------------------------------------------- #
# Admin on-behalf-of
# --------------------------------------------------------------------------- #


def test_admin_can_set_a_profile_for_another_user(admin_client, make_user):
    user, _ = make_user("bob")
    resp = admin_client.put(
        f"/api/users/{user['id']}/mcp-profiles/calendar",
        json={"profile": "bob", "auth_token": "bob-tok"},
    )
    assert resp.status_code == 200
    assert resp.json()["profile"] == "bob"


def test_a_regular_user_cannot_set_profiles_for_someone_else(user_client, other_user_client):
    me = user_client.get("/api/auth/me").json()
    resp = other_user_client.put(
        f"/api/users/{me['id']}/mcp-profiles/calendar", json={"profile": "hijacked"}
    )
    assert resp.status_code == 403


def test_admin_setting_a_profile_is_visible_to_that_user(admin_client, make_user):
    user, as_user = make_user("carol")
    admin_client.put(
        f"/api/users/{user['id']}/mcp-profiles/calendar", json={"profile": "carol"}
    )
    body = next(
        s for s in as_user.get("/api/me/mcp-profiles").json() if s["server_name"] == "calendar"
    )
    assert body["profile"] == "carol"
    assert body["has_override"] is True


def test_admin_can_clear_a_users_profile(admin_client, make_user):
    user, _ = make_user("dave")
    admin_client.put(f"/api/users/{user['id']}/mcp-profiles/calendar", json={"profile": "dave"})
    resp = admin_client.delete(f"/api/users/{user['id']}/mcp-profiles/calendar")
    assert resp.status_code == 200
    assert resp.json()["removed"] is True


def test_admin_endpoints_404_for_an_unknown_user(admin_client):
    resp = admin_client.put("/api/users/9999/mcp-profiles/calendar", json={"profile": "x"})
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Matching actually uses the per-user profile
# --------------------------------------------------------------------------- #


class RecordingMCP:
    """Captures which profile each call was made with."""

    calls: list[tuple[str, str | None]] = []

    def __init__(self, config):
        self.config = config

    async def search_events(self, **kwargs):
        RecordingMCP.calls.append(("calendar", self.config.default_profile))
        return []

    async def search_emails(self, query, **kwargs):
        RecordingMCP.calls.append(("email", self.config.default_profile))
        return []


async def test_gather_candidates_uses_the_requesting_users_profile(
    conn, isolated_settings, monkeypatch
):
    from datetime import datetime, timezone

    from app.services import matching as matching_svc
    from app.db import get_conn, utcnow

    monkeypatch.setattr(matching_svc.mcp_svc, "MCPClient", RecordingMCP)
    RecordingMCP.calls = []

    now = utcnow()
    conn.execute(
        "INSERT INTO threads (id, owner_id, title, created_at, updated_at) VALUES (1, 1, 'T', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO meetings (id, thread_id, owner_id, title, created_at, updated_at) "
        "VALUES (1, 1, 1, 'M', ?, ?)",
        (now, now),
    )
    mcp_svc.set_user_override(conn, 1, "calendar", profile="jenny")
    mcp_svc.set_user_override(conn, 1, "email", profile="jenny")
    conn.commit()

    def conn_factory():
        return get_conn(isolated_settings.db_path)

    start = datetime(2026, 3, 11, tzinfo=timezone.utc)
    end = datetime(2026, 3, 21, tzinfo=timezone.utc)

    await matching_svc.gather_candidates(
        conn_factory,
        meeting_id=1,
        keywords=["atlas"],
        start=start,
        end=end,
        max_candidates=10,
        user_id=1,
    )

    assert ("calendar", "jenny") in RecordingMCP.calls
    assert ("email", "jenny") in RecordingMCP.calls


async def test_gather_candidates_falls_back_to_shared_profile_with_no_override(
    conn, isolated_settings, monkeypatch
):
    from datetime import datetime, timezone

    from app.services import matching as matching_svc
    from app.db import get_conn, utcnow

    monkeypatch.setattr(matching_svc.mcp_svc, "MCPClient", RecordingMCP)
    RecordingMCP.calls = []

    now = utcnow()
    conn.execute(
        "INSERT INTO threads (id, owner_id, title, created_at, updated_at) VALUES (1, 1, 'T', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO meetings (id, thread_id, owner_id, title, created_at, updated_at) "
        "VALUES (1, 1, 1, 'M', ?, ?)",
        (now, now),
    )
    conn.commit()
    shared = mcp_svc.load_config(conn, "calendar")

    def conn_factory():
        return get_conn(isolated_settings.db_path)

    start = datetime(2026, 3, 11, tzinfo=timezone.utc)
    end = datetime(2026, 3, 21, tzinfo=timezone.utc)

    await matching_svc.gather_candidates(
        conn_factory,
        meeting_id=1,
        keywords=["atlas"],
        start=start,
        end=end,
        max_candidates=10,
        user_id=1,
    )

    assert ("calendar", shared.default_profile) in RecordingMCP.calls
