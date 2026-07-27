"""Per-user integrations: storage, masking, and the migration off shared MCP config.

The migration tests carry the most weight here. An upgrade that silently drops
somebody's calendar access would look exactly like "matching stopped finding
things", with nothing in the logs to say why.
"""

from __future__ import annotations

import json

import pytest

from app.db import get_conn, seed_mcp_servers, utcnow
from app.errors import ConflictError, NotFoundError, ValidationError
from app.services import integrations as svc
from app.services import secretstore


def _add_users(db_path, *names: str) -> None:
    with get_conn(db_path) as conn:
        now = utcnow()
        for i, name in enumerate(names, start=1):
            conn.execute(
                "INSERT INTO users (id, username, password_hash, password_salt, "
                "created_at, updated_at) VALUES (?, ?, 'h', 's', ?, ?)",
                (i, name, now, now),
            )


@pytest.fixture
def db(initialised_db):
    """A database with the two seeded MCP servers and two users."""
    seed_mcp_servers(initialised_db)
    _add_users(initialised_db, "alice", "bob")
    return initialised_db


# --------------------------------------------------------------------------- #
# Secret handling
# --------------------------------------------------------------------------- #


class TestSecretHandling:
    @pytest.mark.parametrize(
        "given,expected",
        [
            (None, (None, False)),
            ("", (None, True)),
            ("••••1234", (None, False)),
            ("a-new-token", ("a-new-token", False)),
        ],
    )
    def test_the_tri_state_secret_field(self, given, expected):
        """Unset, cleared, a masked echo of what was shown, or genuinely new."""
        assert svc.resolve_secret_update(given) == expected

    def test_masking_keeps_only_the_tail(self):
        assert svc.mask("supersecrettoken1234") == "••••1234"
        assert svc.mask("abc") == "••••"
        assert svc.mask(None) is None

    def test_the_secret_is_encrypted_in_the_database(self, db):
        with get_conn(db) as conn:
            svc.create(
                conn,
                user_id=1,
                provider="mcp_email",
                account_key="email:alice",
                secret={"auth_token": "plaintext-token-value"},
            )
        with get_conn(db) as conn:
            stored = conn.execute("SELECT secret_json FROM integrations").fetchone()[0]
        assert "plaintext-token-value" not in stored
        assert secretstore.decrypt(stored)["auth_token"] == "plaintext-token-value"

    def test_the_api_shape_never_carries_the_secret(self, db):
        with get_conn(db) as conn:
            svc.create(
                conn,
                user_id=1,
                provider="mcp_email",
                account_key="email:alice",
                secret={"auth_token": "supersecrettoken1234"},
            )
            payload = svc.list_for_user(conn, 1)[0]

        assert "secret_json" not in payload
        assert payload["has_secret"] is True
        assert payload["secret_preview"] == "••••1234"
        assert "supersecrettoken1234" not in json.dumps(payload)

    def test_an_unreadable_secret_does_not_break_listing(self, db):
        """A lost key must degrade to "reconnect", not to a broken settings page."""
        with get_conn(db) as conn:
            svc.create(
                conn,
                user_id=1,
                provider="mcp_email",
                account_key="email:alice",
                secret={"auth_token": "value"},
            )
            conn.execute(
                "UPDATE integrations SET secret_json = ?",
                (json.dumps({"key_id": "deadbeef", "ct": "gAAAAABm-not-a-real-token"}),),
            )
            payload = svc.list_for_user(conn, 1)[0]

        assert payload["has_secret"] is True
        assert payload["secret_preview"] is None


# --------------------------------------------------------------------------- #
# CRUD and ownership
# --------------------------------------------------------------------------- #


