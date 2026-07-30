"""Thread chat: ask questions about a thread's meetings, calendar events and emails.

The context sent to the model is a digest, not raw material: meeting summaries
(already an LLM-compressed artifact), full calendar/email metadata, and one
on-demand tool -- fetch a specific meeting's transcript -- for the minority of
questions that need verbatim wording. No embedding index: the digest already
fits comfortably for the vast majority of threads, and the one artifact big
enough to threaten that (a transcript) is fetched only when asked for, exactly
like ``summarize.py`` reaches for map-reduce only once a transcript alone
exceeds the budget.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3

from app.config import get_settings
from app.db import get_conn, utcnow
from app.errors import AppError, NotFoundError
from app.logging_config import get_logger
from app.services import llm as llm_svc
from app.services import prompts as prompts_svc
from app.services import summarize as summarize_svc
from app.services import threads as threads_svc
from app.services import transcript as transcript_svc

log = get_logger("chat")

MAX_TOOL_HOPS = 2
MAX_HISTORY_MESSAGES = 20
TOOL_LINE_RE = re.compile(r"^\s*TOOL:\s*get_transcript\s+(\d+)\s*$", re.IGNORECASE)

# How many characters of a hop's answer to withhold before deciding it isn't
# becoming "TOOL: get_transcript <id>" and can be streamed to the client. The
# tool line is always short and on its own line per chat_prompt.md's contract,
# so this stays well clear of it without needing per-character lookahead.
TOOL_SNIFF_LIMIT = 48
KEEPALIVE_SEC = 15.0


def _format_meeting_block(conn: sqlite3.Connection, meeting: sqlite3.Row) -> str:
    meeting_id = meeting["id"]
    title = meeting["title"] or "Untitled meeting"
    when = (meeting["meeting_at"] or "")[:16].replace("T", " ")
    header = f"### Meeting {meeting_id}: {title}" + (f" ({when})" if when else "")

    try:
        summary = summarize_svc.get_current_summary(conn, meeting_id)
    except NotFoundError:
        if meeting["active_diarization_id"] is not None:
            return (
                f"{header}\n"
                "(no summary yet -- transcript only; ask and it can be fetched "
                f"with meeting_id={meeting_id})"
            )
        return f"{header}\n(no summary or transcript yet)"

    lines = [header]
    if summary["tldr"]:
        lines.append(f"TL;DR: {summary['tldr']}")

    if summary["key_decisions"]:
        lines.append("Decisions:")
        for d in summary["key_decisions"]:
            who = d.get("made_by")
            lines.append(f"- {d.get('decision', '')}" + (f" (by {who})" if who else ""))

    if summary["action_items"]:
        lines.append("Action items:")
        for a in summary["action_items"]:
            bits = ", ".join(b for b in (a.get("owner_label"), a.get("due_date") or a.get("due_text")) if b)
            lines.append(f"- {a['text']}" + (f" ({bits})" if bits else "") + f" [{a['status']}]")

    if summary["open_questions"]:
        lines.append("Open questions:")
        lines.extend(f"- {q}" for q in summary["open_questions"])

    names = ", ".join(
        p.get("inferred_name") or p.get("speaker")
        for p in summary["participants"]
        if p.get("inferred_name") or p.get("speaker")
    )
    if names:
        lines.append(f"Participants: {names}")

    lines.append(f"(meeting_id={meeting_id}, ask for its transcript for exact wording)")
    return "\n".join(lines)


def _format_events_and_emails(conn: sqlite3.Connection, thread_id: int) -> str:
    events = conn.execute(
        "SELECT summary, start_at, location, calendar_name, description "
        "FROM thread_calendar_events WHERE thread_id = ? ORDER BY start_at",
        (thread_id,),
    ).fetchall()
    emails = conn.execute(
        "SELECT subject, sender, date, snippet FROM thread_emails "
        "WHERE thread_id = ? ORDER BY date",
        (thread_id,),
    ).fetchall()

    lines: list[str] = []
    if events:
        lines.append("### Calendar events")
        for e in events:
            when = (e["start_at"] or "")[:16].replace("T", " ")
            where = e["location"] or e["calendar_name"] or ""
            bits = ", ".join(b for b in (when, where) if b)
            lines.append(f"- {e['summary'] or 'Untitled'}" + (f" ({bits})" if bits else ""))
            if e["description"]:
                lines.append(f"  {e['description'].strip()}")

    if emails:
        lines.append("### Emails")
        for m in emails:
            meta = ", ".join(b for b in (m["sender"], (m["date"] or "")[:10]) if b)
            lines.append(f"- {m['subject'] or '(no subject)'}" + (f" ({meta})" if meta else ""))
            if m["snippet"]:
                lines.append(f"  {m['snippet'].strip()}")

    if not lines:
        return "(no calendar events or emails attached to this thread)"
    return "\n".join(lines)


def build_thread_digest(conn: sqlite3.Connection, thread_id: int) -> tuple[str, bool]:
    """Compose the context sent to the model. Returns ``(digest, truncated)``."""
    meetings = conn.execute(
        "SELECT * FROM meetings WHERE thread_id = ? ORDER BY meeting_at DESC, id DESC",
        (thread_id,),
    ).fetchall()

    parts = [
        "## Calendar events and emails attached to this thread",
        _format_events_and_emails(conn, thread_id),
        "",
        "## Meetings (most recent first)",
    ]
    budget = get_settings().summary_max_input_tokens
    used = llm_svc.estimate_tokens("\n".join(parts))
    truncated = False
    shown = 0

    for meeting in meetings:
        block = _format_meeting_block(conn, meeting)
        block_tokens = llm_svc.estimate_tokens(block)
        if shown > 0 and used + block_tokens > budget:
            truncated = True
            break
        parts.append(block)
        used += block_tokens
        shown += 1

    if truncated:
        remaining = len(meetings) - shown
        parts.append(
            f"\n({remaining} older meeting(s) in this thread are not shown here "
            "due to the context limit.)"
        )

    return "\n\n".join(parts), truncated


def fetch_meeting_transcript_text(conn: sqlite3.Connection, thread_id: int, meeting_id: int) -> str:
    meeting = threads_svc.get_meeting(conn, meeting_id)
    if meeting is None or meeting["thread_id"] != thread_id:
        raise NotFoundError(f"No meeting {meeting_id} in this thread")
    transcript = transcript_svc.get_transcript(conn, meeting_id)
    return transcript_svc.render(transcript, "text")


def _history_messages(conn: sqlite3.Connection, thread_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content FROM chat_messages WHERE thread_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (thread_id, MAX_HISTORY_MESSAGES),
    ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def _row_to_message(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "thread_id": row["thread_id"],
        "role": row["role"],
        "content": row["content"],
        "created_at": row["created_at"],
    }


def list_messages(conn: sqlite3.Connection, thread_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM chat_messages WHERE thread_id = ? ORDER BY created_at, id",
        (thread_id,),
    ).fetchall()
    return [_row_to_message(r) for r in rows]


def clear_messages(conn: sqlite3.Connection, thread_id: int) -> int:
    """Delete every chat message on a thread. Returns how many were removed."""
    return conn.execute(
        "DELETE FROM chat_messages WHERE thread_id = ?", (thread_id,)
    ).rowcount


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _run_hop(
    config: llm_svc.LLMConfig, payload: dict, queue: asyncio.Queue[str | None]
) -> tuple[str, dict]:
    """Stream one LLM turn, forwarding visible text to `queue` as it arrives --
    except while it might still turn into a `TOOL: get_transcript <id>` line,
    which must never reach the client. Chunks were observed arriving in large
    lumps rather than per character, so a bounded sniff window is enough: keep
    withholding while the buffer is still short and has no newline, then decide
    once and either flush-and-go-live or (if it matched the tool line) stay
    silent for the rest of this hop.
    """
    usage: dict = {}
    buf = ""
    passthrough = False

    async for delta in llm_svc.achat_stream(config, payload, usage_out=usage):
        buf += delta
        if passthrough:
            await queue.put(_sse("token", {"text": delta}))
            continue
        if len(buf) < TOOL_SNIFF_LIMIT and "\n" not in buf:
            continue
        passthrough = True
        await queue.put(_sse("token", {"text": buf}))

    if not passthrough and not TOOL_LINE_RE.match(buf.strip()):
        # A short normal answer never tripped the sniff window -- flush it now.
        await queue.put(_sse("token", {"text": buf}))

    return buf, usage


async def _produce(
    queue: asyncio.Queue[str | None],
    db_path,
    thread_id: int,
    user_id: int,
    message: str,
    model: str | None,
) -> None:
    """Runs the digest + tool-hop loop + persistence, independent of whether
    anyone is still listening on `queue` -- a disconnected client must not stop
    the answer from being generated and saved, matching the old behavior where
    this ran synchronously inside the request regardless of the client.
    """
    try:
        with get_conn(db_path) as conn:
            threads_svc.require_thread(conn, thread_id)
            digest, _truncated = build_thread_digest(conn, thread_id)
            history = _history_messages(conn, thread_id)
            config = llm_svc.LLMConfig.from_db(conn, model_override=model)

        prompt = prompts_svc.load("chat_prompt")
        system, _ = prompt.render({"thread_digest": digest})
        if prompt.temperature is not None:
            config.temperature = prompt.temperature

        messages: list[dict] = [
            {"role": "system", "content": system},
            *history,
            {"role": "user", "content": message},
        ]

        content = ""
        usage: dict = {}
        for _ in range(MAX_TOOL_HOPS + 1):
            payload = {
                "model": config.model,
                "messages": messages,
                "temperature": config.temperature,
                "stream": True,
                "include_reasoning": False,
            }
            content, usage = await _run_hop(config, payload, queue)
            match = TOOL_LINE_RE.match(content.strip())
            if not match:
                break

            meeting_id = int(match.group(1))
            messages.append({"role": "assistant", "content": content})
            with get_conn(db_path) as conn:
                try:
                    transcript_text = fetch_meeting_transcript_text(conn, thread_id, meeting_id)
                    tool_result = f"[Transcript for meeting {meeting_id}]\n{transcript_text}"
                except NotFoundError as exc:
                    tool_result = f"[No transcript available for meeting {meeting_id}: {exc.message}]"
            messages.append({"role": "user", "content": tool_result})
        else:
            # Ran out of hops still asking for a transcript -- never show the raw
            # "TOOL: ..." line to the user. Nothing was streamed for that last
            # hop (it matched the tool pattern), so send the fallback now.
            if TOOL_LINE_RE.match(content.strip()):
                content = (
                    "I wasn't able to find a direct answer within the context available. "
                    "Try asking about a specific meeting by name or date."
                )
                await queue.put(_sse("token", {"text": content}))

        now = utcnow()
        with get_conn(db_path) as conn:
            conn.execute(
                "INSERT INTO chat_messages (thread_id, owner_id, role, content, created_at) "
                "VALUES (?, ?, 'user', ?, ?)",
                (thread_id, user_id, message, now),
            )
            cur = conn.execute(
                "INSERT INTO chat_messages (thread_id, owner_id, role, content, model, "
                "prompt_tokens, completion_tokens, created_at) "
                "VALUES (?, ?, 'assistant', ?, ?, ?, ?, ?)",
                (
                    thread_id, user_id, content, config.model,
                    usage.get("prompt_tokens"), usage.get("completion_tokens"), utcnow(),
                ),
            )
            assistant_row = conn.execute(
                "SELECT * FROM chat_messages WHERE id = ?", (cur.lastrowid,)
            ).fetchone()

        log.info("chat reply for thread %s (%s)", thread_id, config.model)
        await queue.put(_sse("done", _row_to_message(assistant_row)))
    except AppError as exc:
        # Same "a failed send is never saved" behavior as before -- nothing is
        # persisted, the client just learns why.
        await queue.put(_sse("error", {"code": exc.code, "message": exc.message}))
    finally:
        await queue.put(None)


async def stream_chat_response(
    db_path, thread_id: int, user_id: int, message: str, *, model: str | None = None,
):
    """SSE generator for StreamingResponse.

    The actual work runs in a background task feeding `queue`, decoupled from
    this generator's own lifecycle: if the client disconnects, Starlette just
    stops iterating us, but the task keeps running and still persists the
    answer -- unlike jobs.py's poll loop, there's no cost to keep going, and
    stopping early would silently drop a reply that already cost LLM budget.
    """
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    asyncio.create_task(_produce(queue, db_path, thread_id, user_id, message, model))

    while True:
        try:
            item = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SEC)
        except asyncio.TimeoutError:
            # The reasoning phase alone can run 80s+ with nothing to send --
            # long enough for an intermediate proxy to give up on a silent
            # connection, same reasoning as jobs.py's idle keepalive.
            yield ": keepalive\n\n"
            continue
        if item is None:
            return
        yield item
