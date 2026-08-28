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
from app.errors import NoIntegrationsError, NotFoundError
from app.logging_config import get_logger
from app.services import llm as llm_svc
from app.services import prompts as prompts_svc
from app.services import threads as threads_svc
from app.services.providers import loader as providers_svc

# Native query syntax lives with the providers that speak it. Re-exported here
# because these names are part of this module's established surface.
from app.services.providers.query import (  # noqa: F401
    build_gmail_query,
    gmail_date,
    iso_date,
)

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


# --------------------------------------------------------------------------- #
# Gathering
# --------------------------------------------------------------------------- #


def _truncate(value: str | None, limit: int) -> str:
    text = (value or "").strip()
    return text[:limit] + "…" if len(text) > limit else text


async def gather_candidates(
    conn_factory,
    *,
    thread_id: int,
    keywords: list[str],
    start: datetime,
    end: datetime,
    max_candidates: int,
    user_id: int | None = None,
    calendar_start: datetime | None = None,
    calendar_end: datetime | None = None,
) -> dict:
    """Search every calendar and inbox this user has connected, concurrently.

    ``calendar_start``/``calendar_end`` default to ``start``/``end`` and let the
    calendar search run its own, usually wider, window than email -- a
    date-range list call costs a calendar provider nothing extra, and
    interviews get booked much further out than an email ever goes unanswered.

    One account being down is not fatal, and that property has to survive a user
    adding a second account: an aggregate error is only reported when *every*
    source of that kind failed. Otherwise the warning banner would fire on a
    perfectly good search just because one of three calendars was unreachable.

    Integrations are per-user, so this searches the requesting user's own accounts
    and nobody else's.

    Scoped to a thread, not a meeting: what has already been attached is a
    property of the thread, and the periodic sweep in ``followups`` has a thread
    but no meeting to anchor on.
    """
    with conn_factory() as conn:
        calendar_sources = providers_svc.load_for_user(conn, user_id, kind="calendar")
        email_sources = providers_svc.load_for_user(conn, user_id, kind="email")

        attached_uids = {
            r["uid"]
            for r in conn.execute(
                "SELECT uid FROM thread_calendar_events WHERE thread_id = ?",
                (thread_id,),
            )
        }
        attached_msgs = {
            r["message_id"]
            for r in conn.execute(
                "SELECT message_id FROM thread_emails WHERE thread_id = ?",
                (thread_id,),
            )
        }

    if not calendar_sources and not email_sources:
        raise NoIntegrationsError(
            "Connect a calendar or email account in Settings → Integrations before "
            "matching a meeting."
        )

    cal_start = calendar_start if calendar_start is not None else start
    cal_end = calendar_end if calendar_end is not None else end

    gmail_query = build_gmail_query(keywords, start, end)
    calendar_query = " ".join(keywords[:3])

    # (kind, provider, coroutine). Coroutines do not run until gathered.
    planned: list[tuple[str, object, object]] = [
        (
            "calendar",
            source,
            source.search_events(query=calendar_query or None, start=cal_start, end=cal_end),
        )
        for source in calendar_sources
    ] + [
        ("email", source, source.search_emails(keywords=keywords, start=start, end=end))
        for source in email_sources
    ]

    results = await asyncio.gather(
        *(coro for _, _, coro in planned), return_exceptions=True
    )

    events: list[dict] = []
    emails: list[dict] = []
    source_errors: list[dict] = []
    failures = {"calendar": 0, "email": 0}

    for (kind, source, _), result in zip(planned, results):
        if isinstance(result, BaseException):
            message = getattr(result, "message", None) or str(result)
            failures[kind] += 1
            source_errors.append(
                {
                    "kind": kind,
                    "provider": source.ref.provider,
                    "integration_id": source.ref.id,
                    "account": source.ref.display,
                    "error": message,
                }
            )
            log.warning("%s search failed for %s: %s", kind, source.ref.display, message)
            continue

        bucket = events if kind == "calendar" else emails
        bucket.extend(candidate.to_dict() for candidate in result)

    events = dedupe_events(events, attached_uids)
    emails = _dedupe_emails(emails, attached_msgs)

    # Nearest-in-time first, so the cap keeps what is most likely to matter.
    # Uses the calendar window specifically, since it may be wider than email's.
    event_anchor = cal_start + (cal_end - cal_start) / 2

    def event_distance(item: dict) -> float:
        try:
            value = datetime.fromisoformat(item.get("start") or "")
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return abs((value - event_anchor).total_seconds())
        except (ValueError, TypeError):
            return float("inf")

    events.sort(key=event_distance)
    emails.sort(key=lambda m: m.get("date") or "", reverse=True)

    events = events[:max_candidates]
    emails = emails[:max_candidates]

    # Aggregates for the SPA's warning banner, kept for API compatibility. Set
    # only when a kind had sources and every one of them failed -- a kind with no
    # sources at all is a configuration choice, not an error, and reporting it
    # would make every search 'partial' for a user who only connected email.
    calendar_error = aggregate_error(source_errors, "calendar", calendar_sources, failures)
    email_error = aggregate_error(source_errors, "email", email_sources, failures)

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
        "source_errors": source_errors,
        "query": {
            "keywords": keywords,
            # Kept because a zero-result match has to stay debuggable from the UI.
            # With several providers there is no single native query, so these two
            # are the representative rendering and `sources` carries the detail.
            "calendar": {
                "query": calendar_query,
                "start_date": iso_date(cal_start),
                "end_date": iso_date(cal_end),
            },
            "email": {"query": gmail_query},
            "sources": [
                {
                    "kind": kind,
                    "provider": source.ref.provider,
                    "integration_id": source.ref.id,
                    "account": source.ref.display,
                }
                for kind, source, _ in planned
            ],
        },
    }


