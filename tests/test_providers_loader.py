"""Loading a user's integrations into live providers.

Faked at the transport (``MCPClient``) rather than at the provider, so these
exercise the real chain: loader → registry → provider → MCPServerConfig. The
provider-level fake in test_matching.py deliberately skips all of that, so this is
where a wiring mistake between the layers gets caught.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.db import get_conn, utcnow
from app.services import integrations as integrations_svc
from app.services import matching as matching_svc
from app.services import mcpclient as mcp_svc
from app.services import secretstore
from app.services.providers import loader

START = datetime(2026, 3, 11, tzinfo=timezone.utc)
END = datetime(2026, 3, 21, tzinfo=timezone.utc)


class RecordingMCP:
    """Captures the config each call was actually made with."""

    calls: list[tuple[str, str | None, str | None]] = []

    def __init__(self, config):
        self.config = config

    async def search_events(self, **kwargs):
        RecordingMCP.calls.append(
            ("calendar", self.config.default_profile, self.config.auth_token)
        )
        return []

    async def search_emails(self, query, **kwargs):
        RecordingMCP.calls.append(
            ("email", self.config.default_profile, self.config.auth_token)
        )
        return []


@pytest.fixture(autouse=True)
def recording(monkeypatch):
    RecordingMCP.calls = []
    monkeypatch.setattr(mcp_svc, "MCPClient", RecordingMCP)
    return RecordingMCP


@pytest.fixture
def two_users(initialised_db):
    with get_conn(initialised_db) as conn:
        now = utcnow()
        for i, name in enumerate(("alice", "bob"), start=1):
            conn.execute(
                "INSERT INTO users (id, username, password_hash, password_salt, "
                "created_at, updated_at) VALUES (?, ?, 'h', 's', ?, ?)",
                (i, name, now, now),
            )
        conn.execute(
            "INSERT INTO threads (id, owner_id, title, created_at, updated_at) "
            "VALUES (1, 1, 'T', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO meetings (id, thread_id, owner_id, title, created_at, updated_at) "
            "VALUES (1, 1, 1, 'M', ?, ?)",
            (now, now),
        )
    return initialised_db


def _connect(db, user_id: int, provider: str, profile: str, token: str) -> int:
    with get_conn(db) as conn:
        return integrations_svc.create(
            conn,
            user_id=user_id,
            provider=provider,
            account_key=f"{provider}:{profile}",
            config={
                "transport": "sse",
                "base_url": "http://mcp.test:4006",
                "tool_name": "search_events" if "calendar" in provider else "search_emails",
                "profile": profile,
            },
            secret={"auth_token": token},
        )


async def _gather(db, user_id):
    def conn_factory():
        return get_conn(db)

    return await matching_svc.gather_candidates(
        conn_factory,
        meeting_id=1,
        keywords=["atlas"],
        start=START,
        end=END,
        max_candidates=10,
        user_id=user_id,
    )


class TestPerUserScoping:
    async def test_a_users_own_account_is_the_one_searched(self, two_users):
        _connect(two_users, 1, "mcp_calendar", "jenny", "jenny-token")
        _connect(two_users, 1, "mcp_email", "jenny", "jenny-token")

        await _gather(two_users, user_id=1)

        assert ("calendar", "jenny", "jenny-token") in RecordingMCP.calls
        assert ("email", "jenny", "jenny-token") in RecordingMCP.calls

    async def test_another_users_account_is_never_searched(self, two_users):
        """The core promise of per-user integrations."""
        _connect(two_users, 1, "mcp_calendar", "alice-cal", "alice-token")
        _connect(two_users, 2, "mcp_calendar", "bob-cal", "bob-token")

        await _gather(two_users, user_id=1)

        profiles = {profile for _, profile, _ in RecordingMCP.calls}
        assert profiles == {"alice-cal"}

    async def test_a_user_with_nothing_connected_gets_a_clear_error(self, two_users):
        from app.errors import NoIntegrationsError

        _connect(two_users, 2, "mcp_calendar", "bob-cal", "bob-token")

        with pytest.raises(NoIntegrationsError):
            await _gather(two_users, user_id=1)
        assert RecordingMCP.calls == []


class TestCapabilityFiltering:
    def test_a_disabled_capability_excludes_the_account(self, two_users):
        made = _connect(two_users, 1, "mcp_calendar", "jenny", "t")
        with get_conn(two_users) as conn:
            conn.execute(
                "UPDATE integrations SET calendar_enabled = 0 WHERE id = ?", (made,)
            )
        with get_conn(two_users) as conn:
            assert loader.load_for_user(conn, 1, kind="calendar") == []

    def test_a_disabled_integration_is_excluded_entirely(self, two_users):
        made = _connect(two_users, 1, "mcp_calendar", "jenny", "t")
        with get_conn(two_users) as conn:
            conn.execute("UPDATE integrations SET enabled = 0 WHERE id = ?", (made,))
        with get_conn(two_users) as conn:
            assert loader.load_for_user(conn, 1) == []

    def test_an_email_account_is_not_offered_for_calendar_search(self, two_users):
        _connect(two_users, 1, "mcp_email", "jenny", "t")
        with get_conn(two_users) as conn:
            assert loader.load_for_user(conn, 1, kind="calendar") == []
            assert len(loader.load_for_user(conn, 1, kind="email")) == 1

    def test_no_user_means_no_providers(self, two_users):
        """Background jobs without a user must not fall back to somebody's account."""
        _connect(two_users, 1, "mcp_calendar", "jenny", "t")
        with get_conn(two_users) as conn:
            assert loader.load_for_user(conn, None) == []


