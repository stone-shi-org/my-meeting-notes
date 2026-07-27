"""Summary generation and versioning.

Every run stores the model, the full prompt text and its sha256 alongside the
output, so an edited prompt file never orphans the history of what produced an
existing summary.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time

from pydantic import BaseModel, Field, ValidationError as PydanticValidationError

from app.config import effective, get_settings
from app.db import get_conn, utcnow
from app.errors import LLMError, NotFoundError
from app.logging_config import get_logger
from app.services import llm as llm_svc
from app.services import matching as matching_svc
from app.services import prompts as prompts_svc
from app.services import threads as threads_svc
from app.services import transcript as transcript_svc

log = get_logger("summarize")

CONTEXT_SNIPPET_LIMIT = 240


# Defaults on every field: a model that omits open_questions must not fail a
# job that already spent minutes on diarization.
class KeyDecision(BaseModel):
    decision: str = ""
    context: str = ""
    made_by: str = ""


class ActionItem(BaseModel):
    text: str = ""
    owner: str = ""
    owner_speaker: str = ""
    due_text: str = ""
    due_date: str = ""
    priority: str = "medium"
    confidence: float = 0.5


class Participant(BaseModel):
    speaker: str = ""
    inferred_name: str = ""
    evidence: str = ""


class MeetingSummaryResult(BaseModel):
    title_suggestion: str = ""
    tldr: str = ""
    summary_md: str = ""
    topics: list[str] = Field(default_factory=list)
    key_decisions: list[KeyDecision] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    participants: list[Participant] = Field(default_factory=list)


def _render_related_context(context: dict) -> str:
    events = context.get("events") or []
    emails = context.get("emails") or []
    if not events and not emails:
        return "(none confirmed for this meeting)"

    lines: list[str] = []
    for e in events:
        when = (e.get("start_at") or "")[:16].replace("T", " ")
        where = e.get("location") or e.get("calendar_name") or ""
        bits = ", ".join(b for b in (when, where) if b)
        header = f"[Event] {e.get('summary') or 'Untitled'}"
        if bits:
            header += f" ({bits})"
        lines.append(header)
        if e.get("description"):
            lines.append(f"  {e['description'][:CONTEXT_SNIPPET_LIMIT].strip()}")

    for m in emails:
        when = (m.get("date") or "")[:10]
        meta = ", ".join(b for b in (m.get("sender"), when) if b)
        header = f"[Email] {m.get('subject') or '(no subject)'}"
        if meta:
            header += f" ({meta})"
        lines.append(header)
        if m.get("snippet"):
            lines.append(f"  {m['snippet'][:CONTEXT_SNIPPET_LIMIT].strip()}")

    return "\n".join(lines)


def build_prompt_values(
    conn: sqlite3.Connection, meeting_id: int, transcript: dict, transcript_text: str
) -> dict[str, str]:
    meeting = threads_svc.require_meeting(conn, meeting_id)
    thread = threads_svc.require_thread(conn, meeting["thread_id"])

    speakers = ", ".join(
        s["display_name"] or s["id"] for s in transcript.get("speakers", [])
    )
    duration = transcript.get("duration") or meeting["audio_duration_sec"] or 0
    related_context = _render_related_context(matching_svc.attached_context(conn, meeting_id))

    return {
        "thread_title": thread["title"] or "",
        "thread_description": thread["description"] or "",
        "meeting_title": meeting["title"] or "",
        "meeting_date": (meeting["meeting_at"] or "")[:10],
        "duration_human": transcript_svc.fmt_clock(duration),
        "speaker_list": speakers,
        "related_context": related_context,
        "transcript": transcript_text,
    }


def _chunk_segments(segments: list[dict], max_chars: int) -> list[list[dict]]:
    """Split on speaker boundaries so a chunk never bisects a turn."""
    chunks: list[list[dict]] = []
    current: list[dict] = []
    size = 0

    for seg in segments:
        seg_len = len(seg.get("text") or "") + 40
        if current and size + seg_len > max_chars:
            chunks.append(current)
            current, size = [], 0
        current.append(seg)
        size += seg_len

    if current:
        chunks.append(current)
    return chunks


def _render_segments(segments: list[dict]) -> str:
    return "\n".join(
        f"[{transcript_svc.fmt_clock(s['start'] or 0)}] {s['speaker_name']}: "
        f"{(s['text'] or '').strip()}"
        for s in segments
    )


def _resolve_owner_speaker(value: str, known: set[str]) -> str:
    """Blank an owner the model invented, rather than storing a dangling id."""
    return value if value in known else ""


def generate_summary_sync(
    db_path,
    meeting_id: int,
    *,
    model: str | None = None,
    prompt_name: str | None = None,
    prompt_override: str | None = None,
    temperature: float | None = None,
    created_by: int | None = None,
) -> int:
    """Produce one new summary version. Blocking; call via asyncio.to_thread."""
    settings = get_settings()
    started = time.monotonic()

    with get_conn(db_path) as conn:
        transcript = transcript_svc.get_transcript(conn, meeting_id)
        diarization = transcript_svc.load_diarization(conn, meeting_id)
        config = llm_svc.LLMConfig.from_db(conn, model_override=model)
        chosen_prompt_name = (
            prompt_name or effective(conn, "summary_prompt_name")
        )

    if prompt_override:
        prompt = prompts_svc.load_override(chosen_prompt_name, prompt_override)
        prompt_version = "override"
    else:
        prompt = prompts_svc.load(chosen_prompt_name)
        prompt_version = prompt.version

    if temperature is not None:
        config.temperature = temperature
    elif prompt.temperature is not None:
        config.temperature = prompt.temperature

    segments = transcript["segments"]
    full_text = _render_segments(segments)
    fingerprint = transcript_svc.transcript_sha256(transcript)

    max_tokens_in = settings.summary_max_input_tokens
    mode = "single"
    raw_content = ""

    if llm_svc.estimate_tokens(full_text) <= max_tokens_in:
        values = build_prompt_values(
            _open(db_path), meeting_id, transcript, full_text
        )
        system, user = prompt.render(values)
        parsed, usage, raw_content = llm_svc.chat_json(config, system, user)
    else:
        # Map-reduce: summarise windows, then summarise the summaries.
        mode = "mapreduce"
        max_chars = int(max_tokens_in * 3.6 * 0.6)
        chunks = _chunk_segments(segments, max_chars)
        log.info("transcript too long; using map-reduce over %d chunks", len(chunks))

        partials = []
        total_usage = {"prompt_tokens": 0, "completion_tokens": 0}
        for i, chunk in enumerate(chunks, start=1):
            values = build_prompt_values(
                _open(db_path), meeting_id, transcript, _render_segments(chunk)
            )
            values["meeting_title"] = (
                f"{values['meeting_title']} (part {i} of {len(chunks)})"
            )
            system, user = prompt.render(values)
            part, usage, _ = llm_svc.chat_json(config, system, user)
            partials.append(part)
            for key in total_usage:
                total_usage[key] += usage.get(key, 0) or 0

        combined = "\n\n".join(
            f"--- Part {i} ---\n{json.dumps(p, ensure_ascii=False)}"
            for i, p in enumerate(partials, start=1)
        )
        values = build_prompt_values(_open(db_path), meeting_id, transcript, combined)
        system, user = prompt.render(values)
        parsed, usage, raw_content = llm_svc.chat_json(config, system, user)
        usage = {**total_usage}
        raw_content = json.dumps({"mode": "mapreduce", "chunks": len(chunks)})

    try:
        result = MeetingSummaryResult.model_validate(parsed)
    except PydanticValidationError as exc:
        raise LLMError(f"Summary did not match the expected shape: {exc}") from exc

    known_speakers = {s["id"] for s in transcript.get("speakers", [])}
    duration_sec = time.monotonic() - started

    with get_conn(db_path) as conn:
        version = (
            conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM summaries WHERE meeting_id = ?",
                (meeting_id,),
            ).fetchone()[0]
        )
        conn.execute(
            "UPDATE summaries SET is_current = 0 WHERE meeting_id = ?", (meeting_id,)
        )

        cur = conn.execute(
            """
            INSERT INTO summaries (meeting_id, version, is_current, status, model,
                llm_base_url, temperature, prompt_name, prompt_version, prompt_sha256,
                prompt_text, diarization_id, transcript_sha256, tldr, summary_md,
                title_suggestion, key_decisions_json, topics_json, open_questions_json,
                participants_json, raw_response, prompt_tokens, completion_tokens,
                duration_sec, created_by, created_at)
            VALUES (?, ?, 1, 'ok', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                meeting_id, version, config.model, config.base_url, config.temperature,
                prompt.name, prompt_version, prompt.sha256, prompt.body,
                diarization["id"], fingerprint,
                result.tldr, result.summary_md, result.title_suggestion,
                json.dumps([d.model_dump() for d in result.key_decisions]),
                json.dumps(result.topics),
                json.dumps(result.open_questions),
                json.dumps([p.model_dump() for p in result.participants]),
                raw_content[:100_000],
                usage.get("prompt_tokens"), usage.get("completion_tokens"),
                duration_sec, created_by, utcnow(),
            ),
        )
        summary_id = cur.lastrowid

        for idx, item in enumerate(result.action_items):
            if not item.text.strip():
                continue
            conn.execute(
                """
                INSERT INTO action_items (summary_id, meeting_id, idx, text, owner_label,
                    owner_speaker_id, due_text, due_date, priority, confidence,
                    status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (
                    summary_id, meeting_id, idx, item.text.strip(),
                    item.owner or None,
                    _resolve_owner_speaker(item.owner_speaker, known_speakers) or None,
                    item.due_text or None, item.due_date or None,
                    item.priority, item.confidence, utcnow(),
                ),
            )

        conn.execute(
            "UPDATE meetings SET active_summary_id = ?, updated_at = ? WHERE id = ?",
            (summary_id, utcnow(), meeting_id),
        )

        # Names the model heard in the transcript become greyed suggestions.
        suggested_any = False
        for participant in result.participants:
            if participant.speaker in known_speakers and participant.inferred_name:
                suggested_any = True
                conn.execute(
                    """
                    INSERT INTO speaker_map (meeting_id, speaker_id, display_name,
                                             source, updated_at)
                    VALUES (?, ?, ?, 'llm_suggested', ?)
                    ON CONFLICT(meeting_id, speaker_id) DO UPDATE SET
                        display_name = CASE
                            WHEN speaker_map.source = 'user' THEN speaker_map.display_name
                            ELSE excluded.display_name END,
                        source = CASE
                            WHEN speaker_map.source = 'user' THEN 'user'
                            ELSE 'llm_suggested' END,
                        updated_at = excluded.updated_at
                    """,
                    (meeting_id, participant.speaker, participant.inferred_name, utcnow()),
                )

        if suggested_any:
            # Applying our own suggestions changes the rendered transcript, so
            # re-fingerprint against the post-suggestion view. Otherwise every
            # summary would be flagged stale the instant it was written.
            refreshed = transcript_svc.transcript_sha256(
                transcript_svc.get_transcript(conn, meeting_id)
            )
            conn.execute(
                "UPDATE summaries SET transcript_sha256 = ? WHERE id = ?",
                (refreshed, summary_id),
            )

    log.info(
        "summary v%d for meeting %s (%s, %s mode, %.1fs)",
        version, meeting_id, config.model, mode, duration_sec,
    )
    return summary_id


def _open(db_path):
    """Short-lived connection for the prompt-value lookups."""
    from app.db import connect

    return connect(db_path)


async def summarize_meeting(ctx, meeting_id: int, **kwargs) -> int:
    return await asyncio.to_thread(
        generate_summary_sync, ctx.db_path, meeting_id, **kwargs
    )


# --------------------------------------------------------------------------- #
# Reading summaries back
# --------------------------------------------------------------------------- #


def row_to_summary(row: sqlite3.Row, action_items: list[sqlite3.Row] | None = None) -> dict:
    return {
        "id": row["id"],
        "meeting_id": row["meeting_id"],
        "version": row["version"],
        "is_current": bool(row["is_current"]),
        "status": row["status"],
        "model": row["model"],
        "llm_base_url": row["llm_base_url"],
        "temperature": row["temperature"],
        "prompt_name": row["prompt_name"],
        "prompt_version": row["prompt_version"],
        "prompt_sha256": row["prompt_sha256"],
        "tldr": row["tldr"],
        "summary_md": row["summary_md"],
        "title_suggestion": row["title_suggestion"],
        "topics": json.loads(row["topics_json"] or "[]"),
        "key_decisions": json.loads(row["key_decisions_json"] or "[]"),
        "open_questions": json.loads(row["open_questions_json"] or "[]"),
        "participants": json.loads(row["participants_json"] or "[]"),
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "duration_sec": row["duration_sec"],
        "transcript_sha256": row["transcript_sha256"],
        "error": row["error"],
        "created_at": row["created_at"],
        "action_items": [row_to_action_item(a) for a in (action_items or [])],
    }


def row_to_action_item(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "summary_id": row["summary_id"],
        "meeting_id": row["meeting_id"],
        "idx": row["idx"],
        "text": row["text"],
        "owner_label": row["owner_label"],
        "owner_speaker_id": row["owner_speaker_id"],
        "due_text": row["due_text"],
        "due_date": row["due_date"],
        "priority": row["priority"],
        "confidence": row["confidence"],
        "status": row["status"],
        "done_at": row["done_at"],
    }


def get_current_summary(conn: sqlite3.Connection, meeting_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM summaries WHERE meeting_id = ? AND is_current = 1",
        (meeting_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError("This meeting has no summary yet")

    items = conn.execute(
        "SELECT * FROM action_items WHERE summary_id = ? ORDER BY idx", (row["id"],)
    ).fetchall()
    summary = row_to_summary(row, items)

    # Tell the UI when renames or a re-diarization have moved on since this ran.
    try:
        current = transcript_svc.transcript_sha256(
            transcript_svc.get_transcript(conn, meeting_id)
        )
        summary["stale"] = current != row["transcript_sha256"]
    except NotFoundError:
        summary["stale"] = False

    return summary
