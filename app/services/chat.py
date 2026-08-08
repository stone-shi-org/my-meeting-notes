"""Thread chat: ask questions about a thread's meetings, calendar events, emails and notes.

The context sent to the model is a digest, not raw material: meeting summaries
(already an LLM-compressed artifact), full calendar/email metadata, whatever
notes have been written on the thread, and one on-demand tool -- fetch a specific meeting's transcript -- for the minority of
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

from app.config import effective, get_settings
from app.db import get_conn, utcnow
from app.errors import AppError, NoIntegrationsError, NotFoundError
from app.logging_config import get_logger
from app.services import chat_followups as chat_followups_svc
from app.services import llm as llm_svc
from app.services import matching as matching_svc
from app.services import prompts as prompts_svc
from app.services import summarize as summarize_svc
from app.services import threads as threads_svc
from app.services import transcript as transcript_svc
from app.services.providers import loader as providers_svc

log = get_logger("chat")

# One hop more than a transcript-only answer ever needed: search_context ->
# get_email (verify wording) -> attach_email/attach_event -> final prose.
MAX_TOOL_HOPS = 3
MAX_HISTORY_MESSAGES = 20

# Notes are included whole, unlike the one-line snippets events and emails get:
# a note is short by construction (one chat reply, or something typed), and
# half of one is worse than none. The cap is a backstop against a pasted
# document, not a routine truncation.
NOTE_BODY_LIMIT = 4000
# Same backstop, for a fetched email body: bounds a message that turns out to
# be a forwarded thread or an attachment dump rather than routine truncation.
EMAIL_BODY_LIMIT = 8000
# How many new candidates search_context surfaces per call -- kept well below
# match_max_candidates, since these are read back into a chat reply rather
# than rendered as a picker.
SEARCH_MAX_CANDIDATES = 10

TOOL_RE = re.compile(
    r"^\s*TOOL:\s*(get_transcript|search_context|get_email|attach_email|attach_event)"
    r"\s+(.+?)\s*$",
    re.IGNORECASE,
)

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

    speaker_map = transcript_svc.load_speaker_map(conn, meeting_id)
    me_id = transcript_svc.me_speaker_id(conn, meeting_id)
    parts = []
    for p in summary["participants"]:
        label = p.get("inferred_name") or p.get("speaker")
        if not label:
            continue
        raw_speaker = p.get("speaker")
        if me_id and raw_speaker and transcript_svc.canonical_speaker_id(raw_speaker, speaker_map) == me_id:
            label = transcript_svc.label_with_me(label, True)
        parts.append(label)
    names = ", ".join(parts)
    if names:
        lines.append(f"Participants: {names}")

    lines.append(f"(meeting_id={meeting_id}, ask for its transcript for exact wording)")
    return "\n".join(lines)


def _format_attachments(conn: sqlite3.Connection, thread_id: int) -> str:
    events = conn.execute(
        "SELECT summary, start_at, location, calendar_name, description "
        "FROM thread_calendar_events WHERE thread_id = ? ORDER BY start_at",
        (thread_id,),
    ).fetchall()
    emails = conn.execute(
        "SELECT message_id, subject, sender, date, snippet FROM thread_emails "
        "WHERE thread_id = ? ORDER BY date",
        (thread_id,),
    ).fetchall()
    notes = conn.execute(
        "SELECT title, body, source, created_at FROM thread_notes "
        "WHERE thread_id = ? ORDER BY created_at",
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
            lines.append(
                f"- {m['subject'] or '(no subject)'}" + (f" ({meta})" if meta else "")
                + f" [email_id: {m['message_id']}, ask for its full body if needed]"
            )
            if m["snippet"]:
                lines.append(f"  {m['snippet'].strip()}")

    if notes:
        lines.append("### Notes")
        for n in notes:
            when = (n["created_at"] or "")[:10]
            # Whose words these are matters more here than for the other two:
            # a note saved out of a chat reply is this assistant's own earlier
            # output, and treating it as evidence would be circular.
            origin = "saved from an AI answer" if n["source"] == "ai_chat" else "written by the user"
            bits = ", ".join(b for b in (when, origin) if b)
            lines.append(f"- {n['title'] or 'Untitled note'}" + (f" ({bits})" if bits else ""))
            body = (n["body"] or "").strip()
            if body:
                shown = body[:NOTE_BODY_LIMIT]
                if len(body) > NOTE_BODY_LIMIT:
                    shown += "\n(note truncated)"
                lines.append(f"  {_indent(shown)}")

    if not lines:
        return "(no calendar events, emails or notes attached to this thread)"
    return "\n".join(lines)


def _indent(text: str) -> str:
    """Keep a multi-line note body inside its bullet.

    Events and emails contribute one-line snippets; a note is markdown someone
    wrote, and its own headings and bullets would otherwise read as part of the
    digest's structure rather than as content nested under the note.
    """
    return text.replace("\n", "\n  ")


def build_thread_digest(conn: sqlite3.Connection, thread_id: int) -> tuple[str, bool]:
    """Compose the context sent to the model. Returns ``(digest, truncated)``."""
    meetings = conn.execute(
        "SELECT * FROM meetings WHERE thread_id = ? ORDER BY meeting_at DESC, id DESC",
        (thread_id,),
    ).fetchall()

    parts = [
        "## Calendar events, emails and notes attached to this thread",
        _format_attachments(conn, thread_id),
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


async def _run_tool(
    conn: sqlite3.Connection,
    db_path,
    thread_id: int,
    user_id: int,
    verb: str,
    arg: str,
    found: dict[str, dict[str, dict]],
) -> str:
    """Dispatch one `TOOL: <verb> <arg>` line to its implementation.

    Every branch catches its own AppError and turns it into a bracketed tool
    result string instead of raising, so a transient provider hiccup mid-chat
    becomes something the model can explain rather than a failed request.
    """
    if verb == "get_transcript":
        return _tool_get_transcript(conn, thread_id, arg)
    if verb == "search_context":
        return await _tool_search_context(conn, db_path, thread_id, user_id, arg, found)
    if verb == "get_email":
        return await _tool_get_email(conn, thread_id, user_id, arg, found)
    return _tool_attach(conn, thread_id, user_id, verb, arg, found)


def _tool_get_transcript(conn: sqlite3.Connection, thread_id: int, arg: str) -> str:
    try:
        meeting_id = int(arg)
    except ValueError:
        return "[Invalid meeting id]"
    try:
        transcript_text = fetch_meeting_transcript_text(conn, thread_id, meeting_id)
        return f"[Transcript for meeting {meeting_id}]\n{transcript_text}"
    except NotFoundError as exc:
        return f"[No transcript available for meeting {meeting_id}: {exc.message}]"


def _format_search_results(gathered: dict) -> str:
    events = gathered["events"]
    emails = gathered["emails"]
    lines: list[str] = []

    if events:
        lines.append("Calendar events found (not yet attached to this thread):")
        for e in events:
            when = (e.get("start") or "")[:16].replace("T", " ")
            where = e.get("location") or e.get("calendar_name") or ""
            bits = ", ".join(b for b in (when, where) if b)
            lines.append(
                f"- {e.get('summary') or 'Untitled'}" + (f" ({bits})" if bits else "")
                + f" [event_id: {e['uid']}]"
            )
            if e.get("description"):
                lines.append(f"  {e['description'].strip()}")

    if emails:
        lines.append("Emails found (not yet attached to this thread):")
        for m in emails:
            meta = ", ".join(b for b in (m.get("sender"), (m.get("date") or "")[:10]) if b)
            lines.append(
                f"- {m.get('subject') or '(no subject)'}" + (f" ({meta})" if meta else "")
                + f" [email_id: {m['message_id']}]"
            )
            if m.get("snippet"):
                lines.append(f"  {m['snippet'].strip()}")

    if gathered.get("calendar_error"):
        lines.append(f"(calendar search failed: {gathered['calendar_error']})")
    if gathered.get("email_error"):
        lines.append(f"(email search failed: {gathered['email_error']})")

    if not lines:
        return "[No new calendar events or emails found for that search]"
    return "\n".join(lines)


async def _tool_search_context(
    conn: sqlite3.Connection,
    db_path,
    thread_id: int,
    user_id: int,
    arg: str,
    found: dict[str, dict[str, dict]],
) -> str:
    keywords = [w for w in arg.split() if w][:8]
    if not keywords:
        return "[search_context needs at least one keyword]"

    days_before = effective(conn, "match_window_days_before")
    days_after = effective(conn, "match_window_days_after")
    cal_days_before = effective(conn, "match_window_calendar_days_before")
    cal_days_after = effective(conn, "match_window_calendar_days_after")
    # Anchored on now, not a meeting -- this is an ad-hoc chat-triggered search,
    # not the meeting-anchored search run_match does.
    start, end = matching_svc.date_window(None, days_before, days_after)
    cal_start, cal_end = matching_svc.date_window(None, cal_days_before, cal_days_after)

    try:
        gathered = await matching_svc.gather_candidates(
            lambda: get_conn(db_path),
            thread_id=thread_id,
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

    for event in gathered["events"]:
        if event.get("uid"):
            found["events"][event["uid"]] = event
    for email in gathered["emails"]:
        if email.get("message_id"):
            found["emails"][email["message_id"]] = email

    return _format_search_results(gathered)


def _resolve_email_ref(
    conn: sqlite3.Connection, thread_id: int, message_id: str, found: dict[str, dict[str, dict]]
) -> dict | None:
    """Where to fetch a full body from, for an id from search_context or the digest.

    Only ever resolves an id that was either just surfaced by this turn's own
    search, or already attached to this thread -- never an arbitrary id, which
    is what keeps get_email from being a way to probe other integrations.
    """
    cached = found["emails"].get(message_id)
    if cached is not None:
        return {
            "integration_id": cached.get("integration_id"),
            "native_id": cached.get("id"),
            "folder_id": cached.get("folder_id"),
        }

    row = conn.execute(
        "SELECT mcp_id, folder_id FROM thread_emails WHERE thread_id = ? AND message_id = ?",
        (thread_id, message_id),
    ).fetchone()
    if row is None:
        return None

    # thread_emails has no integration_id column of its own; message_id's own
    # composite shape (`{provider}:{integration_id}:{...}`) is the only place
    # it's recorded once attached.
    parts = message_id.split(":", 2)
    integration_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    return {
        "integration_id": integration_id,
        "native_id": row["mcp_id"],
        "folder_id": row["folder_id"],
    }


async def _tool_get_email(
    conn: sqlite3.Connection,
    thread_id: int,
    user_id: int,
    message_id: str,
    found: dict[str, dict[str, dict]],
) -> str:
    ref = _resolve_email_ref(conn, thread_id, message_id, found)
    if ref is None or ref["integration_id"] is None or not ref["native_id"]:
        return (
            "[No such email in this thread's context. Run search_context first, "
            "or use an email_id already shown in THREAD CONTEXT.]"
        )

    # Owner-scoped: someone else's integration id is "not found," same as any
    # other object route in this app.
    row = conn.execute(
        "SELECT * FROM integrations WHERE id = ? AND user_id = ?",
        (ref["integration_id"], user_id),
    ).fetchone()
    provider = providers_svc.build_provider(conn, row) if row is not None else None
    try:
        body = (
            await provider.get_email_body(native_id=ref["native_id"], folder_id=ref["folder_id"])
            if provider is not None
            else None
        )
    except AppError as exc:
        return f"[Could not fetch that email's body: {exc.message}]"
    if body is None:
        return "[Full body is not available for that email's account. Use the snippet already shown.]"

    if len(body) > EMAIL_BODY_LIMIT:
        body = body[:EMAIL_BODY_LIMIT] + "\n(email truncated)"
    return f"[Email {message_id}]\n{body}"


def _tool_attach(
    conn: sqlite3.Connection,
    thread_id: int,
    user_id: int,
    verb: str,
    arg: str,
    found: dict[str, dict[str, dict]],
) -> str:
    if verb == "attach_email":
        item = found["emails"].get(arg)
        if item is None:
            return "[No email with that id found this turn -- run search_context first.]"
        matching_svc.attach_email(
            conn, thread_id=thread_id, meeting_id=None, email=item, user_id=user_id, auto=False
        )
        label = item.get("subject") or "(no subject)"
    elif verb == "attach_event":
        item = found["events"].get(arg)
        if item is None:
            return "[No event with that id found this turn -- run search_context first.]"
        matching_svc.attach_event(
            conn, thread_id=thread_id, meeting_id=None, event=item, user_id=user_id, auto=False
        )
        label = item.get("summary") or "Untitled"
    else:
        return "[Unknown tool]"

    threads_svc.touch_thread(conn, thread_id)
    return f"[Attached: {label}]"


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
        "model": row["model"],
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
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

        # Populated by search_context, read by get_email/attach_email/attach_event --
        # scoped to this one request and never persisted. This is also the safety
        # boundary: attach_* only ever act on an id this turn's own search just
        # surfaced, never on an arbitrary model- or client-supplied id.
        found: dict[str, dict[str, dict]] = {"events": {}, "emails": {}}

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
            log.info("tool hop for thread %s: %s %s", thread_id, verb, arg)
            await queue.put(_sse("tool_call", {"tool": verb, "arg": arg}))
            messages.append({"role": "assistant", "content": content})
            with get_conn(db_path) as conn:
                tool_result = await _run_tool(
                    conn, db_path, thread_id, user_id, verb, arg, found
                )
            log.info(
                "tool hop for thread %s: %s %s -> %d char(s)",
                thread_id, verb, arg, len(tool_result),
            )
            await queue.put(_sse("tool_result", {"tool": verb, "arg": arg, "result": tool_result}))
            messages.append({"role": "user", "content": tool_result})
        else:
            # Ran out of hops still asking for a tool -- never show the raw
            # "TOOL: ..." line to the user. Nothing was streamed for that last
            # hop (it matched the tool pattern), so send the fallback now.
            if TOOL_RE.match(content.strip()):
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

        suggestions = await asyncio.to_thread(
            chat_followups_svc.generate_sync,
            db_path, question=message, answer=content, model=config.model,
        )
        if suggestions:
            await queue.put(_sse("suggestions", {"suggestions": suggestions}))
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
