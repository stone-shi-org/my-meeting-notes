"""Notes: the one kind of attached document this app writes itself.

Emails and calendar events are fetched from a provider and attached; a note is
authored — either typed by hand or saved out of an AI chat reply. Everything
else about it behaves like the other two attachments: it hangs off a thread,
optionally off one meeting within it, and it shows up on the timeline.

Two deliberate differences from ``matching.attach_*``:

**A note is never unread.** ``auto_attached``/``seen_at`` exist because the
sweep attaches things while nobody is looking. Every note is created by a
person pressing a button, so there is nothing to mark.

**A note is not summarizer input.** ``matching.attached_context`` deliberately
does not read this table. An AI-written note that fed the next summary of the
meeting it was written from would put the model's own prose back into its
input; notes reach the model through the thread chat digest and the next-step
payload instead, where they are clearly the user's working material rather than
ground truth about what was said.
"""

from __future__ import annotations

import re
import sqlite3

from app.db import get_conn, utcnow
from app.errors import NotFoundError
from app.logging_config import get_logger
from app.services import llm as llm_svc
from app.services import prompts as prompts_svc

log = get_logger("notes")

TITLE_MAX = 120
SOURCES = ("ai_chat", "manual")

# How a saved chat reply is joined onto a note that already has content. A rule
# rather than a blank line: the appended block is a separate answer to a
# separate question, and the note is rendered as markdown.
APPEND_SEPARATOR = "\n\n---\n\n"

# Leading markdown that would otherwise end up inside a derived title.
_TITLE_NOISE = re.compile(r"^\s*(?:[#>\-*+]+|\d+[.)])\s*")
_EMPHASIS = re.compile(r"[*_`]")


def derive_title(body: str) -> str:
    """A title from the note's own first line, for when the LLM can't be asked.

    Used both as the fallback when title generation fails and as the title of a
    hand-written note the user left untitled. Never raises and never returns
    empty: losing the body because nothing could name it would be the worst
    possible outcome of pressing "save this answer".
    """
    for line in (body or "").splitlines():
        cleaned = _EMPHASIS.sub("", _TITLE_NOISE.sub("", line)).strip()
        if cleaned:
            if len(cleaned) > TITLE_MAX:
                return cleaned[: TITLE_MAX - 1].rstrip() + "…"
            return cleaned
    return "Untitled note"


def generate_title_sync(
    db_path,
    *,
    body: str,
    question: str | None = None,
    context_label: str = "",
    model: str | None = None,
) -> tuple[str, str | None]:
    """Name a note with one LLM call. Returns ``(title, title_model)``.

    Blocking (an HTTP round trip), so the caller runs it off the event loop the
    same way ``next_step.generate_sync`` is run. Failure is not an error: the
    note still gets saved under :func:`derive_title`, with ``title_model`` NULL
    to record that nothing generated it.
    """
    try:
        with get_conn(db_path) as conn:
            config = llm_svc.LLMConfig.from_db(conn, model_override=model)

        prompt = prompts_svc.load("note_title_prompt")
        if prompt.temperature is not None:
            config.temperature = prompt.temperature

        system, user = prompt.render(
            {
                "note_body": body,
                "question": question or "(not recorded)",
                "context_label": context_label or "(none)",
            }
        )
        parsed, _, _ = llm_svc.chat_json(config, system, user)
        title = (parsed.get("title") or "").strip()
        if not title:
            raise llm_svc.LLMError("Model returned an empty title")
    except Exception as exc:
        log.warning("note title generation failed, falling back to the first line: %s", exc)
        return derive_title(body), None

    return title[:TITLE_MAX], config.model


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def row_to_note(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "thread_id": row["thread_id"],
        "meeting_id": row["meeting_id"],
        "title": row["title"],
        "body": row["body"],
        "source": row["source"],
        "model": row["model"],
        "title_model": row["title_model"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_note(
    conn: sqlite3.Connection,
    *,
    thread_id: int,
    meeting_id: int | None,
    title: str,
    body: str,
    source: str,
    user_id: int,
    model: str | None = None,
    title_model: str | None = None,
) -> dict:
    now = utcnow()
    cur = conn.execute(
        """
        INSERT INTO thread_notes (thread_id, meeting_id, title, body, source, model,
                                  title_model, created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (thread_id, meeting_id, title, body, source, model, title_model, user_id, now, now),
    )
    return require_note(conn, thread_id, cur.lastrowid)  # type: ignore[arg-type]


def get_note(conn: sqlite3.Connection, thread_id: int, note_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM thread_notes WHERE id = ? AND thread_id = ?", (note_id, thread_id)
    ).fetchone()


def require_note(conn: sqlite3.Connection, thread_id: int, note_id: int) -> dict:
    row = get_note(conn, thread_id, note_id)
    if row is None:
        raise NotFoundError("Note not found on this thread")
    return row_to_note(row)


def list_notes(
    conn: sqlite3.Connection, *, thread_id: int, meeting_id: int | None = None
) -> list[dict]:
    """Newest first — a note list is a working document, not a chronology, so
    the thing just saved belongs at the top rather than buried under a year of
    older ones. The timeline re-sorts them by date alongside everything else."""
    sql = "SELECT * FROM thread_notes WHERE thread_id = ?"
    params: list = [thread_id]
    if meeting_id is not None:
        sql += " AND meeting_id = ?"
        params.append(meeting_id)
    sql += " ORDER BY created_at DESC, id DESC"
    return [row_to_note(r) for r in conn.execute(sql, params).fetchall()]


def update_note(
    conn: sqlite3.Connection,
    *,
    thread_id: int,
    note_id: int,
    title: str | None = None,
    body: str | None = None,
) -> dict:
    require_note(conn, thread_id, note_id)

    updates: dict = {}
    if title is not None:
        updates["title"] = title
    if body is not None:
        updates["body"] = body

    if updates:
        updates["updated_at"] = utcnow()
        assignments = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE thread_notes SET {assignments} WHERE id = ? AND thread_id = ?",
            [*updates.values(), note_id, thread_id],
        )

    return require_note(conn, thread_id, note_id)


def append_to_note(conn: sqlite3.Connection, *, thread_id: int, note_id: int, body: str) -> dict:
    """Add another answer to the end of an existing note.

    Separate from :func:`update_note` because it is a different operation: the
    client sends only what is being added and never has to hold — or race with
    another tab over — the note's current text.
    """
    note = require_note(conn, thread_id, note_id)
    combined = (note["body"] + APPEND_SEPARATOR + body) if note["body"].strip() else body
    conn.execute(
        "UPDATE thread_notes SET body = ?, updated_at = ? WHERE id = ? AND thread_id = ?",
        (combined, utcnow(), note_id, thread_id),
    )
    return require_note(conn, thread_id, note_id)


def delete_note(conn: sqlite3.Connection, *, thread_id: int, note_id: int) -> None:
    cur = conn.execute(
        "DELETE FROM thread_notes WHERE id = ? AND thread_id = ?", (note_id, thread_id)
    )
    if cur.rowcount == 0:
        raise NotFoundError("Note not found on this thread")
