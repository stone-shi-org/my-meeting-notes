"""Home chat: ask questions across every thread on the home screen.

Same shape as thread chat (``services/chat.py``) at every layer -- SSE
plumbing, the ``TOOL: <verb> <arg>`` text protocol and its sniff window,
prompt substitution -- except the digest is one line per thread rather than
one thread's own meetings, and each tool takes an explicit thread_id where
thread chat's tools get one implicitly from the URL. Read-only: there is no
attach tool here, because there is no single unambiguous thread to write a
found item onto.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3

from app.config import effective, get_settings
from app.db import get_conn, utcnow
from app.errors import AppError, NoIntegrationsError, NotFoundError
from app.logging_config import get_logger
from app.services import chat as chat_svc
from app.services import chat_followups as chat_followups_svc
from app.services import llm as llm_svc
from app.services import matching as matching_svc
from app.services import prompts as prompts_svc
from app.services import threads as threads_svc
from app.services import upcoming as upcoming_svc

log = get_logger("home_chat")

MAX_TOOL_HOPS = 3
MAX_HISTORY_MESSAGES = 20
SEARCH_MAX_CANDIDATES = 10

TOOL_RE = re.compile(
    r"^\s*TOOL:\s*(get_thread_detail|get_transcript|get_upcoming|search_context)"
    r"\s+(.+?)\s*$",
    re.IGNORECASE,
)

KEEPALIVE_SEC = 15.0


def _format_thread_block(row: sqlite3.Row) -> str:
    title = row["title"] or "Untitled thread"
    header = f"### Thread {row['id']}: {title}" + (" (Archived)" if row["archived"] else "")
    lines = [header, f"Group: {row['group_name'] or 'Ungrouped'}"]

    if row["description"]:
        lines.append(row["description"].strip())

    lines.append(
        f"{row['meeting_count']} meeting(s), {row['event_count']} calendar event(s), "
        f"{row['email_count']} email(s), {row['note_count']} note(s)"
    )

    when = (row["updated_at"] or "")[:16].replace("T", " ")
    if when:
        lines.append(f"Last updated: {when}")

    next_step = row["next_step"]
    lines.append(f"Next step: {next_step}" if next_step else "Next step: (not generated yet)")
    lines.append(f"(thread_id={row['id']}, ask for its details or transcript for more)")
    return "\n".join(lines)


def build_home_digest(conn: sqlite3.Connection, user_id: int) -> tuple[str, bool]:
    """Compose the context sent to the model. Returns ``(digest, truncated)``."""
    rows = conn.execute(
        f"""
        SELECT t.*, g.name AS group_name, {threads_svc.THREAD_COUNTS_SQL}
          FROM threads t
          LEFT JOIN thread_groups g ON g.id = t.group_id
         WHERE t.owner_id = ?
         ORDER BY t.archived ASC, t.updated_at DESC, t.id DESC
        """,
        (user_id,),
    ).fetchall()

    active = sum(1 for r in rows if not r["archived"])
    header = f"## Threads ({active} active, {len(rows) - active} archived)"
    parts = [header]

    budget = get_settings().summary_max_input_tokens
    used = llm_svc.estimate_tokens(header)
    truncated = False
    shown = 0

    for row in rows:
        block = _format_thread_block(row)
        block_tokens = llm_svc.estimate_tokens(block)
        if shown > 0 and used + block_tokens > budget:
            truncated = True
            break
        parts.append(block)
        used += block_tokens
        shown += 1

    if truncated:
        remaining = len(rows) - shown
        parts.append(
            f"\n({remaining} more thread(s) not shown here due to the context limit -- "
            "ask about one by name if it is not listed.)"
        )
    if not rows:
        parts.append("(no threads yet)")

    return "\n\n".join(parts), truncated


def _require_owned_thread(conn: sqlite3.Connection, thread_id: int, user_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM threads WHERE id = ? AND owner_id = ?", (thread_id, user_id)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"No thread {thread_id}")
    return row


async def _run_tool(conn: sqlite3.Connection, db_path, user_id: int, verb: str, arg: str) -> str:
    """Dispatch one `TOOL: <verb> <arg>` line to its implementation.

    Every branch catches its own AppError and turns it into a bracketed tool
    result string instead of raising, so a transient provider hiccup mid-chat
    becomes something the model can explain rather than a failed request.
    """
    if verb == "get_thread_detail":
        return _tool_get_thread_detail(conn, user_id, arg)
    if verb == "get_transcript":
        return _tool_get_transcript(conn, user_id, arg)
    if verb == "get_upcoming":
        return await _tool_get_upcoming(db_path, user_id, arg)
    return await _tool_search_context(conn, db_path, user_id, arg)


def _tool_get_thread_detail(conn: sqlite3.Connection, user_id: int, arg: str) -> str:
    try:
        thread_id = int(arg.split()[0])
    except (ValueError, IndexError):
        return "[Invalid thread id]"
    try:
        row = _require_owned_thread(conn, thread_id, user_id)
    except NotFoundError:
        return f"[No thread {thread_id}]"
    digest, _truncated = chat_svc.build_thread_digest(conn, thread_id)
    title = row["title"] or "Untitled thread"
    return f"[Thread {thread_id}: {title}]\n{digest}"


def _tool_get_transcript(conn: sqlite3.Connection, user_id: int, arg: str) -> str:
    parts = arg.split()
    if len(parts) < 2:
        return "[get_transcript needs a thread id and a meeting id]"
    try:
        thread_id, meeting_id = int(parts[0]), int(parts[1])
    except ValueError:
        return "[Invalid thread id or meeting id]"
    try:
        _require_owned_thread(conn, thread_id, user_id)
    except NotFoundError:
        return f"[No thread {thread_id}]"
    try:
        transcript_text = chat_svc.fetch_meeting_transcript_text(conn, thread_id, meeting_id)
        return f"[Transcript for meeting {meeting_id}]\n{transcript_text}"
    except NotFoundError as exc:
        return f"[No transcript available for meeting {meeting_id}: {exc.message}]"


async def _tool_get_upcoming(db_path, user_id: int, arg: str) -> str:
    try:
        days = int(arg.strip()) if arg.strip() else upcoming_svc.DEFAULT_DAYS
    except ValueError:
        days = upcoming_svc.DEFAULT_DAYS

    result = await upcoming_svc.collect(lambda: get_conn(db_path), user_id=user_id, days=days)
    if result["connected"] == 0:
        return "[No calendars connected]"
    if not result["events"]:
        return f"[No upcoming events in the next {days} day(s)]"

    lines = [f"Upcoming events (next {days} day(s)):"]
    for e in result["events"]:
        when = (e.get("start") or "")[:16].replace("T", " ")
        where = e.get("location") or e.get("calendar_name") or ""
        bits = ", ".join(b for b in (when, where) if b)
        attached = e.get("attached")
        tag = f" [already on thread {attached['thread_id']}]" if attached else ""
        lines.append(f"- {e.get('summary') or 'Untitled'}" + (f" ({bits})" if bits else "") + tag)
    if result.get("error"):
        lines.append(f"(calendar search failed: {result['error']})")
    return "\n".join(lines)


def _format_search_results(gathered: dict) -> str:
    events = gathered["events"]
    emails = gathered["emails"]
    lines: list[str] = []

    if events:
        lines.append("Calendar events found (not attached to any thread):")
        for e in events:
            when = (e.get("start") or "")[:16].replace("T", " ")
            where = e.get("location") or e.get("calendar_name") or ""
            bits = ", ".join(b for b in (when, where) if b)
            lines.append(f"- {e.get('summary') or 'Untitled'}" + (f" ({bits})" if bits else ""))
            if e.get("description"):
                lines.append(f"  {e['description'].strip()}")

    if emails:
        lines.append("Emails found (not attached to any thread):")
        for m in emails:
            meta = ", ".join(b for b in (m.get("sender"), (m.get("date") or "")[:10]) if b)
            lines.append(f"- {m.get('subject') or '(no subject)'}" + (f" ({meta})" if meta else ""))
            if m.get("snippet"):
                lines.append(f"  {m['snippet'].strip()}")

    if gathered.get("calendar_error"):
        lines.append(f"(calendar search failed: {gathered['calendar_error']})")
    if gathered.get("email_error"):
        lines.append(f"(email search failed: {gathered['email_error']})")

    if not lines:
        return "[No matching calendar events or emails found]"
    return "\n".join(lines)


async def _tool_search_context(conn: sqlite3.Connection, db_path, user_id: int, arg: str) -> str:
    keywords = [w for w in arg.split() if w][:8]
    if not keywords:
        return "[search_context needs at least one keyword]"

    days_before = effective(conn, "match_window_days_before")
    days_after = effective(conn, "match_window_days_after")
    cal_days_before = effective(conn, "match_window_calendar_days_before")
    cal_days_after = effective(conn, "match_window_calendar_days_after")
    start, end = matching_svc.date_window(None, days_before, days_after)
    cal_start, cal_end = matching_svc.date_window(None, cal_days_before, cal_days_after)

    try:
        # No single thread to dedupe against -- 0 is never a real thread id, so
        # nothing comes back marked "already attached," which is the right
        # answer for a search spanning every thread at once.
        gathered = await matching_svc.gather_candidates(
            lambda: get_conn(db_path),
            thread_id=0,
            keywords=keywords,
            start=start,
            end=end,
            calendar_start=cal_start,
            calendar_end=cal_end,
            max_candidates=SEARCH_MAX_CANDIDATES,
            user_id=user_id,
        )
    except NoIntegrationsError as exc:
        return f"[{exc.message}]"

    return _format_search_results(gathered)


def _history_messages(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content FROM home_chat_messages WHERE owner_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (user_id, MAX_HISTORY_MESSAGES),
    ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def _row_to_message(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "role": row["role"],
        "content": row["content"],
        "model": row["model"],
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "created_at": row["created_at"],
    }


def list_messages(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM home_chat_messages WHERE owner_id = ? ORDER BY created_at, id",
        (user_id,),
    ).fetchall()
    return [_row_to_message(r) for r in rows]


def clear_messages(conn: sqlite3.Connection, user_id: int) -> int:
    """Delete every home chat message for this user. Returns how many were removed."""
    return conn.execute(
        "DELETE FROM home_chat_messages WHERE owner_id = ?", (user_id,)
    ).rowcount


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _run_hop(
    config: llm_svc.LLMConfig, payload: dict, queue: asyncio.Queue[str | None]
) -> tuple[str, dict]:
    """Stream one LLM turn, forwarding visible text to `queue` as it arrives --
    except while it might still turn into a `TOOL: <verb> <arg>` line, which
    must never reach the client. Once the output starts with ``TOOL:`` it stays
    withheld until the complete line can be validated; tool arguments are not
    length-bounded and therefore cannot safely use a fixed sniff window.
    """
    usage: dict = {}
    buf = ""
    passthrough = False

    async for delta in llm_svc.achat_stream(config, payload, usage_out=usage):
        buf += delta
        if passthrough:
            await queue.put(_sse("token", {"text": delta}))
            continue

        candidate = buf.lstrip()
        upper = candidate.upper()
        if not candidate or "TOOL:".startswith(upper) or upper.startswith("TOOL:"):
            continue
        passthrough = True
        await queue.put(_sse("token", {"text": buf}))

    if not passthrough and not TOOL_RE.match(buf.strip()):
        # A short normal answer never tripped the sniff window -- flush it now.
        await queue.put(_sse("token", {"text": buf}))

    return buf, usage


async def _produce(
    queue: asyncio.Queue[str | None],
    db_path,
    user_id: int,
    message: str,
    model: str | None,
) -> None:
    """Runs the digest + tool-hop loop + persistence, independent of whether
    anyone is still listening on `queue` -- a disconnected client must not stop
    the answer from being generated and saved.
    """
    try:
        with get_conn(db_path) as conn:
            digest, _truncated = build_home_digest(conn, user_id)
            history = _history_messages(conn, user_id)
            config = llm_svc.LLMConfig.from_db(conn, model_override=model)

        prompt = prompts_svc.load("home_chat_prompt")
        system, _ = prompt.render({"home_digest": digest})
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
            match = TOOL_RE.match(content.strip())
            if not match:
                break

            verb, arg = match.group(1).lower(), match.group(2).strip()
            log.info("tool hop for user %s: %s %s", user_id, verb, arg)
            await queue.put(_sse("tool_call", {"tool": verb, "arg": arg}))
            messages.append({"role": "assistant", "content": content})
            with get_conn(db_path) as conn:
                tool_result = await _run_tool(conn, db_path, user_id, verb, arg)
            log.info(
                "tool hop for user %s: %s %s -> %d char(s)",
                user_id, verb, arg, len(tool_result),
            )
            await queue.put(_sse("tool_result", {"tool": verb, "arg": arg, "result": tool_result}))
            messages.append({"role": "user", "content": tool_result})
        else:
            # Ran out of hops still asking for a tool -- never show the raw
            # "TOOL: ..." line to the user.
            if TOOL_RE.match(content.strip()):
                content = (
                    "I wasn't able to find a direct answer within the context available. "
                    "Try asking about a specific thread by name."
                )
                await queue.put(_sse("token", {"text": content}))

        now = utcnow()
        with get_conn(db_path) as conn:
            conn.execute(
                "INSERT INTO home_chat_messages (owner_id, role, content, created_at) "
                "VALUES (?, 'user', ?, ?)",
                (user_id, message, now),
            )
            cur = conn.execute(
                "INSERT INTO home_chat_messages (owner_id, role, content, model, "
                "prompt_tokens, completion_tokens, created_at) "
                "VALUES (?, 'assistant', ?, ?, ?, ?, ?)",
                (
                    user_id, content, config.model,
                    usage.get("prompt_tokens"), usage.get("completion_tokens"), utcnow(),
                ),
            )
            assistant_row = conn.execute(
                "SELECT * FROM home_chat_messages WHERE id = ?", (cur.lastrowid,)
            ).fetchone()

        log.info("home chat reply for user %s (%s)", user_id, config.model)
        await queue.put(_sse("done", _row_to_message(assistant_row)))

        suggestions = await asyncio.to_thread(
            chat_followups_svc.generate_sync,
            db_path, question=message, answer=content, model=config.model,
        )
        if suggestions:
            await queue.put(_sse("suggestions", {"suggestions": suggestions}))
    except AppError as exc:
        # Same "a failed send is never saved" behavior as thread chat -- nothing
        # is persisted, the client just learns why.
        await queue.put(_sse("error", {"code": exc.code, "message": exc.message}))
    finally:
        await queue.put(None)


async def stream_chat_response(db_path, user_id: int, message: str, *, model: str | None = None):
    """SSE generator for StreamingResponse.

    The actual work runs in a background task feeding `queue`, decoupled from
    this generator's own lifecycle -- same reasoning as thread chat's version.
    """
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    asyncio.create_task(_produce(queue, db_path, user_id, message, model))

    while True:
        try:
            item = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SEC)
        except asyncio.TimeoutError:
            yield ": keepalive\n\n"
            continue
        if item is None:
            return
        yield item
