"""Turn a user's ``integrations`` rows into live provider objects.

``matching`` depends on this module and nothing else in the package, which is what
keeps the fan-out logic free of any per-provider knowledge and gives the test suite
a single seam to fake.
"""

from __future__ import annotations

import json
import sqlite3

from app.db import utcnow
from app.logging_config import get_logger
from app.services import secretstore
from app.services.providers import dev, registry
from app.services.providers.base import BaseProvider, IntegrationRef

log = get_logger("providers.loader")


def row_to_ref(row: sqlite3.Row) -> IntegrationRef:
    return IntegrationRef(
        id=row["id"],
        provider=row["provider"],
        account_label=row["account_label"] or "",
        calendar_enabled=bool(row["calendar_enabled"]),
        email_enabled=bool(row["email_enabled"]),
    )


def _mark_unreadable(conn: sqlite3.Connection, integration_id: int, message: str) -> None:
    """A credential we cannot decrypt is a reconnect, never a 500.

    Most likely cause is a changed or lost encryption key. Flagging the row means
    Settings can say which account to reconnect instead of the whole feature
    failing with an opaque error.
    """
    conn.execute(
        "UPDATE integrations SET status = 'reauth_required', last_test_ok = 0, "
        "last_test_error = ?, last_test_at = ?, updated_at = ? WHERE id = ?",
        (message, utcnow(), utcnow(), integration_id),
    )


def build_provider(conn: sqlite3.Connection, row: sqlite3.Row) -> BaseProvider | None:
    """Instantiate one provider, or None if it cannot be used at all."""
    try:
        provider_spec = registry.spec(row["provider"])
    except Exception:
        # A row for a provider this build no longer ships. Skip it rather than
        # breaking every other integration the user has.
        log.warning("integration %s names unknown provider %r", row["id"], row["provider"])
        return None

    if row["provider"] == dev.PROVIDER_ID and not dev.enabled():
        # A Development account left behind on a build that has the flag off.
        # Going inert is the point: the row keeps its authored data, but nothing
        # it holds reaches a match run.
        log.info("integration %s is a dev account and dev data is disabled", row["id"])
        return None

    try:
        secret = secretstore.decrypt(row["secret_json"])
    except secretstore.SecretDecryptError as exc:
        log.warning("integration %s has an unreadable secret: %s", row["id"], exc)
        _mark_unreadable(conn, row["id"], str(exc))
        return None

    try:
        config = json.loads(row["config_json"] or "{}")
    except ValueError:
        log.warning("integration %s has malformed config_json", row["id"])
        config = {}

    return provider_spec.factory(row_to_ref(row), config, secret)


def load_for_user(
    conn: sqlite3.Connection, user_id: int | None, *, kind: str | None = None
) -> list[BaseProvider]:
    """Every usable provider for this user, optionally filtered to one capability.

    Rows whose ``status`` is already ``reauth_required`` are still returned: the
    provider will fail its own call and that failure gets recorded against the
    account by name, which is far more useful than the account silently vanishing
    from the search.
    """
    if user_id is None:
        return []

    rows = conn.execute(
        "SELECT * FROM integrations WHERE user_id = ? AND enabled = 1 ORDER BY id",
        (user_id,),
    ).fetchall()

    providers: list[BaseProvider] = []
    for row in rows:
        if kind == registry.CALENDAR and not row["calendar_enabled"]:
            continue
        if kind == registry.EMAIL and not row["email_enabled"]:
            continue
        if kind is None and not (row["calendar_enabled"] or row["email_enabled"]):
            continue

        provider = build_provider(conn, row)
        if provider is not None:
            providers.append(provider)

    return providers


def summary_for_user(conn: sqlite3.Connection, user_id: int) -> dict:
    """Counts behind the greyed-out match button, plus who needs reconnecting.

    The SPA needs this *before* the click: starting a match returns 202, so a
    failure raised inside the job would surface as a failed job rather than
    something the button could have prevented.
    """
    rows = conn.execute(
        "SELECT id, provider, account_label, calendar_enabled, email_enabled, status "
        "FROM integrations WHERE user_id = ? AND enabled = 1",
        (user_id,),
    ).fetchall()

    return {
        "calendar": sum(1 for r in rows if r["calendar_enabled"]),
        "email": sum(1 for r in rows if r["email_enabled"]),
        "needs_reauth": [
            {
                "id": r["id"],
                "provider": r["provider"],
                "account_label": r["account_label"] or r["provider"],
            }
            for r in rows
            if r["status"] == "reauth_required"
        ],
    }
