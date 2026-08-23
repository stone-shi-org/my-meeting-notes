"""Live insights over an in-progress recording's rolling transcript.

Meeting types are rows in insight_types (see app/services/insight_types.py
and db.py's SCHEMA), each with its own prompt. Every type returns the same
combined shape -- a running topic list, a question/answer list, and an
action-item list, all three grown together in one call: hand back whatever
the model returned last time plus the transcript so far, get back the same
three lists grown by whatever's new.

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

# Three growing lists now share this budget instead of one -- topics used to
# be the whole reply; a long session's topics + questions (with discussion
# and now up to 5 detailed ai_answer_points each) + action_items needs more
# room to avoid truncating mid-JSON. Set generously (well past what a single
# tick's growth actually needs) so a long recording's accumulated lists never
# get cut off mid-JSON as they carry forward call after call.
MAX_OUTPUT_TOKENS = 8000


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

    # Keep the tail: the most recent lines are what a new question, topic
    # shift or action item is judged against, and the previous_* lists
    # already carry forward everything durable from earlier in the session.
    trimmed = transcript[-MAX_TRANSCRIPT_CHARS:]

    previous = previous or {}
    previous_topics = previous.get("topics") or []
    previous_questions = previous.get("questions") or []
    previous_action_items = previous.get("action_items") or []
    values = {
        "transcript": trimmed,
        "previous_topics": json.dumps(previous_topics, ensure_ascii=False),
        "previous_questions": json.dumps(previous_questions, ensure_ascii=False),
        "previous_action_items": json.dumps(previous_action_items, ensure_ascii=False),
    }

    system, user = prompt.render(values)
    parsed, _usage, _raw = llm_svc.chat_json(
        config, system, user, max_tokens=MAX_OUTPUT_TOKENS
    )

    topics = parsed.get("topics")
    questions = parsed.get("questions")
    action_items = parsed.get("action_items")
    return {
        "topics": topics if isinstance(topics, list) else previous_topics,
        "questions": questions if isinstance(questions, list) else previous_questions,
        "action_items": (
            action_items if isinstance(action_items, list) else previous_action_items
        ),
    }
