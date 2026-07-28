"""OAuth token refresh.

The rules under test all protect one thing: a rotating refresh token. Refresh it
twice concurrently, or write back a token that lost a race, and the account is
orphaned until the user reconnects -- with nothing in the logs saying why.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.db import get_conn, utcnow
from app.errors import IntegrationAuthError
from app.services import integrations as isvc
from app.services import secretstore
from app.services.providers import oauth, tokens

CLIENT = oauth.OAuthClient(
    client_id="cid",
    client_secret="csecret",
    authorize_url="https://auth.test/authorize",
    token_url="https://auth.test/token",
    scopes=("scope-a",),
)


@pytest.fixture(autouse=True)
def _clean_locks():
    tokens.reset_locks()
    yield
    tokens.reset_locks()


@pytest.fixture
def account(initialised_db):
    """One connected Google account whose access token has already expired."""
    with get_conn(initialised_db) as conn:
        now = utcnow()
        conn.execute(
            "INSERT INTO users (id, username, password_hash, password_salt, created_at, "
            "updated_at) VALUES (1, 'alice', 'h', 's', ?, ?)",
            (now, now),
        )
        integration_id = isvc.create(
            conn,
            user_id=1,
            provider="google",
            account_key="sub-123",
            account_label="me@example.com",
            secret={"access_token": "stale", "refresh_token": "rt-1"},
            token_expires_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
            status="ok",
        )
    return initialised_db, integration_id


def _secret(db, integration_id) -> dict:
    with get_conn(db) as conn:
        row = conn.execute(
            "SELECT secret_json FROM integrations WHERE id = ?", (integration_id,)
        ).fetchone()
    return secretstore.decrypt(row["secret_json"])


def _row(db, integration_id):
    with get_conn(db) as conn:
        return conn.execute(
            "SELECT * FROM integrations WHERE id = ?", (integration_id,)
        ).fetchone()


class TestHappyPath:
    async def test_a_live_token_is_returned_without_refreshing(self, account, monkeypatch):
        db, integration_id = account
        with get_conn(db) as conn:
            conn.execute(
                "UPDATE integrations SET token_expires_at = ? WHERE id = ?",
                ((datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), integration_id),
            )

        def boom(*a, **k):
            raise AssertionError("must not refresh a token that is still good")

        monkeypatch.setattr(oauth, "refresh", boom)
        assert await tokens.access_token(integration_id, CLIENT, db) == "stale"

    async def test_an_expired_token_is_refreshed_and_stored(self, account, monkeypatch):
        db, integration_id = account
        monkeypatch.setattr(
            oauth, "refresh",
            lambda client, rt: {"access_token": "fresh", "expires_in": 3600},
        )

        assert await tokens.access_token(integration_id, CLIENT, db) == "fresh"
        assert _secret(db, integration_id)["access_token"] == "fresh"
        assert _row(db, integration_id)["secret_version"] == 2

    async def test_a_token_about_to_expire_is_refreshed_early(self, account, monkeypatch):
        """A token with 30s left would die mid-request."""
        db, integration_id = account
        with get_conn(db) as conn:
            conn.execute(
                "UPDATE integrations SET token_expires_at = ? WHERE id = ?",
                ((datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(), integration_id),
            )
        monkeypatch.setattr(
            oauth, "refresh", lambda client, rt: {"access_token": "fresh", "expires_in": 3600}
        )
        assert await tokens.access_token(integration_id, CLIENT, db) == "fresh"

    async def test_a_rotated_refresh_token_replaces_the_old_one(self, account, monkeypatch):
        db, integration_id = account
        monkeypatch.setattr(
            oauth, "refresh",
            lambda client, rt: {"access_token": "fresh", "refresh_token": "rt-2", "expires_in": 3600},
        )
        await tokens.access_token(integration_id, CLIENT, db)
        assert _secret(db, integration_id)["refresh_token"] == "rt-2"

    async def test_an_unrotated_refresh_token_is_kept(self, account, monkeypatch):
        """Providers that do not rotate omit the field; it must not be dropped."""
        db, integration_id = account
        monkeypatch.setattr(
            oauth, "refresh", lambda client, rt: {"access_token": "fresh", "expires_in": 3600}
        )
        await tokens.access_token(integration_id, CLIENT, db)
        assert _secret(db, integration_id)["refresh_token"] == "rt-1"


class TestConcurrency:
    async def test_simultaneous_callers_refresh_exactly_once(self, account, monkeypatch):
        """The double-checked read is what makes this hold. Without it the second
        caller refreshes again and invalidates the token the first just stored."""
        db, integration_id = account
        calls = []

        def refresh(client, rt):
            calls.append(rt)
            return {"access_token": f"fresh-{len(calls)}", "expires_in": 3600}

        monkeypatch.setattr(oauth, "refresh", refresh)

        results = await asyncio.gather(
            *(tokens.access_token(integration_id, CLIENT, db) for _ in range(5))
        )

        assert len(calls) == 1, f"refreshed {len(calls)} times"
        assert set(results) == {"fresh-1"}
        assert _row(db, integration_id)["secret_version"] == 2

    async def test_a_lost_write_race_discards_the_new_token(self, account, monkeypatch):
        """If someone else rotated while we were on the wire, ours may already be
        dead -- writing it would orphan whichever token the DB now holds."""
        db, integration_id = account

        def refresh(client, rt):
            # Simulate another process winning between our read and our write.
            with get_conn(db) as conn:
                conn.execute(
                    "UPDATE integrations SET secret_json = ?, secret_version = secret_version + 1, "
                    "token_expires_at = ? WHERE id = ?",
                    (
                        secretstore.encrypt(
                            {"access_token": "winner", "refresh_token": "rt-winner"}
                        ),
                        (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                        integration_id,
                    ),
                )
            return {"access_token": "loser", "expires_in": 3600}

        monkeypatch.setattr(oauth, "refresh", refresh)

        returned = await tokens.access_token(integration_id, CLIENT, db)

        stored = _secret(db, integration_id)
        assert stored["access_token"] == "winner", "the winner's token must survive"
        assert stored["refresh_token"] == "rt-winner"
        assert returned == "winner"


class TestTerminalFailure:
    async def test_invalid_grant_flags_the_account_and_stops(self, account, monkeypatch):
        """Retrying a dead grant burns quota and hides the only fix."""
        db, integration_id = account

        def refresh(client, rt):
            raise oauth.OAuthError("Token has been expired or revoked", error_code="invalid_grant")

        monkeypatch.setattr(oauth, "refresh", refresh)

        with pytest.raises(IntegrationAuthError) as exc:
            await tokens.access_token(integration_id, CLIENT, db)

        assert "me@example.com" in str(exc.value), "name the account so it is actionable"
        row = _row(db, integration_id)
        assert row["status"] == "reauth_required"
        assert row["last_test_error"]

    async def test_a_transient_failure_is_not_treated_as_terminal(self, account, monkeypatch):
        """A 503 must not mark a perfectly good account as needing reauth."""
        db, integration_id = account

        def refresh(client, rt):
            raise oauth.OAuthError("http_503: upstream busy", error_code="http_503")

        monkeypatch.setattr(oauth, "refresh", refresh)

        with pytest.raises(oauth.OAuthError):
            await tokens.access_token(integration_id, CLIENT, db)
        assert _row(db, integration_id)["status"] == "ok"

    async def test_a_missing_refresh_token_asks_for_a_reconnect(self, account, monkeypatch):
        db, integration_id = account
        with get_conn(db) as conn:
            conn.execute(
                "UPDATE integrations SET secret_json = ? WHERE id = ?",
                (secretstore.encrypt({"access_token": "stale"}), integration_id),
            )

        with pytest.raises(IntegrationAuthError):
            await tokens.access_token(integration_id, CLIENT, db)
        assert _row(db, integration_id)["status"] == "reauth_required"

    async def test_refreshing_clears_a_previous_reauth_flag(self, account, monkeypatch):
        db, integration_id = account
        with get_conn(db) as conn:
            conn.execute(
                "UPDATE integrations SET status = 'reauth_required' WHERE id = ?",
                (integration_id,),
            )
        monkeypatch.setattr(
            oauth, "refresh", lambda client, rt: {"access_token": "fresh", "expires_in": 3600}
        )

        await tokens.access_token(integration_id, CLIENT, db)
        assert _row(db, integration_id)["status"] == "ok"