def aggregate_error(
    source_errors: list[dict], kind: str, sources: list, failures: dict[str, int]
) -> str | None:
    """The banner-level error for one kind of source, or None.

    Public because the upcoming-events listing fans out the same way and has to
    apply the same rule: set it only when *every* account of that kind failed,
    or adding a second calendar makes every search look broken.
    """
    if not sources or failures[kind] < len(sources):
        return None
    messages = [e["error"] for e in source_errors if e["kind"] == kind]
    if len(sources) == 1:
        return messages[0] if messages else None
    # Name the accounts: "all my calendars failed" is only actionable if the user
    # can see which ones and why.
    return "; ".join(
        f"{e['account']}: {e['error']}" for e in source_errors if e["kind"] == kind
    )


def dedupe_events(events: list[dict], attached_uids: set[str]) -> list[dict]:
    """Drop already-attached events, then collapse the same event seen twice.

    Two providers can surface one real event (a Google account connected both
    directly and through the calendar MCP server). They agree on the provider's
    own ``source_uid`` but not on ``uid``, which is namespaced per integration --
    so the cross-provider key is (source_uid, start).

    Pass an empty ``attached_uids`` to get the collapse without the filtering:
    the upcoming-events list shows attached events rather than hiding them.
    """
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for event in events:
        if event.get("uid") in attached_uids:
            continue
        key = (event.get("source_uid") or event.get("uid") or "", event.get("start") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(event)
    return out


def _dedupe_emails(emails: list[dict], attached_msgs: set[str]) -> list[dict]:
    """Same idea for mail, keyed on the RFC 2822 Message-ID where we have one."""
    seen: set[str] = set()
    out: list[dict] = []
    for mail in emails:
        if mail.get("message_id") in attached_msgs:
            continue
        key = mail.get("rfc_message_id") or mail.get("message_id") or ""
        if key in seen:
            continue
        seen.add(key)
        out.append(mail)
    return out


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
                # Gmail's query has no `-in:sent`, so a candidate list routinely
                # contains the user's own mail. "I wrote about Atlas" and
                # "someone asked me about Atlas" are different evidence for
                # whether this belongs to the meeting, and the ranker could not
                # previously tell them apart. NULL where it is not known.
                "direction": m.get("direction"),
                "to": m.get("to_recipients"),
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


def meeting_context(conn: sqlite3.Connection, meeting_id: int, keywords: list[str]) -> dict:
    """What the ranking prompt is told about the thing being matched."""
    meeting = threads_svc.require_meeting(conn, meeting_id)
    thread = threads_svc.require_thread(conn, meeting["thread_id"])
    tldr = conn.execute(
        "SELECT tldr FROM summaries WHERE meeting_id = ? AND is_current = 1",
        (meeting_id,),
    ).fetchone()

    return {
        "thread_title": thread["title"],
        "thread_description": thread["description"] or "",
        "meeting_title": meeting["title"],
        "meeting_datetime": meeting["meeting_at"],
        "meeting_tldr": (tldr["tldr"] if tldr else "") or "",
        "keywords": keywords,
    }


def rank_candidates_sync(
    db_path, meeting_id: int, gathered: dict, model: str | None = None
) -> dict:
    """Rank a meeting's candidates. Thin wrapper: the work is in :func:`rank_sync`."""
    with get_conn(db_path) as conn:
        context = meeting_context(conn, meeting_id, gathered["query"]["keywords"])
    return rank_sync(db_path, context, gathered, model)


def rank_sync(db_path, context: dict, gathered: dict, model: str | None = None) -> dict:
    """One LLM call. On failure the candidates come back unranked, not lost.

    Takes the context rather than a meeting id so the periodic thread sweep can
    reuse it: same prompt, same scores, same "unranked beats nothing" fallback.
    """
    with get_conn(db_path) as conn:
        config = llm_svc.LLMConfig.from_db(conn, model_override=model)

    events, emails = gathered["events"], gathered["emails"]
    if not events and not emails:
        return {"events": [], "emails": [], "model": None, "prompt_sha256": None,
                "error": None, "notes": ""}

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
            email_error, calendar_error, source_errors_json, error, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            meeting_id, thread_id, user_id, job_id, status,
            json.dumps(gathered["query"]),
            json.dumps({"events": gathered["events"], "emails": gathered["emails"]}),
            json.dumps({"events": ranked["events"], "emails": ranked["emails"],
                        "notes": ranked.get("notes", "")}),
            ranked.get("model"), ranked.get("prompt_sha256"),
            gathered.get("email_error"), gathered.get("calendar_error"),
            json.dumps(gathered.get("source_errors") or []),
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
        # Per-account detail behind the aggregate errors above. Older rows predate
        # the column, hence the guarded access.
        "source_errors": json.loads(
            (row["source_errors_json"] if "source_errors_json" in row.keys() else None) or "[]"
        ),
        "error": row["error"],
        "created_at": row["created_at"],
    }