class TestCrud:
    def test_capabilities_default_to_everything_the_provider_supports(self, db):
        with get_conn(db) as conn:
            svc.create(conn, user_id=1, provider="mcp_calendar", account_key="cal:alice")
            row = svc.list_for_user(conn, 1)[0]
        assert (row["calendar_enabled"], row["email_enabled"]) == (True, False)

    def test_a_calendar_provider_cannot_be_asked_to_search_email(self, db):
        with get_conn(db) as conn:
            with pytest.raises(ValidationError):
                svc.create(
                    conn,
                    user_id=1,
                    provider="mcp_calendar",
                    account_key="cal:alice",
                    email_enabled=True,
                )

    def test_an_unknown_provider_is_rejected(self, db):
        with get_conn(db) as conn:
            with pytest.raises(NotFoundError):
                svc.create(conn, user_id=1, provider="carrier_pigeon", account_key="x")

    def test_connecting_the_same_account_twice_conflicts(self, db):
        with get_conn(db) as conn:
            svc.create(conn, user_id=1, provider="mcp_email", account_key="email:alice")
            with pytest.raises(ConflictError):
                svc.create(conn, user_id=1, provider="mcp_email", account_key="email:alice")

    def test_another_users_integration_is_404_not_403(self, db):
        """A 403 would confirm the row exists."""
        with get_conn(db) as conn:
            made = svc.create(
                conn, user_id=1, provider="mcp_email", account_key="email:alice"
            )
            with pytest.raises(NotFoundError):
                svc.require_own(conn, made, user_id=2)

    def test_listing_is_scoped_to_the_owner(self, db):
        with get_conn(db) as conn:
            svc.create(conn, user_id=1, provider="mcp_email", account_key="email:alice")
            svc.create(conn, user_id=2, provider="mcp_email", account_key="email:bob")
            assert len(svc.list_for_user(conn, 1)) == 1
            assert len(svc.list_for_user(conn, 2)) == 1

    def test_update_merges_config_rather_than_replacing_it(self, db):
        with get_conn(db) as conn:
            made = svc.create(
                conn,
                user_id=1,
                provider="mcp_email",
                account_key="email:alice",
                config={"base_url": "http://a", "tool_name": "search_emails"},
            )
            svc.update(conn, made, 1, config={"base_url": "http://b"})
            config = svc.row_to_dict(svc.require_own(conn, made, 1))["config"]

        assert config["base_url"] == "http://b"
        assert config["tool_name"] == "search_emails", "unrelated keys survive"

    def test_a_masked_secret_echo_leaves_the_stored_value_alone(self, db):
        with get_conn(db) as conn:
            made = svc.create(
                conn,
                user_id=1,
                provider="mcp_email",
                account_key="email:alice",
                secret={"auth_token": "original-token"},
            )
            svc.update(conn, made, 1, secret_updates={"auth_token": "••••oken"})
            row = svc.require_own(conn, made, 1)

        assert secretstore.decrypt(row["secret_json"])["auth_token"] == "original-token"

    def test_supplying_a_new_secret_clears_a_reauth_flag(self, db):
        """The form the user just submitted is the fix for that state."""
        with get_conn(db) as conn:
            made = svc.create(
                conn, user_id=1, provider="mcp_email", account_key="email:alice"
            )
            conn.execute(
                "UPDATE integrations SET status = 'reauth_required' WHERE id = ?", (made,)
            )
            svc.update(conn, made, 1, secret_updates={"auth_token": "fresh"})
            assert svc.require_own(conn, made, 1)["status"] == "unverified"

    def test_delete_leaves_attached_history_alone(self, db):
        """Attached events are copies on the thread. Revoking a token must not
        erase what a meeting was already matched to."""
        with get_conn(db) as conn:
            conn.execute(
                "INSERT INTO threads (id, owner_id, title, created_at, updated_at) "
                "VALUES (1, 1, 'T', ?, ?)",
                (utcnow(), utcnow()),
            )
            conn.execute(
                "INSERT INTO thread_calendar_events (thread_id, uid, raw_json, attached_at) "
                "VALUES (1, 'uid-1', '{}', ?)",
                (utcnow(),),
            )
            made = svc.create(
                conn, user_id=1, provider="mcp_calendar", account_key="cal:alice"
            )
            svc.delete(conn, made, 1)

            assert svc.list_for_user(conn, 1) == []
            assert conn.execute(
                "SELECT COUNT(*) FROM thread_calendar_events"
            ).fetchone()[0] == 1


# --------------------------------------------------------------------------- #
# Migration
# --------------------------------------------------------------------------- #


