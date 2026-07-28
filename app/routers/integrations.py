"""Per-user calendar and email integrations.

Everything here is scoped to the caller. There is no admin view and no shared
configuration: an integration is *my* account, and somebody else's row is a 404
rather than a 403, because a 403 would confirm it exists.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Cookie, Depends, Response
from fastapi.responses import RedirectResponse

from app.config import get_settings
from app.db import utcnow
from app.deps import CurrentUser, active_user, get_db
from app.errors import ValidationError
from app.logging_config import get_logger
from app.schemas import (
    CreateIntegrationRequest,
    IntegrationOut,
    IntegrationSummaryOut,
    ProviderSpecOut,
    UpdateIntegrationRequest,
)
from app.services import integrations as svc
from app.services.providers import google, loader, oauth, registry, zoho

router = APIRouter(prefix="/api/integrations", tags=["integrations"])
log = get_logger("integrations_api")


# Both static paths are declared before /{integration_id} so they are matched as
# literals rather than being parsed as an id.
@router.get("/providers", response_model=list[ProviderSpecOut])
def list_providers(_: CurrentUser = Depends(active_user)) -> list[ProviderSpecOut]:
    """What can be connected. Drives the "Add integration" picker."""
    return [
        ProviderSpecOut(
            id=spec.id,
            label=spec.label,
            kinds=sorted(spec.kinds),
            auth_type=spec.auth_type,
            docs_url=spec.docs_url,
        )
        for spec in registry.all_specs()
    ]


@router.get("/summary", response_model=IntegrationSummaryOut)
def my_summary(
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> IntegrationSummaryOut:
    """Counts the SPA needs *before* offering to match a meeting."""
    return IntegrationSummaryOut(**loader.summary_for_user(conn, user.id))


# --------------------------------------------------------------------------- #
# OAuth connect flow
#
# Declared before /{integration_id} so "oauth" is never parsed as an id.
# --------------------------------------------------------------------------- #

# Only OAuth providers appear here; token/password providers are created
# directly through POST /api/integrations.
#
# Each entry is (build_client, fetch_identity, initial_config). The third pins
# anything the account needs remembering at connect time -- Zoho's data centre,
# for instance, which is a property of the account rather than of the app.
OAUTH_CLIENTS = {
    "google": (google.client_for, google.fetch_identity, lambda conn: {}),
    "zoho": (
        zoho.client_for,
        zoho.fetch_identity,
        lambda conn: {"dc": zoho.data_centre(conn)},
    ),
}


@router.get("/oauth/{provider}/start")
def oauth_start(
    provider: str,
    response: Response,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Begin authorising an account. Returns the URL for the SPA to navigate to."""
    if provider not in OAUTH_CLIENTS:
        raise ValidationError(f"{provider} is not connected by authorising it")

    build_client, _, _initial = OAUTH_CLIENTS[provider]
    client = build_client(conn)
    redirect = oauth.redirect_uri(conn, provider)
    nonce = oauth.new_nonce()
    state = oauth.make_state(user.id, provider, nonce)

    # The nonce lives in an httpOnly cookie as well as inside the signed state,
    # so a state lifted out of a redirect URL cannot be replayed elsewhere.
    response.set_cookie(
        oauth.STATE_COOKIE,
        nonce,
        max_age=oauth.STATE_TTL_SEC,
        httponly=True,
        samesite="lax",
        secure=get_settings().session_cookie_secure,
    )
    return {"authorize_url": oauth.authorize_url(client, redirect, state)}