def attached_context(conn: sqlite3.Connection, meeting_id: int) -> dict:
    """Calendar events and emails the user confirmed as relevant to this meeting.

    Scoped to this meeting, not the whole thread: a thread can span many
    meetings, and pulling in items attached via a different meeting's match
    would feed the summarizer content the user never confirmed for this one.

    Two things deliberately absent, both of which the next person will want to
    add:

    **No email bodies.** This feeds the summarizer, whose input is a meeting's
    transcript; a full inbox alongside it changes what the minutes are written
    from. ``ai_summary`` goes instead -- one labelled sentence, bounded by the
    same ``CONTEXT_SNIPPET_LIMIT`` as a snippet.

    **No conversation grouping.** Chains are a whole-thread notion and this is
    scoped per meeting, so two messages of one exchange can be attached under
    different meetings -- or one under a meeting and one under NULL from the
    sweep. Grouping here would render a fragment and present it as the whole
    conversation. Chains belong only where the scope is the whole thread:
    ``next_step._payload``, ``chat._format_attachments`` and the chains route.
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
        SELECT subject, sender, date, snippet, direction, ai_summary
        FROM thread_emails WHERE meeting_id = ? ORDER BY date
        """,
        (meeting_id,),
    ).fetchall()
    return {
        "events": [dict(r) for r in events],
        "emails": [dict(r) for r in emails],
    }


def _unread_flags(auto: bool) -> tuple[int, str | None]:
    """``(auto_attached, seen_at)`` for a newly attached row.

    Anything a person ticked is seen the moment it lands -- they were looking at
    it. Only the sweep leaves ``seen_at`` NULL, which is what makes the blue dot
    and the bold row mean "the app did this while you were away".
    """
    return (1, None) if auto else (0, utcnow())


def attach_event(
    conn: sqlite3.Connection,
    *,
    thread_id: int,
    meeting_id: int | None,
    event: dict,
    user_id: int,
    auto: bool = False,
) -> None:
    """Write one calendar event onto a thread. Idempotent per (thread, uid).

    The single place that knows this column list: the match-confirm flow, the
    "create a meeting from an upcoming event" flow and the periodic sweep all
    land here, and a column added in one but not the other is exactly how
    ``attached_context`` -- and so the summarizer -- ends up reading a NULL.

    ``meeting_id`` is None for a sweep: ``attached_context`` is per-meeting, so a
    follow-up nobody has confirmed must not silently become an input to an
    existing meeting's next summary.
    """
    auto_attached, seen_at = _unread_flags(auto)
    conn.execute(
        """
        INSERT INTO thread_calendar_events (thread_id, meeting_id, uid, url, summary,
            description, location, start_at, end_at, calendar_name, account,
            event_type, raw_json, relevance_score, relevance_reason,
            source_uid, provider, auto_attached, seen_at, attached_by, attached_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(thread_id, uid) DO UPDATE SET
            meeting_id = excluded.meeting_id,
            relevance_score = excluded.relevance_score,
            relevance_reason = excluded.relevance_reason
        """,
        (
            thread_id, meeting_id, event.get("uid"), event.get("url"),
            event.get("summary"), event.get("description"), event.get("location"),
            normalize_timestamp(event.get("start")),
            normalize_timestamp(event.get("end")),
            event.get("calendar_name"), event.get("account"),
            event.get("type"), json.dumps(event),
            event.get("relevance_score"), event.get("relevance_reason"),
            event.get("source_uid"), event.get("provider"),
            auto_attached, seen_at,
            user_id, utcnow(),
        ),
    )


