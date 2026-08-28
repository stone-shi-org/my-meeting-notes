"""Authoring the Development provider's fake inbox and calendar.

Storage plus one LLM call. The provider that *reads* these rows lives in
``providers/dev.py``; this module is only how they get written.

Everything is scoped to an integration, which is what
``routers/dev.py`` has already checked the caller owns. Nothing here re-derives
ownership from a user id -- there is one authorisation point, and it is the route.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

from app.db import get_conn, utcnow
from app.errors import AppError, LLMError, NotFoundError, ValidationError
from app.logging_config import get_logger
from app.services import llm as llm_svc
from app.services import prompts as prompts_svc
from app.services.providers.dev import DATE_MODES, MAX_REPEAT

log = get_logger("dev_data")

# Same value as chat.py/home_chat.py/meeting_chat.py's KEEPALIVE_SEC: long enough
# that a normal LLM call never triggers it, short enough that an intermediate
# proxy never decides a silent connection is dead.
KEEPALIVE_SEC = 15.0

# What a client may set, per table. Writing the list out rather than accepting a
# dict keeps a typo in the SPA from silently creating nothing, and keeps
# integration_id and the timestamps off the wire.
EMAIL_FIELDS = (
    "subject", "sender", "snippet", "account",
    "date_mode", "at", "offset_minutes", "anchor_meeting_id",
    "rfc2822_date", "expected_relevant",
)
EVENT_FIELDS = (
    "summary", "description", "location", "attendees_json", "calendar_name",
    "event_type", "duration_minutes",
    "date_mode", "at", "offset_minutes", "anchor_meeting_id",
    "all_day", "repeat_weekly", "expected_relevant",
)

TABLES = {"emails": ("dev_emails", EMAIL_FIELDS), "events": ("dev_events", EVENT_FIELDS)}

# Bounded like every other free-text field that reaches the model or the database.
TEXT_MAX = 4000
MAX_GENERATED = 20


def _table(kind: str) -> tuple[str, tuple[str, ...]]:
    try:
        return TABLES[kind]
    except KeyError:
        raise NotFoundError(f"Unknown dev item kind {kind!r}") from None


def row_to_dict(kind: str, row: sqlite3.Row) -> dict:
    _, fields = _table(kind)
    out = {"id": row["id"], "integration_id": row["integration_id"]}
    for field in fields:
        value = row[field]
        if field in ("rfc2822_date", "all_day", "expected_relevant"):
            value = bool(value)
        out[field] = value
    if kind == "events":
        try:
            out["attendees"] = json.loads(row["attendees_json"] or "[]")
        except ValueError:
            out["attendees"] = []
    out["created_at"] = row["created_at"]
    out["updated_at"] = row["updated_at"]
    return out


def _clean(kind: str, payload: dict) -> dict:
    """Validate and narrow a client payload to the columns it may set."""
    _, fields = _table(kind)
    values = {k: v for k, v in payload.items() if k in fields}

    mode = values.get("date_mode")
    if mode is not None and mode not in DATE_MODES:
        raise ValidationError(f"date_mode must be one of {', '.join(DATE_MODES)}")
    if mode == "absolute" and not values.get("at"):
        raise ValidationError("An absolute item needs a date")
    if mode == "anchored" and not values.get("anchor_meeting_id"):
        raise ValidationError("An anchored item needs a meeting to anchor to")

    repeat = values.get("repeat_weekly")
    if repeat is not None and not (1 <= int(repeat) <= MAX_REPEAT):
        raise ValidationError(f"repeat_weekly must be between 1 and {MAX_REPEAT}")

    for key in ("subject", "summary", "snippet", "description"):
        if isinstance(values.get(key), str) and len(values[key]) > TEXT_MAX:
            values[key] = values[key][:TEXT_MAX]

    # Attendees arrive as a list and are stored as JSON, the same shape the
    # provider hands to base.clean_attendees.
    if "attendees" in payload:
        attendees = payload["attendees"] or []
        if not isinstance(attendees, list):
            raise ValidationError("attendees must be a list of names")
        values["attendees_json"] = json.dumps([str(a) for a in attendees][:24])

    for key in ("rfc2822_date", "all_day", "expected_relevant"):
        if key in values:
            values[key] = int(bool(values[key]))

    return values


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #


def list_items(conn: sqlite3.Connection, kind: str, integration_id: int) -> list[dict]:
    table, _ = _table(kind)
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE integration_id = ? ORDER BY id DESC",
        (integration_id,),
    ).fetchall()
    return [row_to_dict(kind, r) for r in rows]


def get_item(conn: sqlite3.Connection, kind: str, item_id: int) -> sqlite3.Row:
    table, _ = _table(kind)
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise NotFoundError("Item not found")
    return row


def create_item(
    conn: sqlite3.Connection, kind: str, integration_id: int, payload: dict
) -> dict:
    table, _ = _table(kind)
    values = _clean(kind, payload)

    required = "subject" if kind == "emails" else "summary"
    if not (values.get(required) or "").strip():
        raise ValidationError(f"A {required} is required")

    now = utcnow()
    values["integration_id"] = integration_id
    values["created_at"] = now
    values["updated_at"] = now

    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    cur = conn.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", list(values.values())
    )
    return row_to_dict(kind, get_item(conn, kind, cur.lastrowid))


def update_item(conn: sqlite3.Connection, kind: str, item_id: int, payload: dict) -> dict:
    table, _ = _table(kind)
    values = _clean(kind, payload)
    if values:
        values["updated_at"] = utcnow()
        assignments = ", ".join(f"{k} = ?" for k in values)
        conn.execute(
            f"UPDATE {table} SET {assignments} WHERE id = ?", [*values.values(), item_id]
        )
    return row_to_dict(kind, get_item(conn, kind, item_id))


def delete_item(conn: sqlite3.Connection, kind: str, item_id: int) -> None:
    table, _ = _table(kind)
    if conn.execute(f"DELETE FROM {table} WHERE id = ?", (item_id,)).rowcount == 0:
        raise NotFoundError("Item not found")


# --------------------------------------------------------------------------- #
# Export / import
#
# Items are CASCADE-deleted with their account, so this is how authored fixtures
# survive a disconnect -- and how one gets shared with someone else's checkout.
# --------------------------------------------------------------------------- #


def export_items(conn: sqlite3.Connection, integration_id: int) -> dict:
    return {
        "emails": list_items(conn, "emails", integration_id),
        "events": list_items(conn, "events", integration_id),
    }


def import_items(conn: sqlite3.Connection, integration_id: int, payload: dict) -> dict:
    """Add items from an export. Additive: ids in the payload are ignored.

    Not a replace, because the common use is topping a fixture set up from
    someone else's file, and a silent wipe of what you had authored is not a
    recoverable mistake.
    """
    counts = {}
    for kind in TABLES:
        items = payload.get(kind) or []
        if not isinstance(items, list):
            raise ValidationError(f"{kind} must be a list")
        for item in items:
            create_item(conn, kind, integration_id, item)
        counts[kind] = len(items)
    return counts


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #


def thread_brief(conn: sqlite3.Connection, thread_id: int) -> dict:
    """What the model is told about the thread it is inventing traffic around."""
    thread = conn.execute(
        "SELECT id, title, description FROM threads WHERE id = ?", (thread_id,)
    ).fetchone()
    if thread is None:
        raise NotFoundError("Thread not found")

    meetings = conn.execute(
        """
        SELECT m.id, m.title, m.meeting_at,
               (SELECT s.tldr FROM summaries s
                 WHERE s.meeting_id = m.id AND s.is_current = 1 LIMIT 1) AS tldr
          FROM meetings m WHERE m.thread_id = ? ORDER BY m.meeting_at
        """,
        (thread_id,),
    ).fetchall()

    return {
        "title": thread["title"],
        "description": thread["description"] or "",
        "meetings": [
            {
                "id": m["id"],
                "title": m["title"],
                "meeting_at": m["meeting_at"],
                "tldr": (m["tldr"] or "")[:500],
            }
            for m in meetings
        ],
    }


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _stream_llm_json(config: llm_svc.LLMConfig, system: str, user: str,
                            queue: "asyncio.Queue[str | None]") -> dict:
    """Stream one LLM call, forwarding a ``progress`` frame as content arrives,
    and retry once if the reply is not parseable JSON -- the same retry
    ``chat_json`` does, just over ``achat_stream`` instead of one buffered POST.

    The generation is a single JSON object, not prose, so there is nothing
    sensible to render live token by token; ``progress`` only reports a running
    character count, purely so the caller can show the request is still alive
    instead of one long silent wait -- which is the actual bug this replaces.
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    payload = {
        "model": config.model,
        "messages": messages,
        "temperature": config.temperature,
        "stream": True,
        "include_reasoning": False,
    }

    async def _once() -> str:
        raw = ""
        async for delta in llm_svc.achat_stream(config, payload):
            raw += delta
            await queue.put(_sse("progress", {"chars": len(raw)}))
        return raw

    raw = await _once()
    try:
        return llm_svc.extract_json(raw)
    except LLMError:
        log.warning("dev seed generation returned unparseable JSON; retrying once")
        messages.extend(
            [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": "Your previous reply was not valid JSON. Return "
                               "only the JSON object, with no prose and no code fence.",
                },
            ]
        )
        raw = await _once()
        return llm_svc.extract_json(raw)


