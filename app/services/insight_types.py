"""Meeting/insight types -- a name, an output shape, and a prompt, stored in
the ``insight_types`` table (see db.py's SCHEMA and its two seeded rows).

Replaces what used to be a hardcoded 'interview'/'general' enum with two
fixed prompt files: this list is admin-extensible at runtime from Settings
-> Meeting types (see app/routers/insight_types.py), and every row -- built
-in or added later -- carries its own prompt instead of pointing at a file.

``kind`` decides what services/insights.analyze expects the model to return
and carry forward across calls, and what InsightsPanel renders:
  - 'topics'    -- a running topic list (previous_topics / topics)
  - 'questions' -- a question/answer list (previous_items / items)
There is no third shape; every type picks one of these two at creation time.
"""

from __future__ import annotations

import re
import sqlite3

from app.db import utcnow
from app.errors import NotFoundError, ValidationError
from app.services import prompts as prompts_svc

KINDS = ("topics", "questions")

# What services/insights.py's prompt needs besides {{transcript}}, depending
# on which list it's growing across calls.
_REQUIRED_PLACEHOLDER_BY_KIND = {
    "topics": "previous_topics",
    "questions": "previous_items",
}


def _slugify(name: str) -> str:
    # Same recipe as integrations.py's account_key-from-label: a slug is only
    # ever derived once, at create time, and never rewritten -- see
    # create_type and the doc comment on _unique_slug below.
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug or "type"


def _unique_slug(conn: sqlite3.Connection, name: str) -> str:
    """First-come slug, deduped with a numeric suffix -- two types named
    "Standup" is a legitimate thing to want (e.g. two teams), and the slug
    only has to be stable and unique, never pretty."""
    base = _slugify(name)
    slug = base
    n = 2
    while conn.execute("SELECT 1 FROM insight_types WHERE slug = ?", (slug,)).fetchone():
        slug = f"{base}-{n}"
        n += 1
    return slug


def _validate(kind: str, prompt: str) -> None:
    if kind not in KINDS:
        raise ValidationError(f"Unknown kind {kind!r}; must be one of {', '.join(KINDS)}")
    if not (prompt or "").strip():
        raise ValidationError("A prompt is required")

    # Parsing (not loading from disk -- load_override just wraps raw text
    # the same way parse() would) is enough to reject malformed frontmatter
    # early, same as prompts_svc.save() does for the file-based prompts.
    prompts_svc.load_override("insight_type", prompt)

    present = prompts_svc.find_placeholders(prompt)
    required = {"transcript", _REQUIRED_PLACEHOLDER_BY_KIND[kind]}
    missing = required - present
    if missing:
        raise ValidationError(
            "Prompt is missing required placeholder(s): "
            + ", ".join("{{%s}}" % m for m in sorted(missing))
        )


def list_types(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM insight_types ORDER BY sort_order, id").fetchall()


def get_type(conn: sqlite3.Connection, slug: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM insight_types WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown meeting type: {slug!r}")
    return row


def create_type(conn: sqlite3.Connection, *, name: str, kind: str, prompt: str) -> sqlite3.Row:
    name = (name or "").strip()
    if not name:
        raise ValidationError("A name is required")
    _validate(kind, prompt)

    slug = _unique_slug(conn, name)
    now = utcnow()
    next_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM insight_types"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO insight_types (slug, name, kind, prompt, sort_order, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (slug, name, kind, prompt, next_order, now, now),
    )
    return get_type(conn, slug)


def update_type(
    conn: sqlite3.Connection,
    slug: str,
    *,
    name: str | None = None,
    kind: str | None = None,
    prompt: str | None = None,
) -> sqlite3.Row:
    """Edits in place -- the slug never changes once created (see module
    docstring), so a rename cannot orphan whatever referenced it earlier in
    a still-running recording."""
    row = get_type(conn, slug)
    next_name = row["name"] if name is None else name.strip()
    next_kind = row["kind"] if kind is None else kind
    next_prompt = row["prompt"] if prompt is None else prompt
    if not next_name:
        raise ValidationError("A name is required")
    _validate(next_kind, next_prompt)

    conn.execute(
        "UPDATE insight_types SET name = ?, kind = ?, prompt = ?, updated_at = ? WHERE slug = ?",
        (next_name, next_kind, next_prompt, utcnow(), slug),
    )
    return get_type(conn, slug)


def delete_type(conn: sqlite3.Connection, slug: str) -> None:
    get_type(conn, slug)  # 404s on an unknown slug rather than a silent no-op
    conn.execute("DELETE FROM insight_types WHERE slug = ?", (slug,))