@router.get("/oauth/{provider}/callback")
def oauth_callback(
    provider: str,
    state: str = "",
    code: str = "",
    error: str = "",
    mmn_oauth_nonce: str | None = Cookie(default=None),
    conn: sqlite3.Connection = Depends(get_db),
) -> RedirectResponse:
    """Finish authorising and store the account.

    Deliberately not authenticated by session: the browser arrives here from
    Google, and the signed state plus the nonce cookie are what prove who started
    the flow. The row is created only *after* the identity call, so a repeated
    "Connect" cannot pile up half-built rows.
    """
    settings_url = "/settings/integrations"

    if error:
        return RedirectResponse(f"{settings_url}?error={error}", status_code=303)
    if provider not in OAUTH_CLIENTS:
        return RedirectResponse(f"{settings_url}?error=unknown_provider", status_code=303)

    build_client, fetch_identity, initial_config = OAUTH_CLIENTS[provider]
    payload = oauth.parse_state(state, mmn_oauth_nonce)
    user_id = payload["u"]

    client = build_client(conn)
    granted = oauth.exchange_code(client, code, oauth.redirect_uri(conn, provider))
    identity = fetch_identity(granted["access_token"], conn)
    if not identity.get("account_key"):
        return RedirectResponse(f"{settings_url}?error=no_identity", status_code=303)

    secret = {"access_token": granted["access_token"]}
    if granted.get("refresh_token"):
        secret["refresh_token"] = granted["refresh_token"]

    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=int(granted.get("expires_in", 3600)))
    ).isoformat()

    existing = conn.execute(
        "SELECT id FROM integrations WHERE user_id = ? AND provider = ? AND account_key = ?",
        (user_id, provider, identity["account_key"]),
    ).fetchone()

    if existing:
        # Reconnecting an account we already know: refresh its credentials and
        # clear whatever failure state it was in.
        svc.update(
            conn,
            existing["id"],
            user_id,
            secret_updates={k: v for k, v in secret.items()},
        )
        conn.execute(
            "UPDATE integrations SET status = 'ok', token_expires_at = ?, scopes = ?, "
            "refresh_token_obtained_at = COALESCE(?, refresh_token_obtained_at) "
            "WHERE id = ?",
            (
                expires_at,
                granted.get("scope"),
                utcnow() if granted.get("refresh_token") else None,
                existing["id"],
            ),
        )
        log.info("user %s reconnected %s (%s)", user_id, provider, identity["email"])
    else:
        svc.create(
            conn,
            user_id=user_id,
            provider=provider,
            account_key=identity["account_key"],
            account_label=identity.get("email") or identity["account_key"],
            config=initial_config(conn),
            secret=secret,
            status="ok",
            scopes=granted.get("scope"),
            token_expires_at=expires_at,
        )
        log.info("user %s connected %s (%s)", user_id, provider, identity["email"])

    redirect = RedirectResponse(f"{settings_url}?connected={provider}", status_code=303)
    redirect.delete_cookie(oauth.STATE_COOKIE)
    return redirect


@router.get("", response_model=list[IntegrationOut])
def list_mine(
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[IntegrationOut]:
    return [IntegrationOut(**row) for row in svc.list_for_user(conn, user.id)]


@router.post("", response_model=IntegrationOut, status_code=201)
def create_integration(
    payload: CreateIntegrationRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> IntegrationOut:
    """Connect an account that authenticates with a token or a password.

    OAuth providers are not connectable here -- they go through
    /api/integrations/oauth/{provider}/start, because only the callback knows
    which account was actually authorized.
    """
    account_key = svc.derive_account_key(payload.provider, payload.config, payload.secret)
    integration_id = svc.create(
        conn,
        user_id=user.id,
        provider=payload.provider,
        account_key=account_key,
        account_label=payload.account_label,
        calendar_enabled=payload.calendar_enabled,
        email_enabled=payload.email_enabled,
        config=payload.config,
        secret=payload.secret or None,
    )
    log.info("user %s connected %s (%s)", user.username, payload.provider, account_key)
    return IntegrationOut(**svc.row_to_dict(svc.require_own(conn, integration_id, user.id)))


@router.patch("/{integration_id}", response_model=IntegrationOut)
def update_integration(
    integration_id: int,
    payload: UpdateIntegrationRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> IntegrationOut:
    row = svc.update(
        conn,
        integration_id,
        user.id,
        account_label=payload.account_label,
        calendar_enabled=payload.calendar_enabled,
        email_enabled=payload.email_enabled,
        enabled=payload.enabled,
        config=payload.config,
        secret_updates=payload.secret,
    )
    return IntegrationOut(**svc.row_to_dict(row))


@router.delete("/{integration_id}")
def delete_integration(
    integration_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    svc.delete(conn, integration_id, user.id)
    log.info("user %s disconnected integration %s", user.username, integration_id)
    return {"ok": True}


@router.post("/{integration_id}/test")
async def test_integration(
    integration_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Verify a connected account, reporting each leg separately.

    A provider can be half-working -- Apple's CalDAV succeeding while IMAP login
    is rejected is the obvious case -- so the result carries per-check detail
    rather than a single flag.
    """
    row = svc.require_own(conn, integration_id, user.id)

    provider = loader.build_provider(conn, row)
    if provider is None:
        result = {
            "ok": False,
            "latency_ms": 0,
            "checks": [],
            "error": "This account's stored credentials could not be read. Reconnect it.",
        }
    else:
        result = await provider.test()

    svc.record_test(conn, integration_id, result)
    log.info(
        "user %s tested integration %s (%s): ok=%s",
        user.username, integration_id, row["provider"], result.get("ok"),
    )
    return result
