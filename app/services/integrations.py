"""Per-user calendar/email integrations: storage, masking, and the MCP migration.

An integration is one connected *account*. Credentials never leave this module in
readable form -- :func:`row_to_dict` reports ``has_secret`` and a masked tail, and
that is all any API response ever carries.
"""

from __future__ import annotations

import json
import re
import sqlite3

from app.db import get_conn, utcnow
from app.errors import ConflictError, NotFoundError, ValidationError
from app.logging_config import get_logger
from app.services import secretstore
from app.services.providers import registry

log = get_logger("integrations")

MASK = "••••"
MIGRATION_KEY = "integrations_migrated_from_mcp"


def mask(value: str | None) -> str | None:
    """Show only the tail so the UI can round-trip without leaking the secret."""
    if not value:
        return None
    return f"{MASK}{value[-4:]}" if len(value) > 4 else MASK


def resolve_secret_update(value: str | None) -> tuple[str | None, bool]:
    """Interpret the tri-state secret field the forms submit.

    Unset means leave alone, empty string means clear, a masked echo of what was
    displayed means leave alone, anything else is a genuinely new value. Returns
    ``(new_value, clear)``.
    """
    if value == "":
        return None, True
    if value is not None and value.startswith(MASK):
        return None, False
    return value, False


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def row_to_dict(row: sqlite3.Row) -> dict:
    """API shape. Deliberately never includes ``secret_json``."""
    try:
        spec = registry.spec(row["provider"])
        label = spec.label
        kinds = sorted(spec.kinds)
    except NotFoundError:
        label = row["provider"]
        kinds = []

    secret_preview = None
    if row["secret_json"]:
        try:
            secret = secretstore.decrypt(row["secret_json"])
            # Whichever field this provider keeps its long-lived credential in.
            for key in ("auth_token", "refresh_token", "password"):
                if secret.get(key):
                    secret_preview = mask(str(secret[key]))
                    break
        except secretstore.SecretDecryptError:
            secret_preview = None

    return {
        "id": row["id"],
        "provider": row["provider"],
        "provider_label": label,
        "supported_kinds": kinds,
        "account_key": row["account_key"],
        "account_label": row["account_label"],
        "calendar_enabled": bool(row["calendar_enabled"]),
        "email_enabled": bool(row["email_enabled"]),
        "enabled": bool(row["enabled"]),
        "auth_type": row["auth_type"],
        "config": json.loads(row["config_json"] or "{}"),
        "has_secret": bool(row["secret_json"]),
        "secret_preview": secret_preview,
        "status": row["status"],
        "scopes": row["scopes"],
        "token_expires_at": row["token_expires_at"],
        "last_test": {
            "at": row["last_test_at"],
            "ok": None if row["last_test_ok"] is None else bool(row["last_test_ok"]),
            "error": row["last_test_error"],
        },
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_for_user(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM integrations WHERE user_id = ? ORDER BY provider, id", (user_id,)
    ).fetchall()
    return [row_to_dict(r) for r in rows]


def require_own(conn: sqlite3.Connection, integration_id: int, user_id: int) -> sqlite3.Row:
    """Fetch an integration the caller owns.

    Someone else's row is a 404, not a 403 -- a 403 would confirm it exists.
    """
    row = conn.execute(
        "SELECT * FROM integrations WHERE id = ? AND user_id = ?",
        (integration_id, user_id),
    ).fetchone()
    if row is None:
        raise NotFoundError("Integration not found")
    return row


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


def _validate_kinds(provider: str, calendar: bool, email: bool) -> None:
    supported = registry.supported_kinds(provider)
    if calendar and registry.CALENDAR not in supported:
        raise ValidationError(f"{provider} cannot search a calendar")
    if email and registry.EMAIL not in supported:
        raise ValidationError(f"{provider} cannot search email")


def derive_account_key(
    provider: str, config: dict, secret: dict, label: str | None = None
) -> str:
    """Work out the stable identity of the account being connected.

    Must not come from anything the user can rename, and must not be blank --
    SQLite treats NULLs as distinct in a UNIQUE index, so an empty key would let
    the same account be connected over and over.

    OAuth providers do not use this: their identity comes from an identity call
    made during the callback, which is the only way to know *which* account was
    actually authorized.
    """
    spec = registry.spec(provider)

    if spec.auth_type == "token":
        # An MCP account is (which server, whose profile on it).
        base = (config.get("base_url") or config.get("command") or "").rstrip("/")
        profile = config.get("profile") or "default"
        if not base:
            raise ValidationError("A server URL is required")
        return f"{base}:{profile}"

    if spec.auth_type == "none":
        # The Development provider has no credential to identify, so its key is
        # a slug of the label -- the one case where the key is derived from
        # something renameable. It is snapshotted here and never rewritten, so
        # the unique index still does its documented job of stopping repeated
        # Connect clicks piling up rows, while two accounts under two labels
        # stay distinct (which is what makes multi-account failures testable).
        slug = re.sub(r"[^a-z0-9]+", "-", (label or "").strip().lower()).strip("-")
        return slug or "default"

    if spec.auth_type == "password":
        username = (secret.get("username") or config.get("username") or "").strip()
        if not username:
            raise ValidationError("A username is required")
        return username.lower()

    raise ValidationError(
        f"{spec.label} accounts are connected by authorising them, not by this form"
    )


def create(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    provider: str,
    account_key: str,
    account_label: str | None = None,
    calendar_enabled: bool | None = None,
    email_enabled: bool | None = None,
    config: dict | None = None,
    secret: dict | None = None,
    auth_type: str | None = None,
    status: str = "unverified",
    scopes: str | None = None,
    token_expires_at: str | None = None,
) -> int:
    spec = registry.spec(provider)

    # Default to everything the provider can do: someone connecting an account
    # wants it used, and a single-capability provider has nothing to choose.
    if calendar_enabled is None:
        calendar_enabled = registry.CALENDAR in spec.kinds
    if email_enabled is None:
        email_enabled = registry.EMAIL in spec.kinds
    _validate_kinds(provider, calendar_enabled, email_enabled)

    if not account_key:
        raise ValidationError("An integration needs a stable account key")

    now = utcnow()
    try:
        cur = conn.execute(
            """
            INSERT INTO integrations (user_id, provider, account_key, account_label,
                calendar_enabled, email_enabled, enabled, auth_type, config_json,
                secret_json, secret_version, scopes, token_expires_at, status,
                created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 1, ?, ?, ?, ?, ?)
            """,
            (
                user_id, provider, account_key, account_label or account_key,
                int(calendar_enabled), int(email_enabled),
                auth_type or spec.auth_type,
                json.dumps(config or {}),
                secretstore.encrypt(secret) if secret else None,
                scopes, token_expires_at, status, now, now,
            ),
        )
    except sqlite3.IntegrityError:
        raise ConflictError(
            f"That {spec.label} account is already connected"
        ) from None
    return cur.lastrowid


def update(
    conn: sqlite3.Connection,
    integration_id: int,
    user_id: int,
    *,
    account_label: str | None = None,
    calendar_enabled: bool | None = None,
    email_enabled: bool | None = None,
    enabled: bool | None = None,
    config: dict | None = None,
    secret_updates: dict | None = None,
) -> sqlite3.Row:
    row = require_own(conn, integration_id, user_id)

    calendar = row["calendar_enabled"] if calendar_enabled is None else calendar_enabled
    email = row["email_enabled"] if email_enabled is None else email_enabled
    _validate_kinds(row["provider"], bool(calendar), bool(email))

    merged_config = json.loads(row["config_json"] or "{}")
    if config:
        merged_config.update(config)

    secret_json = row["secret_json"]
    if secret_updates:
        try:
            existing = secretstore.decrypt(secret_json)
        except secretstore.SecretDecryptError:
            # Unreadable anyway; a fresh secret is exactly the fix.
            existing = {}
        for key, raw in secret_updates.items():
            value, clear = resolve_secret_update(raw)
            if clear:
                existing.pop(key, None)
            elif value is not None:
                existing[key] = value
        secret_json = secretstore.encrypt(existing) if existing else None

    # Supplying working credentials clears a reauth flag; that is the whole point
    # of the form the user just submitted.
    status = row["status"]
    if secret_updates and status == "reauth_required":
        status = "unverified"

    conn.execute(
        """
        UPDATE integrations SET account_label = ?, calendar_enabled = ?, email_enabled = ?,
            enabled = ?, config_json = ?, secret_json = ?, status = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            account_label if account_label is not None else row["account_label"],
            int(bool(calendar)), int(bool(email)),
            int(row["enabled"] if enabled is None else bool(enabled)),
            json.dumps(merged_config), secret_json, status, utcnow(), integration_id,
        ),
    )
    return require_own(conn, integration_id, user_id)


def delete(conn: sqlite3.Connection, integration_id: int, user_id: int) -> None:
    """Disconnect an account.

    Already-attached events and emails are left alone: they are copies on the
    thread, so a meeting's history does not evaporate because a token was revoked.
    """
    require_own(conn, integration_id, user_id)
    conn.execute("DELETE FROM integrations WHERE id = ?", (integration_id,))


def record_test(conn: sqlite3.Connection, integration_id: int, result: dict) -> None:
    ok = bool(result.get("ok"))
    conn.execute(
        "UPDATE integrations SET last_test_at = ?, last_test_ok = ?, last_test_error = ?, "
        "status = CASE WHEN ? THEN 'ok' ELSE status END, updated_at = ? WHERE id = ?",
        (utcnow(), int(ok), result.get("error"), int(ok), utcnow(), integration_id),
    )


# --------------------------------------------------------------------------- #
# Migration from the shared-MCP era
# --------------------------------------------------------------------------- #


def migrate_mcp_servers(db_path=None) -> int:
    """Turn shared ``mcp_servers`` config into per-user integrations.

    Runs once, guarded by a marker in ``app_settings``. Every existing user gets an
    integration per configured server, using their ``user_mcp_profiles`` override
    where they had one and the shared account otherwise -- so nobody's matching
    quietly stops working across the upgrade.

    The old tables are left in place. Dropping them buys nothing on a single-file
    SQLite database and costs the rollback path.
    """
    created = 0
    with get_conn(db_path) as conn:
        marker = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (MIGRATION_KEY,)
        ).fetchone()
        if marker and marker["value"] == "1":
            return 0

        servers = conn.execute("SELECT * FROM mcp_servers").fetchall()
        users = conn.execute("SELECT id FROM users").fetchall()

        for server in servers:
            provider = (
                "mcp_calendar" if server["kind"] == "calendar" else "mcp_email"
            )
            # Nothing to reach means nothing worth migrating -- a bare .env leaves
            # the seeded rows with no base_url and no command.
            if not (server["base_url"] or server["command"]):
                continue

            config = {
                "transport": server["transport"],
                "base_url": server["base_url"],
                "tool_name": server["tool_name"],
                "timeout_sec": server["timeout_sec"],
                "command": server["command"],
                "args": json.loads(server["args_json"] or "[]"),
                "cwd": server["cwd"],
                "env": json.loads(server["env_json"] or "{}"),
            }

            for user in users:
                override = conn.execute(
                    "SELECT * FROM user_mcp_profiles WHERE user_id = ? AND server_name = ?",
                    (user["id"], server["name"]),
                ).fetchone()

                profile = (override["profile"] if override else None) or server["default_profile"]
                token = (
                    (override["auth_token"] if override else None) or server["auth_token"]
                )

                user_config = dict(config, profile=profile)
                try:
                    integration_id = create(
                        conn,
                        user_id=user["id"],
                        provider=provider,
                        # One row per (server, account). Two users on the same
                        # shared profile still get a row each, because
                        # integrations are per-user by construction.
                        account_key=f"{server['name']}:{profile or 'default'}",
                        account_label=f"{server['name']} ({profile or 'default'})",
                        config=user_config,
                        secret={"auth_token": token} if token else None,
                    )
                except ConflictError:
                    # Already migrated this pair; the marker write below is the
                    # normal guard, this covers a half-finished earlier run.
                    continue

                if not server["enabled"]:
                    conn.execute(
                        "UPDATE integrations SET enabled = 0 WHERE id = ?",
                        (integration_id,),
                    )
                created += 1

        conn.execute(
            "INSERT INTO app_settings (key, value, value_type, is_secret, updated_at) "
            "VALUES (?, '1', 'str', 0, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = '1', updated_at = excluded.updated_at",
            (MIGRATION_KEY, utcnow()),
        )

    if created:
        log.info("migrated %d MCP server/user pair(s) into per-user integrations", created)
    return created
