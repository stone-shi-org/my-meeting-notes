"""Authoring the Development provider's fake data.

Every route here 404s when ``MMN_DEV_PROVIDER_ENABLED`` is off. A 404 rather
than a 403: on a server that does not have this feature these paths genuinely do
not exist, and saying "forbidden" would advertise a surface that is not there.

Ownership works exactly as it does everywhere else -- ``require_own`` on the
integration, and someone else's is a 404. Item routes resolve their integration
from the item and then run the same check, so an id guessed out of thin air
cannot reach another user's fixtures.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.config import get_settings
from app.deps import CurrentUser, active_user, get_db
from app.errors import NotFoundError, ValidationError
from app.logging_config import get_logger
from app.schemas import DevGenerateRequest, DevImportRequest, DevItemRequest
from app.services import dev_data
from app.services import integrations as integrations_svc
from app.services.providers import dev as dev_provider

router = APIRouter(prefix="/api/dev", tags=["dev"])
log = get_logger("dev")


def require_dev_enabled() -> None:
    if not dev_provider.enabled():
        raise NotFoundError("Development data is not enabled on this server")


def _own_integration(
    conn: sqlite3.Connection, integration_id: int, user: CurrentUser
) -> sqlite3.Row:
    row = integrations_svc.require_own(conn, integration_id, user.id)
    if row["provider"] != dev_provider.PROVIDER_ID:
        raise ValidationError("That integration is not a Development account")
    return row


def _own_item(
    conn: sqlite3.Connection, kind: str, item_id: int, user: CurrentUser
) -> sqlite3.Row:
    item = dev_data.get_item(conn, kind, item_id)
    _own_integration(conn, item["integration_id"], user)
    return item


# --------------------------------------------------------------------------- #
# Items
#
# One pair of routes per kind rather than a `{kind}` path parameter: the segment
# would be interpolated straight into a table name, and a map lookup that cannot
# be reached from the URL is the safer shape.
# --------------------------------------------------------------------------- #


@router.get("/integrations/{integration_id}/emails", dependencies=[Depends(require_dev_enabled)])
def list_emails(
    integration_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    _own_integration(conn, integration_id, user)
    return dev_data.list_items(conn, "emails", integration_id)


@router.post(
    "/integrations/{integration_id}/emails",
    status_code=201,
    dependencies=[Depends(require_dev_enabled)],
)
def create_email(
    integration_id: int,
    payload: DevItemRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    _own_integration(conn, integration_id, user)
    return dev_data.create_item(conn, "emails", integration_id, payload.model_dump())


@router.patch("/emails/{item_id}", dependencies=[Depends(require_dev_enabled)])
def update_email(
    item_id: int,
    payload: DevItemRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    _own_item(conn, "emails", item_id, user)
    return dev_data.update_item(conn, "emails", item_id, payload.model_dump())


@router.delete("/emails/{item_id}", dependencies=[Depends(require_dev_enabled)])
def delete_email(
    item_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    _own_item(conn, "emails", item_id, user)
    dev_data.delete_item(conn, "emails", item_id)
    return {"ok": True}


@router.get("/integrations/{integration_id}/events", dependencies=[Depends(require_dev_enabled)])
def list_events(
    integration_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    _own_integration(conn, integration_id, user)
    return dev_data.list_items(conn, "events", integration_id)


@router.post(
    "/integrations/{integration_id}/events",
    status_code=201,
    dependencies=[Depends(require_dev_enabled)],
)
def create_event(
    integration_id: int,
    payload: DevItemRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    _own_integration(conn, integration_id, user)
    return dev_data.create_item(conn, "events", integration_id, payload.model_dump())


@router.patch("/events/{item_id}", dependencies=[Depends(require_dev_enabled)])
def update_event(
    item_id: int,
    payload: DevItemRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    _own_item(conn, "events", item_id, user)
    return dev_data.update_item(conn, "events", item_id, payload.model_dump())


@router.delete("/events/{item_id}", dependencies=[Depends(require_dev_enabled)])
def delete_event(
    item_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    _own_item(conn, "events", item_id, user)
    dev_data.delete_item(conn, "events", item_id)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Export / import
# --------------------------------------------------------------------------- #


@router.get("/integrations/{integration_id}/export", dependencies=[Depends(require_dev_enabled)])
def export_data(
    integration_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Everything authored on this account, as a file you can keep.

    Items are CASCADE-deleted with the integration, so this is the only way an
    authored fixture set survives disconnecting the account.
    """
    _own_integration(conn, integration_id, user)
    return dev_data.export_items(conn, integration_id)


@router.post("/integrations/{integration_id}/import", dependencies=[Depends(require_dev_enabled)])
def import_data(
    integration_id: int,
    payload: DevImportRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Add items from an export. Additive -- it never replaces what is there."""
    _own_integration(conn, integration_id, user)
    counts = dev_data.import_items(
        conn, integration_id, {"emails": payload.emails, "events": payload.events}
    )
    log.info("user %s imported %s into integration %s", user.username, counts, integration_id)
    return {"ok": True, "imported": counts}


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #


@router.post("/integrations/{integration_id}/generate", dependencies=[Depends(require_dev_enabled)])
async def generate(
    integration_id: int,
    payload: DevGenerateRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> StreamingResponse:
    """Draft items around a thread, for the caller to review.

    Streamed as SSE (``progress``/``done``/``error``), the same contract as
    ``chat.send_chat_message``: a batch generation is a long enough LLM call
    that one blocking POST left the connection silent long enough for a proxy
    or the browser to give up on it. Returns drafts and writes nothing --
    accepting one POSTs it back through the ordinary create route, so there is
    a single write path and a model that returns half a batch of nonsense
    costs you a click rather than a cleanup.

    Ownership is checked synchronously here, before the stream starts, exactly
    like ``chat.send_chat_message`` checks thread access up front.
    """
    _own_integration(conn, integration_id, user)
    # Authorise the thread as its own object -- a thread id is not covered by
    # owning the integration.
    thread = conn.execute(
        "SELECT owner_id FROM threads WHERE id = ?", (payload.thread_id,)
    ).fetchone()
    if thread is None or (thread["owner_id"] != user.id and not user.is_admin):
        raise NotFoundError("Thread not found")

    log.info(
        "user %s generating dev draft(s) for thread %s", user.username, payload.thread_id
    )
    return StreamingResponse(
        dev_data.stream_generate_response(
            get_settings().db_path,
            thread_id=payload.thread_id,
            count=payload.count,
            model=payload.model,
            additional_prompt=payload.additional_prompt,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