def attach_email(
    conn: sqlite3.Connection,
    *,
    thread_id: int,
    meeting_id: int | None,
    email: dict,
    user_id: int,
    auto: bool = False,
) -> None:
    """Write one email onto a thread. Idempotent per (thread, message_id).

    The counterpart to :func:`attach_event`, and for the same reason: two callers
    writing this column list independently is how one of them starts leaving a
    column NULL.

    Deliberately does **not** write `body`, `body_fetched_at`, `ai_summary` or
    `ai_summary_model`. No provider has a body at search time, so those belong to
    hydration (`services/email_bodies.py`) and writing NULLs for them here would
    let a re-attach wipe a body someone already fetched.

    Note the ``COALESCE`` in the conflict clause. Re-attaching is the only way a
    row that predates these columns ever gets them, so the clause has to update
    them -- but a bare ``excluded.x`` would let a *second* attach from a provider
    with no headers (Zoho, MCP) **erase** threading a first attach had stored.
    The clause could not lose data while it only touched meeting_id and the
    relevance pair; extending it can, and COALESCE is what keeps it write-once.
    """
    auto_attached, seen_at = _unread_flags(auto)
    references = email.get("references") or ()
    conn.execute(
        """
        INSERT INTO thread_emails (thread_id, meeting_id, mcp_id, message_id, sender,
            subject, date, snippet, account, triage_level, tag, reason, summary,
            score, raw_json, relevance_score, relevance_reason,
            url, rfc_message_id, provider, auto_attached, seen_at,
            attached_by, attached_at, folder_id,
            conversation_id, in_reply_to, references_json, to_recipients,
            cc_recipients, direction, integration_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(thread_id, message_id) DO UPDATE SET
            meeting_id = excluded.meeting_id,
            relevance_score = excluded.relevance_score,
            relevance_reason = excluded.relevance_reason,
            conversation_id = COALESCE(excluded.conversation_id, conversation_id),
            in_reply_to     = COALESCE(excluded.in_reply_to, in_reply_to),
            references_json = COALESCE(excluded.references_json, references_json),
            to_recipients   = COALESCE(excluded.to_recipients, to_recipients),
            cc_recipients   = COALESCE(excluded.cc_recipients, cc_recipients),
            direction       = COALESCE(excluded.direction, direction),
            integration_id  = COALESCE(excluded.integration_id, integration_id),
            rfc_message_id  = COALESCE(excluded.rfc_message_id, rfc_message_id)
        """,
        (
            thread_id, meeting_id, email.get("id"), email.get("message_id"),
            email.get("sender"), email.get("subject"),
            normalize_timestamp(email.get("date")), email.get("snippet"),
            email.get("account"), email.get("triage_level"), email.get("tag"),
            email.get("reason"), email.get("summary"), email.get("score"),
            json.dumps(email), email.get("relevance_score"),
            email.get("relevance_reason"),
            email.get("url"), email.get("rfc_message_id"), email.get("provider"),
            auto_attached, seen_at,
            user_id, utcnow(), email.get("folder_id"),
            email.get("conversation_id"), email.get("in_reply_to"),
            # NULL rather than "[]" for no references, so the COALESCE above
            # treats "this provider has none" as "leave whatever is there".
            json.dumps(list(references)) if references else None,
            email.get("to_recipients"), email.get("cc_recipients"),
            email.get("direction"), email.get("integration_id"),
        ),
    )


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
        attach_event(
            conn,
            thread_id=thread_id,
            meeting_id=meeting_id,
            event=event,
            user_id=user_id,
        )
        attached_events += 1

    attached_emails = 0
    for message_id in email_message_ids:
        email = emails_by_id.get(message_id)
        if email is None:
            continue
        attach_email(
            conn,
            thread_id=thread_id,
            meeting_id=meeting_id,
            email=email,
            user_id=user_id,
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
        calendar_days_before = ctx.payload.get(
            "calendar_window_days_before"
        ) or effective(conn, "match_window_calendar_days_before")
        calendar_days_after = ctx.payload.get(
            "calendar_window_days_after"
        ) or effective(conn, "match_window_calendar_days_after")
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
    calendar_start, calendar_end = date_window(
        meeting["meeting_at"], calendar_days_before, calendar_days_after
    )

    ctx.stage(
        "gathering",
        f"Searching calendar {iso_date(calendar_start)} to {iso_date(calendar_end)}, "
        f"email {iso_date(start)} to {iso_date(end)}",
    )
    ctx.event(f"Keywords: {', '.join(keywords) or '(none)'}", stage="gathering")

    def conn_factory():
        return get_conn(ctx.db_path)

    gathered = await gather_candidates(
        conn_factory,
        thread_id=meeting["thread_id"],
        keywords=keywords,
        start=start,
        end=end,
        calendar_start=calendar_start,
        calendar_end=calendar_end,
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
