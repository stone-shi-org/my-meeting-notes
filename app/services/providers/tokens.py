"""The single owner of OAuth token refresh.

Providers never see a token string, only a callable that returns a fresh one.
Refresh logic scattered across four provider modules would be four
implementations and four bugs; concentrating it here also means there is exactly
one place where the concurrency rules below have to hold.

Three layers of protection, because losing a rotating refresh token orphans an
account until the user reconnects:

1. An in-process ``asyncio.Lock`` per integration collapses the common case
   (two match jobs and a Settings test hitting one account at once).
2. A double-checked re-read *inside* that lock, so a waiter does not refresh a
   token the winner already rotated.
3. A DB lease and a ``secret_version`` compare-and-swap for anything the
   in-process lock cannot see. If the CAS is lost the freshly minted token is
   **discarded** rather than written -- writing it would orphan whichever token
   the database already holds.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.db import get_conn, utcnow
from app.errors import IntegrationAuthError
from app.logging_config import get_logger
from app.services import secretstore
from app.services.providers import oauth

log = get_logger("providers.tokens")

# Refresh this far ahead of expiry, so a request that takes a moment to start
# does not go out with a token that dies mid-flight.
REFRESH_MARGIN_SEC = 120
LEASE_SEC = 60

_locks: dict[int, asyncio.Lock] = {}


def _lock_for(integration_id: int) -> asyncio.Lock:
    lock = _locks.get(integration_id)
    if lock is None:
        lock = _locks[integration_id] = asyncio.Lock()
    return lock


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _still_valid(row: sqlite3.Row, secret: dict) -> str | None:
    """The current access token, if it will outlive the refresh margin."""
    token = secret.get("access_token")
    if not token:
        return None
    expires = _parse(row["token_expires_at"])
    if expires is None:
        return token  # no expiry recorded: assume usable, a 401 will correct us
    if expires - timedelta(seconds=REFRESH_MARGIN_SEC) > datetime.now(timezone.utc):
        return token
    return None


def _read(db_path, integration_id: int) -> tuple[sqlite3.Row, dict]:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM integrations WHERE id = ?", (integration_id,)
        ).fetchone()
    if row is None:
        raise IntegrationAuthError("That account is no longer connected")
    return row, secretstore.decrypt(row["secret_json"])


def mark_reauth_required(db_path, integration_id: int, message: str) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE integrations SET status = 'reauth_required', last_test_ok = 0, "
            "last_test_error = ?, updated_at = ? WHERE id = ?",
            (message, utcnow(), integration_id),
        )


def _claim_lease(db_path, integration_id: int) -> bool:
    now = datetime.now(timezone.utc)
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE integrations SET refresh_lease_until = ? "
            "WHERE id = ? AND (refresh_lease_until IS NULL OR refresh_lease_until < ?)",
            ((now + timedelta(seconds=LEASE_SEC)).isoformat(), integration_id, now.isoformat()),
        )
        return cur.rowcount == 1


def _store(db_path, integration_id: int, expected_version: int, secret: dict, expires_at) -> bool:
    """Write the rotated credential back, guarded by the version we read.

    Returns False when somebody else got there first, in which case the caller
    must throw its token away.
    """
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE integrations SET secret_json = ?, secret_version = secret_version + 1, "
            "token_expires_at = ?, status = CASE WHEN status = 'reauth_required' THEN 'ok' "
            "ELSE status END, refresh_lease_until = NULL, updated_at = ? "
            "WHERE id = ? AND secret_version = ?",
            (
                secretstore.encrypt(secret),
                expires_at,
                utcnow(),
                integration_id,
                expected_version,
            ),
        )
        return cur.rowcount == 1


async def access_token(integration_id: int, client: oauth.OAuthClient, db_path=None) -> str:
    """A usable access token for this integration, refreshing if needed."""
    db_path = db_path or get_settings().db_path

    row, secret = await asyncio.to_thread(_read, db_path, integration_id)
    token = _still_valid(row, secret)
    if token:
        return token

    async with _lock_for(integration_id):
        # Double-checked: whoever held the lock before us may have just rotated
        # it, and refreshing again would burn the token they stored.
        row, secret = await asyncio.to_thread(_read, db_path, integration_id)
        token = _still_valid(row, secret)
        if token:
            return token

        refresh_token = secret.get("refresh_token")
        if not refresh_token:
            await asyncio.to_thread(
                mark_reauth_required,
                db_path,
                integration_id,
                "No refresh token is stored for this account. Reconnect it.",
            )
            raise IntegrationAuthError(
                f"{row['account_label'] or row['provider']} needs reconnecting."
            )

        if not await asyncio.to_thread(_claim_lease, db_path, integration_id):
            # Another process is refreshing. Give it a moment, then use whatever
            # it stored rather than racing it.
            await asyncio.sleep(1.0)
            row, secret = await asyncio.to_thread(_read, db_path, integration_id)
            token = _still_valid(row, secret)
            if token:
                return token

        try:
            granted = await asyncio.to_thread(oauth.refresh, client, refresh_token)
        except oauth.OAuthError as exc:
            if exc.is_terminal:
                # The grant is dead. Retrying burns quota and hides the only
                # thing that fixes it, which is the user reconnecting.
                await asyncio.to_thread(
                    mark_reauth_required, db_path, integration_id, exc.message
                )
                raise IntegrationAuthError(
                    f"{row['account_label'] or row['provider']} needs reconnecting: {exc.message}"
                ) from exc
            raise

        updated = dict(secret)
        updated["access_token"] = granted["access_token"]
        # Providers that rotate refresh tokens send a new one; those that do not
        # omit it, and the existing one stays valid.
        if granted.get("refresh_token"):
            updated["refresh_token"] = granted["refresh_token"]

        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=int(granted.get("expires_in", 3600)))
        ).isoformat()

        stored = await asyncio.to_thread(
            _store, db_path, integration_id, row["secret_version"], updated, expires_at
        )
        if not stored:
            # Lost the CAS: someone else rotated concurrently. Ours may already
            # be invalidated, and writing it would orphan theirs.
            log.info("discarding a concurrently superseded token for integration %s", integration_id)
            _, secret = await asyncio.to_thread(_read, db_path, integration_id)
            return secret.get("access_token") or updated["access_token"]

        return updated["access_token"]


def reset_locks() -> None:
    """Drop the per-integration locks. Tests use this between event loops."""
    _locks.clear()
