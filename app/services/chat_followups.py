"""Follow-up suggestion chips shown after an AI chat answer.

Ephemeral by design, unlike notes.generate_title_sync (persisted onto the
note) and next_step.py's cached, fingerprinted suggestion: a follow-up only
makes sense pinned to the one turn that just finished, so nothing is written
to the database -- reloading the page just loses them, and the next round
regenerates its own.
"""

from __future__ import annotations

from app.db import get_conn
from app.logging_config import get_logger
from app.services import llm as llm_svc
from app.services import prompts as prompts_svc

log = get_logger("chat_followups")

MAX_SUGGESTIONS = 3
SUGGESTION_MAX_LEN = 120


def generate_sync(
    db_path,
    *,
    question: str,
    answer: str,
    model: str | None = None,
) -> list[str]:
    """One LLM call for up to three follow-up chips.

    Blocking (an HTTP round trip), so the caller runs it off the event loop
    the same way notes.generate_title_sync is run. Never raises -- an empty
    list just means no chips show, the same failure tolerance as a note
    title falling back to derive_title.
    """
    try:
        with get_conn(db_path) as conn:
            config = llm_svc.LLMConfig.from_db(conn, model_override=model)

        prompt = prompts_svc.load("chat_followups_prompt")
        if prompt.temperature is not None:
            config.temperature = prompt.temperature

        system, user = prompt.render({"question": question, "answer": answer})
        parsed, _, _ = llm_svc.chat_json(config, system, user)
        suggestions = parsed.get("suggestions") or []
    except Exception as exc:
        log.warning("follow-up suggestion generation failed: %s", exc)
        return []

    cleaned = [s.strip()[:SUGGESTION_MAX_LEN] for s in suggestions if isinstance(s, str) and s.strip()]
    return cleaned[:MAX_SUGGESTIONS]
