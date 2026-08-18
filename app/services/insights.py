"""Live insights over an in-progress recording's rolling transcript.

Meeting types are rows in insight_types (see app/services/insight_types.py
and db.py's SCHEMA), each with its own prompt and output ``kind`` -- a
running topic list ('topics') or a question/answer list ('questions'). One
call shape either way: hand back whatever the model returned last time plus
the transcript so far, get back the same shape grown by whatever's new.

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
from app.services import insight_types as insight_types_svc
from app.services import llm as llm_svc
from app.services import prompts as prompts_svc

# A live transcript only grows. Every prompt only needs enough trailing
# context to place a new question or topic shift against what came just
# before it -- an unbounded transcript would eventually blow the model's
# context window and make calls slower the longer a session runs.
MAX_TRANSCRIPT_CHARS = 12_000

MAX_OUTPUT_TOKENS = 1500


def analyze(
    conn: sqlite3.Connection,
    meeting_type: str,
    transcript: str,
    previous: dict | None,
) -> dict:
    # meeting_type is an insight_types.slug -- get_type raises NotFoundError
    # for an unknown one (404, not 400: same "doesn't exist" convention as
    # every other lookup-by-id in this app).
    type_row = insight_types_svc.get_type(conn, meeting_type)
    kind = type_row["kind"]

    model = effective(conn, "insights_model")
    if not model:
        raise ValidationError(
            "No Insights model is configured. Set one in Settings -> LLM."
        )

    config = llm_svc.LLMConfig.from_db(conn, model_override=model)
    # load_override, not load(name): the prompt text is this row's own
    # `prompt` column, not a file on disk -- see insight_types_svc's module
    # docstring for why that table exists at all.
    prompt = prompts_svc.load_override(meeting_type, type_row["prompt"])
    if prompt.temperature is not None:
        config.temperature = prompt.temperature

    # Keep the tail: the most recent lines are what a new question or topic
    # shift is judged against, and previous_items/previous_topics already
    # carries forward everything durable from earlier in the session.
    trimmed = transcript[-MAX_TRANSCRIPT_CHARS:]

    if kind == "questions":
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

    if kind == "questions":
        items = parsed.get("items")
        return {"items": items if isinstance(items, list) else previous_items}

    topics = parsed.get("topics")
    return {"topics": topics if isinstance(topics, list) else previous_topics}
