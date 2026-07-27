"""Find the calendar events and emails that belong to a meeting.

Three steps: a deterministic keyword+window search against both MCP servers, one
LLM call to rank what came back, and an explicit user confirmation before
anything is attached.

The ranking is an accelerator, not a gate. If the LLM is unavailable the
candidates still come back unranked and the user can still tick boxes -- which
is the whole point of "user confirms".
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from app.config import effective
from app.db import get_conn, utcnow
from app.errors import MCPError, NotFoundError
from app.logging_config import get_logger
from app.services import llm as llm_svc
from app.services import mcpclient as mcp_svc
from app.services import prompts as prompts_svc
from app.services import threads as threads_svc

log = get_logger("matching")

SUGGEST_THRESHOLD = 0.6
SNIPPET_LIMIT = 400
DESCRIPTION_LIMIT = 300

STOPWORDS = {
    "a", "about", "above", "after", "again", "all", "also", "am", "an", "and", "any",
    "are", "as", "at", "be", "because", "been", "before", "being", "below", "between",
    "both", "but", "by", "can", "did", "do", "does", "doing", "down", "during", "each",
    "few", "for", "from", "further", "had", "has", "have", "having", "he", "her", "here",
    "hers", "him", "his", "how", "i", "if", "in", "into", "is", "it", "its", "just",
    "me", "more", "most", "my", "no", "nor", "not", "now", "of", "off", "on", "once",
    "only", "or", "other", "our", "out", "over", "own", "same", "she", "should", "so",
    "some", "such", "than", "that", "the", "their", "them", "then", "there", "these",
    "they", "this", "those", "through", "to", "too", "under", "until", "up", "very",
    "was", "we", "were", "what", "when", "where", "which", "while", "who", "whom",
    "why", "will", "with", "you", "your",
    # Meeting noise: present in almost every title, so useless for matching.
    "meeting", "meet", "call", "sync", "syncup", "standup", "weekly", "daily",
    "monthly", "quarterly", "notes", "recording", "session", "chat", "discussion",
    "review", "catchup", "checkin", "1on1", "one",
}

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9'&-]*")


def extract_keywords(*texts: str, limit: int = 8) -> list[str]:
    """Pick the words worth searching on.

    Tokens that were capitalised in the source keep their position at the front:
    proper nouns are what actually match a calendar entry or a correspondent.
    """
    proper: list[str] = []
    ordinary: list[str] = []
    seen: set[str] = set()

    for text in texts:
        if not text:
            continue
        for match in _WORD.finditer(text):
            raw = match.group(0)
            lowered = raw.lower()
            if len(lowered) < 3 or lowered in STOPWORDS or lowered in seen:
                continue
            seen.add(lowered)
            (proper if raw[0].isupper() else ordinary).append(lowered)

    return (proper + ordinary)[:limit]


def date_window(
    meeting_at: str | None, days_before: int, days_after: int
) -> tuple[datetime, datetime]:
    if meeting_at:
        try:
            anchor = datetime.fromisoformat(meeting_at)
        except ValueError:
            anchor = datetime.now(timezone.utc)
    else:
        anchor = datetime.now(timezone.utc)

    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)

    return anchor - timedelta(days=days_before), anchor + timedelta(days=days_after)


def iso_date(value: datetime) -> str:
    """Calendar search wants ISO-8601: 2026-03-11."""
    return value.strftime("%Y-%m-%d")


def normalize_timestamp(value: str | None) -> str | None:
    """Coerce a timestamp to ISO-8601.

    Calendar events arrive ISO-8601 but Gmail returns RFC 2822
    ("Wed, 15 Jul 2026 17:42:00 +0000"). Storing both verbatim makes the
    timeline sort lexically wrong -- "W" sorts above "2", so a July 15 email
    lands above a July 20 meeting.
    """
    if not value:
        return None

    text = value.strip()
    try:
        return datetime.fromisoformat(text).isoformat()
    except ValueError:
        pass

    try:
        from email.utils import parsedate_to_datetime

        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return text  # keep the original rather than losing information

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def gmail_date(value: datetime) -> str:
    """Gmail search wants slashes: after:2026/03/11. Not the same as ISO."""
    return value.strftime("%Y/%m/%d")


def build_gmail_query(keywords: list[str], start: datetime, end: datetime) -> str:
    parts = []
    if keywords:
        top = keywords[:3]
        parts.append(f"({' OR '.join(top)})" if len(top) > 1 else top[0])
    parts.append(f"after:{gmail_date(start)}")
    parts.append(f"before:{gmail_date(end)}")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Gathering
# --------------------------------------------------------------------------- #


def _truncate(value: str | None, limit: int) -> str:
    text = (value or "").strip()
    return text[:limit] + "…" if len(text) > limit else text


async def gather_candidates(
    conn_factory,
    meeting_id: int,
    *,
    keywords: list[str],
    start: datetime,
    end: datetime,
    max_candidates: int,
    user_id: int | None = None,
) -> dict:
    """Query both MCP servers concurrently. One being down is not fatal.

    ``user_id`` selects that user's own profile/token if they have one
    configured (Settings -> Integrations -> your account), so Jenny's meetings
    search her calendar and inbox rather than whoever owns the shared server
    config. With no override, everyone shares the server's default account.
    """
    with conn_factory() as conn:
        try:
            calendar_cfg = mcp_svc.resolve_effective_config(conn, "calendar", user_id)
        except NotFoundError:
            calendar_cfg = None
        try:
            email_cfg = mcp_svc.resolve_effective_config(conn, "email", user_id)
        except NotFoundError:
            email_cfg = None

        attached_uids = {
            r["uid"]
            for r in conn.execute(
                "SELECT uid FROM thread_calendar_events WHERE thread_id = "
                "(SELECT thread_id FROM meetings WHERE id = ?)",
                (meeting_id,),
            )
        }
        attached_msgs = {
            r["message_id"]
            for r in conn.execute(
                "SELECT message_id FROM thread_emails WHERE thread_id = "
                "(SELECT thread_id FROM meetings WHERE id = ?)",
                (meeting_id,),
            )
        }

    gmail_query = build_gmail_query(keywords, start, end)
    calendar_query = " ".join(keywords[:3])

    async def fetch_calendar() -> list[dict]:
        if calendar_cfg is None or not calendar_cfg.enabled:
            raise MCPError("Calendar server is not enabled", server="calendar")
        client = mcp_svc.MCPClient(calendar_cfg)
        # Two passes: a keyword search, plus a bare window sweep. The tool ANDs a
        # single query string, so an event whose title shares no keyword would
        # otherwise be invisible -- and the window is small enough to be cheap.
        keyworded, windowed = await asyncio.gather(
            client.search_events(
                query=calendar_query or None,
                start_date=iso_date(start),
                end_date=iso_date(end),
            ),
            client.search_events(start_date=iso_date(start), end_date=iso_date(end)),
            return_exceptions=True,
        )
        merged: dict[str, dict] = {}
        for batch in (keyworded, windowed):
            if isinstance(batch, BaseException):
                continue
            for item in batch:
                uid = item.get("uid")
                if uid:
                    merged.setdefault(uid, item)
        if not merged and isinstance(keyworded, BaseException):
            raise keyworded
        return list(merged.values())

    async def fetch_email() -> list[dict]:
        if email_cfg is None or not email_cfg.enabled:
            raise MCPError("Email server is not enabled", server="email")
        return await mcp_svc.MCPClient(email_cfg).search_emails(gmail_query)

    events_result, emails_result = await asyncio.gather(
        fetch_calendar(), fetch_email(), return_exceptions=True
    )

    calendar_error = None
    events: list[dict] = []
    if isinstance(events_result, BaseException):
        calendar_error = getattr(events_result, "message", str(events_result))
        log.warning("calendar search failed: %s", calendar_error)
    else:
        events = [e for e in events_result if e.get("uid") not in attached_uids]

    email_error = None
    emails: list[dict] = []
    if isinstance(emails_result, BaseException):
        email_error = getattr(emails_result, "message", str(emails_result))
        log.warning("email search failed: %s", email_error)
    else:
        emails = [
            m for m in emails_result if m.get("message_id") not in attached_msgs
        ]

    # Nearest-in-time first, so the cap keeps what is most likely to matter.
    anchor = start + (end - start) / 2

    def event_distance(item: dict) -> float:
        try:
            value = datetime.fromisoformat(item.get("start") or "")
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return abs((value - anchor).total_seconds())
        except (ValueError, TypeError):
            return float("inf")

    events.sort(key=event_distance)
    emails.sort(key=lambda m: m.get("date") or "", reverse=True)

    events = events[:max_candidates]
    emails = emails[:max_candidates]

    status = "ok"
    if calendar_error and email_error:
        status = "failed"
    elif calendar_error or email_error:
        status = "partial"

    return {
        "status": status,
        "events": events,
        "emails": emails,
        "calendar_error": calendar_error,
        "email_error": email_error,
        "query": {
            "keywords": keywords,
            "calendar": {
                "query": calendar_query,
                "start_date": iso_date(start),
                "end_date": iso_date(end),
            },
            "email": {"query": gmail_query},
        },
    }


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #


def build_rank_payload(context: dict, events: list[dict], emails: list[dict]) -> dict:
    """Short opaque refs so the model can't echo -- or hallucinate -- a long UID."""
    return {
        "context": context,
        "calendar_candidates": [
            {
                "ref": f"c{i}",
                "summary": e.get("summary"),
                "description": _truncate(e.get("description"), DESCRIPTION_LIMIT),
                "location": e.get("location"),
                "start": e.get("start"),
                "end": e.get("end"),
                "calendar_name": e.get("calendar_name"),
                "account": e.get("account"),
            }
            for i, e in enumerate(events)
        ],
        "email_candidates": [
            {
                "ref": f"e{i}",
                "subject": m.get("subject"),
                "sender": m.get("sender"),
                "date": m.get("date"),
                "snippet": _truncate(m.get("snippet"), SNIPPET_LIMIT),
                "account": m.get("account"),
            }
            for i, m in enumerate(emails)
        ],
    }


