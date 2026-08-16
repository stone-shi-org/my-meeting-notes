"""Live insights over an in-progress recording's rolling transcript.

Two meeting types, two prompts (see app/prompts/insights_*_prompt.md), one
call shape: hand back whatever the model returned last time plus the
transcript so far, get back the same shape grown by whatever's new.

Deliberately stateless on the server, the same reasoning as live captions:
there is often no meeting row yet (a recording exists before Stop; a meeting
only exists after), and holding server-side session state for something
this ephemeral is a cleanup problem for no benefit -- the browser already
holds the previous result in memory to hand back on the next call.
"""

from __future__ import annotations

import json
import sqlite3

from app.config import effective
from app.errors import ValidationError
from app.services import llm as llm_svc
from app.services import prompts as prompts_svc

# A live transcript only grows. Both prompts only need enough trailing
# context to place a new question or topic shift against what came just
# before it -- an unbounded transcript would eventually blow the model's
# context window and make calls slower the longer a session runs.
MAX_TRANSCRIPT_CHARS = 12_000

MAX_OUTPUT_TOKENS = 1500

_PROMPT_NAMES = {
    "interview": "insights_interview_prompt",
    "general": "insights_general_prompt",
}


def analyze(
    conn: sqlite3.Connection,
    meeting_type: str,
    transcript: str,
    previous: dict | None,
) -> dict:
    prompt_name = _PROMPT_NAMES.get(meeting_type)
    if prompt_name is None:
        raise ValidationError(f"Unknown meeting type: {meeting_type!r}")

    model = effective(conn, "insights_model")
    if not model:
        raise ValidationError(
            "No Insights model is configured. Set one in Settings -> LLM."
        )

    config = llm_svc.LLMConfig.from_db(conn, model_override=model)
    prompt = prompts_svc.load(prompt_name)
    if prompt.temperature is not None:
        config.temperature = prompt.temperature

    # Keep the tail: the most recent lines are what a new question or topic
    # shift is judged against, and previous_items/previous_topics already
    # carries forward everything durable from earlier in the session.
    trimmed = transcript[-MAX_TRANSCRIPT_CHARS:]

    if meeting_type == "interview":
        previous_items = (previous or {}).get("items") or []
        values = {
            "transcript": trimmed,
            "previous_items": json.dumps(previous_items, ensure_ascii=False),
        }
    else:
        previous_topics = (previous or {}).get("topics") or []
        values = {
            "transcript": trimmed,
            "previous_topics": json.dumps(previous_topics, ensure_ascii=False),
        }

    system, user = prompt.render(values)
    parsed, _usage, _raw = llm_svc.chat_json(
        config, system, user, max_tokens=MAX_OUTPUT_TOKENS
    )

    if meeting_type == "interview":
        items = parsed.get("items")
        return {"items": items if isinstance(items, list) else previous_items}

    topics = parsed.get("topics")
    return {"topics": topics if isinstance(topics, list) else previous_topics}