class TestMigration:
    def test_every_user_gets_an_integration_per_configured_server(self, db):
        assert svc.migrate_mcp_servers(db) == 4  # 2 users x 2 servers

        with get_conn(db) as conn:
            for user_id in (1, 2):
                providers = {r["provider"] for r in conn.execute(
                    "SELECT provider FROM integrations WHERE user_id = ?", (user_id,)
                )}
                assert providers == {"mcp_calendar", "mcp_email"}

    def test_the_shared_config_and_token_carry_over(self, db):
        """"Nobody loses access" is the whole requirement."""
        svc.migrate_mcp_servers(db)

        with get_conn(db) as conn:
            row = conn.execute(
                "SELECT * FROM integrations WHERE user_id = 1 AND provider = 'mcp_calendar'"
            ).fetchone()

        config = json.loads(row["config_json"])
        assert config["base_url"]
        assert config["tool_name"] == "search_events"
        assert config["transport"] == "sse"
        assert secretstore.decrypt(row["secret_json"])["auth_token"] == "test-calendar-token"

    def test_a_users_own_override_wins_over_the_shared_account(self, db):
        with get_conn(db) as conn:
            conn.execute(
                "INSERT INTO user_mcp_profiles (user_id, server_name, profile, "
                "auth_token, updated_at) VALUES (2, 'calendar', 'bob-profile', "
                "'bobs-own-token', ?)",
                (utcnow(),),
            )

        svc.migrate_mcp_servers(db)

        with get_conn(db) as conn:
            bob = conn.execute(
                "SELECT * FROM integrations WHERE user_id = 2 AND provider = 'mcp_calendar'"
            ).fetchone()
            alice = conn.execute(
                "SELECT * FROM integrations WHERE user_id = 1 AND provider = 'mcp_calendar'"
            ).fetchone()

        assert json.loads(bob["config_json"])["profile"] == "bob-profile"
        assert secretstore.decrypt(bob["secret_json"])["auth_token"] == "bobs-own-token"
        # Alice had no override, so she keeps the shared account.
        assert secretstore.decrypt(alice["secret_json"])["auth_token"] == "test-calendar-token"

    def test_a_profile_only_override_still_uses_the_shared_token(self, db):
        """Matches the pre-refactor resolution rule exactly."""
        with get_conn(db) as conn:
            conn.execute(
                "INSERT INTO user_mcp_profiles (user_id, server_name, profile, "
                "auth_token, updated_at) VALUES (2, 'email', 'bob-inbox', NULL, ?)",
                (utcnow(),),
            )

        svc.migrate_mcp_servers(db)

        with get_conn(db) as conn:
            bob = conn.execute(
                "SELECT * FROM integrations WHERE user_id = 2 AND provider = 'mcp_email'"
            ).fetchone()

        assert json.loads(bob["config_json"])["profile"] == "bob-inbox"
        assert secretstore.decrypt(bob["secret_json"])["auth_token"] == "test-email-token"

    def test_running_it_twice_creates_nothing_extra(self, db):
        assert svc.migrate_mcp_servers(db) == 4
        assert svc.migrate_mcp_servers(db) == 0

        with get_conn(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM integrations").fetchone()[0] == 4

    def test_an_unreachable_server_row_is_skipped(self, initialised_db):
        """A bare .env leaves the seeded rows with no base_url; migrating those
        would hand every user an integration that cannot work."""
        _add_users(initialised_db, "alice")
        with get_conn(initialised_db) as conn:
            now = utcnow()
            conn.execute(
                "INSERT INTO mcp_servers (name, kind, transport, enabled, base_url, "
                "tool_name, timeout_sec, created_at, updated_at) "
                "VALUES ('calendar', 'calendar', 'sse', 1, NULL, 'search_events', 60, ?, ?)",
                (now, now),
            )

        assert svc.migrate_mcp_servers(initialised_db) == 0

    def test_a_disabled_server_migrates_as_disabled(self, db):
        with get_conn(db) as conn:
            conn.execute("UPDATE mcp_servers SET enabled = 0 WHERE name = 'email'")

        svc.migrate_mcp_servers(db)

        with get_conn(db) as conn:
            row = conn.execute(
                "SELECT enabled FROM integrations WHERE user_id = 1 AND provider = 'mcp_email'"
            ).fetchone()
        assert row["enabled"] == 0

    def test_the_old_tables_are_left_in_place(self, db):
        """Deliberate: DROP buys nothing on single-file SQLite and costs rollback."""
        svc.migrate_mcp_servers(db)
        with get_conn(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM mcp_servers").fetchone()[0] == 2