def apply_ranking(
    items: list[dict], ranked: list[dict], prefix: str
) -> list[dict]:
    """Merge scores onto candidates by ref, defaulting anything the model skipped."""
    by_ref = {}
    for entry in ranked or []:
        ref = entry.get("ref")
        if isinstance(ref, str):
            by_ref[ref] = entry

    out = []
    for i, item in enumerate(items):
        ref = f"{prefix}{i}"
        entry = by_ref.pop(ref, None)
        if entry is None:
            out.append({**item, "relevance_score": 0.0,
                        "relevance_reason": "not ranked", "suggested": False})
            continue

        try:
            score = max(0.0, min(1.0, float(entry.get("score", 0))))
        except (TypeError, ValueError):
            score = 0.0

        suggested = entry.get("suggested")
        if not isinstance(suggested, bool):
            suggested = score >= SUGGEST_THRESHOLD

        out.append(
            {
                **item,
                "relevance_score": score,
                "relevance_reason": _truncate(entry.get("reason"), 240) or "",
                "suggested": suggested,
            }
        )

    if by_ref:
        log.warning("ranking returned unknown refs, dropped: %s", sorted(by_ref))

    out.sort(key=lambda x: x["relevance_score"], reverse=True)
    return out


def rank_candidates_sync(
    db_path, meeting_id: int, gathered: dict, model: str | None = None
) -> dict:
    """One LLM call. On failure the candidates come back unranked, not lost."""
    with get_conn(db_path) as conn:
        meeting = threads_svc.require_meeting(conn, meeting_id)
        thread = threads_svc.require_thread(conn, meeting["thread_id"])
        config = llm_svc.LLMConfig.from_db(conn, model_override=model)
        tldr = conn.execute(
            "SELECT tldr FROM summaries WHERE meeting_id = ? AND is_current = 1",
            (meeting_id,),
        ).fetchone()

    events, emails = gathered["events"], gathered["emails"]
    if not events and not emails:
        return {"events": [], "emails": [], "model": None, "prompt_sha256": None,
                "error": None, "notes": ""}

    context = {
        "thread_title": thread["title"],
        "thread_description": thread["description"] or "",
        "meeting_title": meeting["title"],
        "meeting_datetime": meeting["meeting_at"],
        "meeting_tldr": (tldr["tldr"] if tldr else "") or "",
        "keywords": gathered["query"]["keywords"],
    }

    prompt = prompts_svc.load("match_rank_prompt")
    if prompt.temperature is not None:
        config.temperature = prompt.temperature

    payload = build_rank_payload(context, events, emails)
    system, user = prompt.render(
        {"payload": json.dumps(payload, ensure_ascii=False, indent=2)}
    )

    try:
        parsed, _, _ = llm_svc.chat_json(config, system, user)
    except Exception as exc:
        log.warning("ranking failed, returning unranked candidates: %s", exc)
        return {
            "events": [
                {**e, "relevance_score": None, "relevance_reason": "", "suggested": False}
                for e in events
            ],
            "emails": [
                {**m, "relevance_score": None, "relevance_reason": "", "suggested": False}
                for m in emails
            ],
            "model": config.model,
            "prompt_sha256": prompt.sha256,
            "error": str(exc),
            "notes": "",
        }

    return {
        "events": apply_ranking(events, parsed.get("calendar") or [], "c"),
        "emails": apply_ranking(emails, parsed.get("email") or [], "e"),
        "model": config.model,
        "prompt_sha256": prompt.sha256,
        "error": None,
        "notes": parsed.get("notes") or "",
    }


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def save_match_run(
    conn: sqlite3.Connection,
    *,
    meeting_id: int,
    thread_id: int,
    user_id: int,
    job_id: str | None,
    gathered: dict,
    ranked: dict,
) -> int:
    status = gathered["status"]
    if status == "ok" and ranked.get("error"):
        status = "partial"

    cur = conn.execute(
        """
        INSERT INTO match_runs (meeting_id, thread_id, user_id, job_id, status,
            query_json, candidates_json, ranked_json, model, prompt_sha256,
            email_error, calendar_error, error, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            meeting_id, thread_id, user_id, job_id, status,
            json.dumps(gathered["query"]),
            json.dumps({"events": gathered["events"], "emails": gathered["emails"]}),
            json.dumps({"events": ranked["events"], "emails": ranked["emails"],
                        "notes": ranked.get("notes", "")}),
            ranked.get("model"), ranked.get("prompt_sha256"),
            gathered.get("email_error"), gathered.get("calendar_error"),
            ranked.get("error"), utcnow(),
        ),
    )
    return cur.lastrowid


def latest_match_run(conn: sqlite3.Connection, meeting_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM match_runs WHERE meeting_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
        (meeting_id,),
    ).fetchone()
    if row is None:
        return None

    ranked = json.loads(row["ranked_json"] or "{}")
    return {
        "id": row["id"],
        "status": row["status"],
        "query": json.loads(row["query_json"] or "{}"),
        "events": ranked.get("events", []),
        "emails": ranked.get("emails", []),
        "notes": ranked.get("notes", ""),
        "model": row["model"],
        "calendar_error": row["calendar_error"],
        "email_error": row["email_error"],
        "error": row["error"],
        "created_at": row["created_at"],
    }


def attached_context(conn: sqlite3.Connection, meeting_id: int) -> dict:
    """Calendar events and emails the user confirmed as relevant to this meeting.

    Scoped to this meeting, not the whole thread: a thread can span many
    meetings, and pulling in items attached via a different meeting's match
    would feed the summarizer content the user never confirmed for this one.
    """
    events = conn.execute(
        """
        SELECT summary, start_at, location, calendar_name, description
        FROM thread_calendar_events WHERE meeting_id = ? ORDER BY start_at
        """,
        (meeting_id,),
    ).fetchall()
    emails = conn.execute(
        """
        SELECT subject, sender, date, snippet
        FROM thread_emails WHERE meeting_id = ? ORDER BY date
        """,
        (meeting_id,),
    ).fetchall()
    return {
        "events": [dict(r) for r in events],
        "emails": [dict(r) for r in emails],
    }


def attach_selected(
    conn: sqlite3.Connection,
    *,
    meeting_id: int,
    user_id: int,
    event_uids: list[str],
    email_message_ids: list[str],
    append_event_title: bool,
) -> dict:
    """Attach the user's picks to the thread. Idempotent."""
    meeting = threads_svc.require_meeting(conn, meeting_id)
    thread_id = meeting["thread_id"]

    run = latest_match_run(conn, meeting_id)
    if run is None:
        raise NotFoundError("Run a match first — there are no candidates to confirm")

    events_by_uid = {e.get("uid"): e for e in run["events"]}
    emails_by_id = {m.get("message_id"): m for m in run["emails"]}

    attached_events = 0
    chosen_event = None
    for uid in event_uids:
        event = events_by_uid.get(uid)
        if event is None:
            continue
        chosen_event = chosen_event or event
        conn.execute(
            """
            INSERT INTO thread_calendar_events (thread_id, meeting_id, uid, url, summary,
                description, location, start_at, end_at, calendar_name, account,
                event_type, raw_json, relevance_score, relevance_reason,
                attached_by, attached_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(thread_id, uid) DO UPDATE SET
                meeting_id = excluded.meeting_id,
                relevance_score = excluded.relevance_score,
                relevance_reason = excluded.relevance_reason
            """,
            (
                thread_id, meeting_id, uid, event.get("url"), event.get("summary"),
                event.get("description"), event.get("location"),
                normalize_timestamp(event.get("start")),
                normalize_timestamp(event.get("end")),
                event.get("calendar_name"), event.get("account"),
                event.get("type"), json.dumps(event),
                event.get("relevance_score"), event.get("relevance_reason"),
                user_id, utcnow(),
            ),
        )
        attached_events += 1

    attached_emails = 0
    for message_id in email_message_ids:
        email = emails_by_id.get(message_id)
        if email is None:
            continue
        conn.execute(
            """
            INSERT INTO thread_emails (thread_id, meeting_id, mcp_id, message_id, sender,
                subject, date, snippet, account, triage_level, tag, reason, summary,
                score, raw_json, relevance_score, relevance_reason, attached_by, attached_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(thread_id, message_id) DO UPDATE SET
                meeting_id = excluded.meeting_id,
                relevance_score = excluded.relevance_score,
                relevance_reason = excluded.relevance_reason
            """,
            (
                thread_id, meeting_id, email.get("id"), message_id, email.get("sender"),
                email.get("subject"), normalize_timestamp(email.get("date")),
                email.get("snippet"),
                email.get("account"), email.get("triage_level"), email.get("tag"),
                email.get("reason"), email.get("summary"), email.get("score"),
                json.dumps(email), email.get("relevance_score"),
                email.get("relevance_reason"), user_id, utcnow(),
            ),
        )
        attached_emails += 1

    title_changed = False
    if append_event_title and chosen_event and chosen_event.get("summary"):
        suffix = f" — {chosen_event['summary'].strip()}"
        current = meeting["title"] or ""
        # Idempotent: confirming twice must not stack the suffix.
        if not current.endswith(suffix):
            conn.execute(
                "UPDATE meetings SET title = ?, updated_at = ? WHERE id = ?",
                (current + suffix, utcnow(), meeting_id),
            )
            title_changed = True

    threads_svc.touch_thread(conn, thread_id)

    return {
        "attached_events": attached_events,
        "attached_emails": attached_emails,
        "title_changed": title_changed,
        "meeting": threads_svc.row_to_meeting(threads_svc.require_meeting(conn, meeting_id)),
    }


# --------------------------------------------------------------------------- #
# Job body
# --------------------------------------------------------------------------- #


async def run_match(ctx) -> dict:
    from app.db import connect

    meeting_id = int(ctx.payload["meeting_id"])
    user_id = int(ctx.payload["user_id"])

    with get_conn(ctx.db_path) as conn:
        meeting = threads_svc.require_meeting(conn, meeting_id)
        thread = threads_svc.require_thread(conn, meeting["thread_id"])
        days_before = ctx.payload.get("window_days_before") or effective(
            conn, "match_window_days_before"
        )
        days_after = ctx.payload.get("window_days_after") or effective(
            conn, "match_window_days_after"
        )
        max_candidates = effective(conn, "match_max_candidates")
        max_keywords = effective(conn, "match_max_keywords")

    keywords = extract_keywords(
        thread["title"] or "",
        thread["description"] or "",
        meeting["title"] or "",
        limit=max_keywords,
    )
    keywords.extend(
        k for k in (ctx.payload.get("extra_keywords") or []) if k not in keywords
    )

    start, end = date_window(meeting["meeting_at"], days_before, days_after)

    ctx.stage("gathering", f"Searching {iso_date(start)} to {iso_date(end)}")
    ctx.event(f"Keywords: {', '.join(keywords) or '(none)'}", stage="gathering")

    def conn_factory():
        return get_conn(ctx.db_path)

    gathered = await gather_candidates(
        conn_factory,
        meeting_id,
        keywords=keywords,
        start=start,
        end=end,
        max_candidates=max_candidates,
        user_id=user_id,
    )

    if gathered["calendar_error"]:
        ctx.event(f"Calendar: {gathered['calendar_error']}", stage="gathering", level="warn")
    if gathered["email_error"]:
        ctx.event(f"Email: {gathered['email_error']}", stage="gathering", level="warn")
    ctx.event(
        f"Found {len(gathered['events'])} events and {len(gathered['emails'])} emails",
        stage="gathering",
    )
    ctx.complete_stage()

    ctx.stage("ranking", "Ranking candidates")
    ranked = await asyncio.to_thread(
        rank_candidates_sync, ctx.db_path, meeting_id, gathered, ctx.payload.get("model")
    )
    if ranked.get("error"):
        ctx.event(
            f"Ranking unavailable ({ranked['error'][:120]}); showing unranked results",
            stage="ranking",
            level="warn",
        )
    ctx.complete_stage()

    with get_conn(ctx.db_path) as conn:
        run_id = save_match_run(
            conn,
            meeting_id=meeting_id,
            thread_id=meeting["thread_id"],
            user_id=user_id,
            job_id=ctx.job_id,
            gathered=gathered,
            ranked=ranked,
        )

    ctx.stage("done", "Finished")
    ctx.complete_stage()

    suggested = sum(
        1 for x in ranked["events"] + ranked["emails"] if x.get("suggested")
    )
    return {
        "match_run_id": run_id,
        "events": len(ranked["events"]),
        "emails": len(ranked["emails"]),
        "suggested": suggested,
        "status": gathered["status"],
    }
