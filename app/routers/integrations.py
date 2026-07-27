"""Per-user calendar and email integrations.

Everything here is scoped to the caller. There is no admin view and no shared
configuration: an integration is *my* account, and somebody else's row is a 404
rather than a 403, because a 403 would confirm it exists.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from app.deps import CurrentUser, active_user, get_db
from app.logging_config import get_logger
from app.schemas import (
    CreateIntegrationRequest,
    IntegrationOut,
    IntegrationSummaryOut,
    ProviderSpecOut,
    UpdateIntegrationRequest,
)
from app.services import integrations as svc
from app.services.providers import loader, registry

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
