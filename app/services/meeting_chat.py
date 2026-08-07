"""Meeting chat: ask questions about a single meeting's transcript.

Unlike ``chat.py`` (a thread's meetings, calendar events and emails, digested
because the whole thread rarely fits), the context here is one meeting's own
transcript -- already the thing being asked about, so it is sent close to
verbatim rather than compressed through a summary. There is no on-demand
tool: a single transcript either fits the budget or gets truncated in place,
with a note saying so.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

from app.config import get_settings
from app.db import get_conn, utcnow
from app.errors import AppError
from app.logging_config import get_logger
from app.services import chat_followups as chat_followups_svc
from app.services import llm as llm_svc
from app.services import prompts as prompts_svc
from app.services import threads as threads_svc
from app.services import transcript as transcript_svc

log = get_logger("meeting_chat")

MAX_HISTORY_MESSAGES = 20
KEEPALIVE_SEC = 15.0


def build_meeting_digest(conn: sqlite3.Connection, meeting_id: int) -> tuple[str, bool]:
    """Compose the context sent to the model. Returns ``(digest, truncated)``."""
    meeting = threads_svc.require_meeting(conn, meeting_id)
    title = meeting["title"] or "Untitled meeting"
    when = (meeting["meeting_at"] or "")[:16].replace("T", " ")
    header = f"# {title}" + (f" ({when})" if when else "")

    transcript = transcript_svc.get_transcript(conn, meeting_id)
    budget = get_settings().summary_max_input_tokens
    used = llm_svc.estimate_tokens(header)
    truncated = False
    lines: list[str] = []

    for seg in transcript["segments"]:
        name = transcript_svc.label_with_me(seg["speaker_name"], seg["is_me"])
        line = f"[{transcript_svc.fmt_clock(seg['start'] or 0)}] {name}: {seg['text'].strip()}"
        line_tokens = llm_svc.estimate_tokens(line)
        if lines and used + line_tokens > budget:
            truncated = True
            break
        lines.append(line)
        used += line_tokens

    if truncated:
        remaining = len(transcript["segments"]) - len(lines)
        lines.append(
            f"\n(transcript truncated -- {remaining} more line(s) not shown due to the "
            "context limit)"
        )

    return header + "\n\n" + "\n".join(lines), truncated


def _history_messages(conn: sqlite3.Connection, meeting_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content FROM meeting_chat_messages WHERE meeting_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (meeting_id, MAX_HISTORY_MESSAGES),
    ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def _row_to_message(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "meeting_id": row["meeting_id"],
        "role": row["role"],
        "content": row["content"],
        "model": row["model"],
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "created_at": row["created_at"],
    }


def list_messages(conn: sqlite3.Connection, meeting_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM meeting_chat_messages WHERE meeting_id = ? ORDER BY created_at, id",
        (meeting_id,),
    ).fetchall()
    return [_row_to_message(r) for r in rows]


def clear_messages(conn: sqlite3.Connection, meeting_id: int) -> int:
    """Delete every chat message on a meeting. Returns how many were removed."""
    return conn.execute(
        "DELETE FROM meeting_chat_messages WHERE meeting_id = ?", (meeting_id,)
    ).rowcount


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _produce(
    queue: asyncio.Queue[str | None],
    db_path,
    meeting_id: int,
    user_id: int,
    message: str,
    model: str | None,
) -> None:
    """Runs the digest + LLM call + persistence, independent of whether anyone
    is still listening on `queue` -- see chat.py's `_produce` for why.
    """
    try:
        with get_conn(db_path) as conn:
            digest, _truncated = build_meeting_digest(conn, meeting_id)
            history = _history_messages(conn, meeting_id)
            config = llm_svc.LLMConfig.from_db(conn, model_override=model)

        prompt = prompts_svc.load("meeting_chat_prompt")
        system, _ = prompt.render({"meeting_digest": digest})
        if prompt.temperature is not None:
            config.temperature = prompt.temperature

        messages: list[dict] = [
            {"role": "system", "content": system},
            *history,
            {"role": "user", "content": message},
        ]
        payload = {
            "model": config.model,
            "messages": messages,
            "temperature": config.temperature,
            "stream": True,
            "include_reasoning": False,
        }

        content = ""
        usage: dict = {}
        async for delta in llm_svc.achat_stream(config, payload, usage_out=usage):
            content += delta
            await queue.put(_sse("token", {"text": delta}))

        now = utcnow()
        with get_conn(db_path) as conn:
            conn.execute(
                "INSERT INTO meeting_chat_messages (meeting_id, owner_id, role, content, created_at) "
                "VALUES (?, ?, 'user', ?, ?)",
                (meeting_id, user_id, message, now),
            )
            cur = conn.execute(
                "INSERT INTO meeting_chat_messages (meeting_id, owner_id, role, content, model, "
                "prompt_tokens, completion_tokens, created_at) "
                "VALUES (?, ?, 'assistant', ?, ?, ?, ?, ?)",
                (
                    meeting_id, user_id, content, config.model,
                    usage.get("prompt_tokens"), usage.get("completion_tokens"), utcnow(),
                ),
            )
            assistant_row = conn.execute(
                "SELECT * FROM meeting_chat_messages WHERE id = ?", (cur.lastrowid,)
            ).fetchone()

        log.info("meeting chat reply for meeting %s (%s)", meeting_id, config.model)
        await queue.put(_sse("done", _row_to_message(assistant_row)))

        suggestions = await asyncio.to_thread(
            chat_followups_svc.generate_sync,
            db_path, question=message, answer=content, model=config.model,
        )
        if suggestions:
            await queue.put(_sse("suggestions", {"suggestions": suggestions}))
    except AppError as exc:
        # A failed send is never saved -- the client just learns why.
        await queue.put(_sse("error", {"code": exc.code, "message": exc.message}))
    finally:
        await queue.put(None)


async def stream_chat_response(
    db_path, meeting_id: int, user_id: int, message: str, *, model: str | None = None,
):
    """SSE generator for StreamingResponse. See chat.py's twin for the
    background-task/keepalive rationale, which applies identically here.
    """
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    asyncio.create_task(_produce(queue, db_path, meeting_id, user_id, message, model))

    while True:
        try:
            item = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SEC)
        except asyncio.TimeoutError:
            yield ": keepalive\n\n"
            continue
        if item is None:
            return
        yield item