async def _produce_generate(
    queue: "asyncio.Queue[str | None]",
    db_path,
    thread_id: int,
    count: int,
    model: str | None,
    additional_prompt: str | None = None,
) -> None:
    """Draft fake email and calendar traffic around a thread. One LLM call,
    streamed over SSE (``progress``/``done``/``error``) instead of one blocking
    POST -- a batch generation runs long enough that a proxy or the browser
    gave up on a silent connection well before the model finished, the same
    fix as ``chat.stream_chat_response``.

    **Nothing is written.** The drafts come back for the caller to accept, edit
    or discard, and accepting goes through :func:`create_item` like the manual
    form does. That is what makes malformed model output a non-event rather than
    a half-populated table, and it keeps one write path.
    """
    try:
        count = max(1, min(count, MAX_GENERATED))

        with get_conn(db_path) as conn:
            brief = thread_brief(conn, thread_id)
            config = llm_svc.LLMConfig.from_db(conn, model_override=model)

        prompt = prompts_svc.load("dev_seed_prompt")
        if prompt.temperature is not None:
            config.temperature = prompt.temperature

        meetings_text = "\n".join(
            f"- id={m['id']} | {m['meeting_at']} | {m['title']}"
            + (f"\n    {m['tldr']}" if m["tldr"] else "")
            for m in brief["meetings"]
        ) or "(no meetings on this thread yet)"

        add_prompt_text = (
            f"Additional Requirements:\n{additional_prompt.strip()}"
            if additional_prompt and additional_prompt.strip()
            else ""
        )

        system, user = prompt.render(
            {
                "thread_title": brief["title"],
                "thread_description": brief["description"] or "(none)",
                "meetings": meetings_text,
                "count": str(count),
                "additional_prompt": add_prompt_text,
            }
        )

        parsed = await _stream_llm_json(config, system, user, queue)
        drafts = _coerce_drafts(parsed, brief)

        if not drafts:
            log.warning("dev seed generation returned nothing usable for thread %s", thread_id)

        await queue.put(_sse("done", {"drafts": drafts[:count], "model": config.model}))
    except AppError as exc:
        await queue.put(_sse("error", {"code": exc.code, "message": exc.message}))
    finally:
        await queue.put(None)