class TestDegradation:
    def test_an_unreadable_secret_flags_the_account_and_skips_it(self, two_users):
        """A changed encryption key must degrade to "reconnect this account",
        never to a 500 that takes the whole match down."""
        made = _connect(two_users, 1, "mcp_calendar", "jenny", "t")
        with get_conn(two_users) as conn:
            conn.execute(
                "UPDATE integrations SET secret_json = ? WHERE id = ?",
                (json.dumps({"key_id": "deadbeef", "ct": "gAAAAAB-not-real"}), made),
            )

        with get_conn(two_users) as conn:
            assert loader.load_for_user(conn, 1) == []

        with get_conn(two_users) as conn:
            row = conn.execute("SELECT * FROM integrations WHERE id = ?", (made,)).fetchone()
        assert row["status"] == "reauth_required"
        assert row["last_test_error"]

    def test_a_row_for_an_unknown_provider_does_not_break_the_others(self, two_users):
        _connect(two_users, 1, "mcp_calendar", "jenny", "t")
        with get_conn(two_users) as conn:
            now = utcnow()
            conn.execute(
                "INSERT INTO integrations (user_id, provider, account_key, auth_type, "
                "calendar_enabled, created_at, updated_at) "
                "VALUES (1, 'retired_provider', 'x', 'token', 1, ?, ?)",
                (now, now),
            )
        with get_conn(two_users) as conn:
            loaded = loader.load_for_user(conn, 1, kind="calendar")
        assert [p.provider_id for p in loaded] == ["mcp_calendar"]

    def test_malformed_config_json_falls_back_to_empty(self, two_users):
        made = _connect(two_users, 1, "mcp_calendar", "jenny", "t")
        with get_conn(two_users) as conn:
            conn.execute(
                "UPDATE integrations SET config_json = 'not json' WHERE id = ?", (made,)
            )
        with get_conn(two_users) as conn:
            loaded = loader.load_for_user(conn, 1, kind="calendar")
        assert len(loaded) == 1
        assert loaded[0].config == {}


class TestSummary:
    def test_it_counts_each_capability_separately(self, two_users):
        _connect(two_users, 1, "mcp_calendar", "jenny", "t")
        _connect(two_users, 1, "mcp_email", "jenny", "t")
        with get_conn(two_users) as conn:
            assert loader.summary_for_user(conn, 1) == {
                "calendar": 1,
                "email": 1,
                "needs_reauth": [],
            }

    def test_zero_when_nothing_is_connected(self, two_users):
        """What the SPA greys the match button on."""
        with get_conn(two_users) as conn:
            summary = loader.summary_for_user(conn, 1)
        assert (summary["calendar"], summary["email"]) == (0, 0)

    def test_an_account_needing_reauth_is_named(self, two_users):
        made = _connect(two_users, 1, "mcp_calendar", "jenny", "t")
        with get_conn(two_users) as conn:
            conn.execute(
                "UPDATE integrations SET status = 'reauth_required', account_label = 'Jenny cal' "
                "WHERE id = ?",
                (made,),
            )
        with get_conn(two_users) as conn:
            summary = loader.summary_for_user(conn, 1)

        assert summary["needs_reauth"] == [
            {"id": made, "provider": "mcp_calendar", "account_label": "Jenny cal"}
        ]
        # Still counted: it is connected, just not currently working. The button
        # stays enabled so the failure is reported per-account rather than the
        # feature silently disappearing.
        assert summary["calendar"] == 1


class TestSecretIsolation:
    def test_the_token_reaches_the_transport_decrypted(self, two_users):
        """End-to-end proof the encryption round-trip is wired up: the provider
        must hand MCPClient a usable token, not the ciphertext."""
        _connect(two_users, 1, "mcp_calendar", "jenny", "the-real-token")
        with get_conn(two_users) as conn:
            stored = conn.execute("SELECT secret_json FROM integrations").fetchone()[0]
        assert "the-real-token" not in stored

        with get_conn(two_users) as conn:
            provider = loader.load_for_user(conn, 1, kind="calendar")[0]
        assert provider._config().auth_token == "the-real-token"
        assert secretstore.decrypt(stored)["auth_token"] == "the-real-token"