async def stream_generate_response(
    db_path,
    *,
    thread_id: int,
    count: int,
    model: str | None = None,
    additional_prompt: str | None = None,
):
    """SSE generator for ``StreamingResponse``, same shape as
    ``chat.stream_chat_response``: the real work runs in a background task
    feeding ``queue``, decoupled from this generator so a disconnected client
    does not cut off a generation that is already spending LLM budget.
    """
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    asyncio.create_task(
        _produce_generate(queue, db_path, thread_id, count, model, additional_prompt)
    )

    while True:
        try:
            item = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SEC)
        except asyncio.TimeoutError:
            yield ": keepalive\n\n"
            continue
        if item is None:
            return
        yield item


def _coerce_drafts(parsed: dict, brief: dict) -> list[dict]:
    """Keep what is usable, drop what is not.

    A model that invented a meeting id, forgot a subject or picked a date mode
    that does not exist should cost you that one draft, not the batch -- the same
    reasoning as ``base.coerce_attendees``, which drops an unrecognised attendee
    rather than failing the search.
    """
    known_meetings = {m["id"] for m in brief["meetings"]}
    out: list[dict] = []

    for raw in parsed.get("items") or []:
        if not isinstance(raw, dict):
            continue
        kind = "emails" if raw.get("kind") == "email" else "events"
        title = (raw.get("subject") or raw.get("summary") or "").strip()
        if not title:
            continue

        mode = raw.get("date_mode") if raw.get("date_mode") in DATE_MODES else "relative"
        anchor = raw.get("anchor_meeting_id")
        if mode == "anchored" and anchor not in known_meetings:
            # Anchored to something that does not exist: keep the item, drop the
            # anchor. An offset from now still puts it somewhere plausible.
            mode, anchor = "relative", None

        draft = {
            "kind": kind,
            "date_mode": mode,
            "anchor_meeting_id": anchor if mode == "anchored" else None,
            "offset_minutes": _int(raw.get("offset_minutes"), default=0),
            "expected_relevant": bool(raw.get("expected_relevant", True)),
            "note": str(raw.get("note") or "")[:200],
        }

        if kind == "emails":
            draft.update(
                subject=title[:300],
                sender=str(raw.get("sender") or "")[:200] or None,
                snippet=str(raw.get("snippet") or "")[:TEXT_MAX] or None,
            )
        else:
            attendees = raw.get("attendees")
            draft.update(
                summary=title[:300],
                description=str(raw.get("description") or "")[:TEXT_MAX] or None,
                location=str(raw.get("location") or "")[:200] or None,
                attendees=[str(a) for a in attendees][:24] if isinstance(attendees, list) else [],
                duration_minutes=_int(raw.get("duration_minutes"), default=60) or 60,
            )
        out.append(draft)

    return out


def _int(value, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
