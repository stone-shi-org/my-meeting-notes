"""The search index: what is searchable, and keeping it in step with the source.

MMN-15. Two tables hold the keyword index -- ``search_fts`` (FTS5, the text)
and its plain twin ``search_docs`` (same id, the B-tree-indexable placement
and a per-row fingerprint). Everything here only ever *reads* the source
tables; nothing writes ``raw_json``, ``updated_at`` or ``seen_at``, and no
email body is fetched -- an index of what is already stored, nothing more.

Work is grouped into **scopes** rather than tracked per row:

* ``t:<thread_id>`` -- the thread itself, its notes, emails and events;
* ``m:<meeting_id>`` -- the meeting, its active transcript's segments, its
  current summary and that summary's action items.

A write path only has to name the scope it touched (``mark_thread`` /
``mark_meeting``) -- one INSERT into ``search_dirty`` inside the caller's own
transaction -- and the background indexer re-renders that scope, diffs it
against ``search_docs`` by fingerprint, and writes only what changed. That is
what keeps ~30 call sites to one line each, and what makes an UPSERT whose row
id the caller never learns (``attach_email``'s ON CONFLICT) indexable at all.

Two exceptions are synchronous, on purpose:

* **Deletes** (``delete_thread_scope`` / ``delete_meeting_scope`` /
  ``delete_doc``). Cascades never reach a virtual table, and a deleted note
  must stop matching now, not in a second.
* **The thread doc itself** (``index_thread_doc``), because the home list's
  "Search threads..." filter runs on it, and that is the one search people
  type into straight after naming a thread.

The safety net is :func:`reconcile`: a cheap SQL fingerprint of every scope's
source rows compared against ``search_scopes.source_fp``. It runs at startup,
on Rebuild, and every ``search_reconcile_interval_minutes``, so a write path
that forgets to mark its scope leaves the index stale for at most that long --
never wrong about ownership, because ownership is read from the source rows
every time a scope is rendered.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from typing import Callable, Iterable

from app.db import utcnow
from app.logging_config import get_logger
from app.services import html_text
from app.services import transcript as transcript_svc

log = get_logger("search_index")

# Bump to re-render every document on the next reconcile -- the renderer's
# output is part of every fingerprint, so changing what gets indexed without
# bumping this would leave old rows looking current.
RENDER_VERSION = "1"

KINDS: tuple[str, ...] = (
    "thread", "meeting", "segment", "summary", "action_item", "note", "email", "event",
)
THREAD_KINDS: tuple[str, ...] = ("thread", "note", "email", "event")
MEETING_KINDS: tuple[str, ...] = ("meeting", "segment", "summary", "action_item")


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Doc:
    kind: str
    ref_id: str
    owner_id: int
    thread_id: int | None
    meeting_id: int | None
    start_sec: float | None
    title: str  # searchable, bm25-weighted above body
    body: str
    label: str  # what a result card shows as its heading
    date: str | None

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            [
                RENDER_VERSION, self.kind, self.ref_id, self.owner_id, self.thread_id,
                self.meeting_id, self.start_sec, self.title, self.body, self.label, self.date,
            ],
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def thread_scope(thread_id: int) -> str:
    return f"t:{thread_id}"


def meeting_scope(meeting_id: int) -> str:
    return f"m:{meeting_id}"


def parse_scope(scope_key: str) -> tuple[str, int]:
    prefix, _, raw = scope_key.partition(":")
    return prefix, int(raw)


def _clean(text: str | None) -> str:
    return (text or "").strip()


def _join(*parts: str | None) -> str:
    return "\n".join(p.strip() for p in parts if p and p.strip())


def _flatten(value) -> list[str]:
    """Every string inside a decoded summary JSON field, in order.

    Decisions are dicts (text, rationale, owner...), topics and questions are
    usually bare strings but an insight type can shape them otherwise -- so
    walk whatever is there rather than hardcoding one shape and silently
    indexing nothing when it changes.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        out: list[str] = []
        for v in value.values():
            out.extend(_flatten(v))
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for v in value:
            out.extend(_flatten(v))
        return out
    return []


def _json_texts(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        return _flatten(json.loads(raw))
    except (ValueError, TypeError):
        return []


def _email_body_text(body: str | None) -> str:
    """Bodies are already converted to text on the way in (html_text), but a
    row stored before that conversion existed may still hold markup."""
    if not body:
        return ""
    try:
        return html_text.to_plain_text(body)
    except Exception:  # pragma: no cover - a bad body must not stop indexing
        return body


# ------------------------------------------------------------ thread scope


def _thread_doc(row: sqlite3.Row) -> Doc:
    return Doc(
        kind="thread",
        ref_id=str(row["id"]),
        owner_id=row["owner_id"],
        thread_id=row["id"],
        meeting_id=None,
        start_sec=None,
        title=_clean(row["title"]),
        body=_clean(row["description"]),
        label=_clean(row["title"]) or "Untitled thread",
        date=row["updated_at"],
    )


def render_thread_scope(conn: sqlite3.Connection, thread_id: int) -> list[Doc] | None:
    """None when the thread no longer exists -- the scope is to be dropped."""
    thread = conn.execute(
        "SELECT id, owner_id, title, description, updated_at FROM threads WHERE id = ?",
        (thread_id,),
    ).fetchone()
    if thread is None:
        return None
    owner_id = thread["owner_id"]
    docs = [_thread_doc(thread)]

    for n in conn.execute(
        "SELECT id, meeting_id, title, body, updated_at FROM thread_notes WHERE thread_id = ?",
        (thread_id,),
    ):
        docs.append(
            Doc(
                kind="note", ref_id=str(n["id"]), owner_id=owner_id, thread_id=thread_id,
                meeting_id=n["meeting_id"], start_sec=None,
                title=_clean(n["title"]), body=_clean(n["body"]),
                label=_clean(n["title"]) or "Untitled note", date=n["updated_at"],
            )
        )

    for e in conn.execute(
        "SELECT id, meeting_id, sender, subject, snippet, ai_summary, body, date, attached_at "
        "FROM thread_emails WHERE thread_id = ?",
        (thread_id,),
    ):
        docs.append(
            Doc(
                kind="email", ref_id=str(e["id"]), owner_id=owner_id, thread_id=thread_id,
                meeting_id=e["meeting_id"], start_sec=None,
                title=_clean(e["subject"]),
                # Only the body that is already stored: indexing never hydrates.
                body=_join(e["sender"], e["snippet"], e["ai_summary"], _email_body_text(e["body"])),
                label=_clean(e["subject"]) or "(no subject)",
                date=e["date"] or e["attached_at"],
            )
        )

    for c in conn.execute(
        "SELECT id, meeting_id, summary, description, location, start_at, attached_at "
        "FROM thread_calendar_events WHERE thread_id = ?",
        (thread_id,),
    ):
        docs.append(
            Doc(
                kind="event", ref_id=str(c["id"]), owner_id=owner_id, thread_id=thread_id,
                meeting_id=c["meeting_id"], start_sec=None,
                title=_clean(c["summary"]), body=_join(c["description"], c["location"]),
                label=_clean(c["summary"]) or "Untitled event",
                date=c["start_at"] or c["attached_at"],
            )
        )
    return docs


# ----------------------------------------------------------- meeting scope


def render_meeting_scope(conn: sqlite3.Connection, meeting_id: int) -> list[Doc] | None:
    meeting = conn.execute(
        "SELECT id, owner_id, thread_id, title, meeting_at, created_at, "
        "active_diarization_id, active_summary_id FROM meetings WHERE id = ?",
        (meeting_id,),
    ).fetchone()
    if meeting is None:
        return None
    owner_id = meeting["owner_id"]
    thread_id = meeting["thread_id"]
    meeting_title = _clean(meeting["title"])
    meeting_date = meeting["meeting_at"] or meeting["created_at"]

    docs = [
        Doc(
            kind="meeting", ref_id=str(meeting_id), owner_id=owner_id, thread_id=thread_id,
            meeting_id=meeting_id, start_sec=None, title=meeting_title, body="",
            label=meeting_title or "Untitled meeting", date=meeting_date,
        )
    ]

    diar_id = meeting["active_diarization_id"]
    if diar_id is not None:
        diar = conn.execute(
            "SELECT raw_json FROM diarizations WHERE id = ?", (diar_id,)
        ).fetchone()
        if diar is not None:
            try:
                payload = json.loads(diar["raw_json"])
            except ValueError:
                payload = {}
            # The same renderer the transcript page uses, so a speaker's name
            # here can never drift from the one on screen. Read-only: raw_json
            # is never written back, renames live in speaker_map.
            mapping = transcript_svc.load_speaker_map(conn, meeting_id)
            rendered = transcript_svc.build_transcript(payload, mapping, include_nonspeech=True)
            for idx, seg in enumerate(rendered["segments"]):
                text = _clean(seg.get("text"))
                if seg.get("non_speech") or not text:
                    continue
                start = seg.get("start")
                speaker = _clean(seg.get("speaker_name")) or _clean(seg.get("speaker"))
                docs.append(
                    Doc(
                        kind="segment", ref_id=f"{diar_id}:{idx}", owner_id=owner_id,
                        thread_id=thread_id, meeting_id=meeting_id,
                        start_sec=float(start) if start is not None else None,
                        title=speaker, body=text,
                        label=f"{speaker or 'Speaker'} @ {transcript_svc.fmt_clock(start or 0)}",
                        date=meeting_date,
                    )
                )

    summary_id = meeting["active_summary_id"]
    if summary_id is not None:
        s = conn.execute(
            "SELECT id, tldr, summary_md, title_suggestion, key_decisions_json, topics_json, "
            "open_questions_json, created_at FROM summaries WHERE id = ?",
            (summary_id,),
        ).fetchone()
        if s is not None:
            docs.append(
                Doc(
                    kind="summary", ref_id=str(s["id"]), owner_id=owner_id,
                    thread_id=thread_id, meeting_id=meeting_id, start_sec=None,
                    title=_clean(s["title_suggestion"]) or meeting_title,
                    body=_join(
                        s["tldr"], s["summary_md"],
                        *_json_texts(s["key_decisions_json"]),
                        *_json_texts(s["topics_json"]),
                        *_json_texts(s["open_questions_json"]),
                    ),
                    label=f"Summary · {meeting_title or 'Untitled meeting'}",
                    date=s["created_at"],
                )
            )
            for a in conn.execute(
                "SELECT id, text, owner_label, due_text, created_at FROM action_items "
                "WHERE summary_id = ? ORDER BY idx",
                (summary_id,),
            ):
                text = _clean(a["text"])
                if not text:
                    continue
                docs.append(
                    Doc(
                        kind="action_item", ref_id=str(a["id"]), owner_id=owner_id,
                        thread_id=thread_id, meeting_id=meeting_id, start_sec=None,
                        title="", body=_join(text, a["owner_label"], a["due_text"]),
                        label=text if len(text) <= 120 else text[:117] + "…",
                        date=a["created_at"],
                    )
                )
    return docs


def render_scope(conn: sqlite3.Connection, scope_key: str) -> list[Doc] | None:
    prefix, ident = parse_scope(scope_key)
    if prefix == "t":
        return render_thread_scope(conn, ident)
    if prefix == "m":
        return render_meeting_scope(conn, ident)
    raise ValueError(f"unknown search scope {scope_key!r}")


# --------------------------------------------------------------------------- #
# Source fingerprints (the reconcile's cheap "did anything change?")
# --------------------------------------------------------------------------- #

# Every column a renderer reads, plus updated_at as a catch-all. Deliberately
# not the email body/summary text themselves -- their length plus
# body_fetched_at/ai_summary_model is enough to notice a write, and hashing
# every body on every reconcile would be the expensive half of the job.
_THREAD_FP_SQL = """
SELECT t.id AS id, t.owner_id AS owner_id,
       t.owner_id || '|' || t.title || '|' || COALESCE(t.description, '') || '|' || t.updated_at
       || '|n:' || COALESCE((SELECT group_concat(tok, ',') FROM (
              SELECT id || ':' || COALESCE(meeting_id, '') || ':' || updated_at
                     || ':' || length(title) || ':' || length(body) AS tok
                FROM thread_notes WHERE thread_id = t.id ORDER BY id)), '')
       || '|e:' || COALESCE((SELECT group_concat(tok, ',') FROM (
              SELECT id || ':' || COALESCE(meeting_id, '') || ':' || COALESCE(subject, '')
                     || ':' || COALESCE(date, '') || ':' || COALESCE(body_fetched_at, '')
                     || ':' || length(COALESCE(body, '')) || ':' || length(COALESCE(snippet, ''))
                     || ':' || length(COALESCE(ai_summary, ''))
                     || ':' || COALESCE(ai_summary_model, '') AS tok
                FROM thread_emails WHERE thread_id = t.id ORDER BY id)), '')
       || '|c:' || COALESCE((SELECT group_concat(tok, ',') FROM (
              SELECT id || ':' || COALESCE(meeting_id, '') || ':' || attached_at
                     || ':' || COALESCE(summary, '') AS tok
                FROM thread_calendar_events WHERE thread_id = t.id ORDER BY id)), '')
       AS raw
  FROM threads t
"""

_MEETING_FP_SQL = """
SELECT m.id AS id, m.owner_id AS owner_id,
       m.owner_id || '|' || m.thread_id || '|' || m.title || '|' || COALESCE(m.meeting_at, '')
       || '|' || m.updated_at || '|' || COALESCE(m.active_diarization_id, '')
       || '|' || COALESCE(m.active_summary_id, '')
       || '|s:' || COALESCE((SELECT group_concat(tok, ',') FROM (
              SELECT speaker_id || ':' || COALESCE(display_name, '') || ':'
                     || COALESCE(merged_into, '') || ':' || COALESCE(source, '')
                     || ':' || COALESCE(updated_at, '') AS tok
                FROM speaker_map WHERE meeting_id = m.id ORDER BY speaker_id)), '')
       || '|a:' || COALESCE((SELECT group_concat(tok, ',') FROM (
              SELECT id || ':' || length(text) || ':' || COALESCE(owner_label, '')
                     || ':' || COALESCE(due_text, '') AS tok
                FROM action_items WHERE summary_id = m.active_summary_id ORDER BY id)), '')
       AS raw
  FROM meetings m
"""


def _fp(raw: str) -> str:
    return hashlib.sha256(f"{RENDER_VERSION}|{raw}".encode("utf-8")).hexdigest()


def source_fingerprint(conn: sqlite3.Connection, scope_key: str) -> tuple[int, str] | None:
    """``(owner_id, fingerprint)`` of one scope's source rows, or None if gone."""
    prefix, ident = parse_scope(scope_key)
    sql = _THREAD_FP_SQL + " WHERE t.id = ?" if prefix == "t" else _MEETING_FP_SQL + " WHERE m.id = ?"
    row = conn.execute(sql, (ident,)).fetchone()
    if row is None:
        return None
    return row["owner_id"], _fp(row["raw"] or "")


def all_source_fingerprints(conn: sqlite3.Connection) -> dict[str, tuple[int, str]]:
    out: dict[str, tuple[int, str]] = {}
    for row in conn.execute(_THREAD_FP_SQL):
        out[thread_scope(row["id"])] = (row["owner_id"], _fp(row["raw"] or ""))
    for row in conn.execute(_MEETING_FP_SQL):
        out[meeting_scope(row["id"])] = (row["owner_id"], _fp(row["raw"] or ""))
    return out


# --------------------------------------------------------------------------- #
# Marking dirty (called from every write path, inside its transaction)
# --------------------------------------------------------------------------- #

_wake_lock = threading.Lock()
_wake: Callable[[], None] | None = None


def set_waker(fn: Callable[[], None] | None) -> None:
    """The indexer registers a thread-safe "work is waiting" callback here."""
    global _wake
    with _wake_lock:
        _wake = fn


def _notify() -> None:
    with _wake_lock:
        fn = _wake
    if fn is not None:
        try:
            fn()
        except Exception:  # pragma: no cover - a nudge must never fail a write
            log.debug("search indexer nudge failed", exc_info=True)


def mark_dirty(conn: sqlite3.Connection, scope_keys: Iterable[str]) -> None:
    now = utcnow()
    keys = list(dict.fromkeys(scope_keys))
    if not keys:
        return
    conn.executemany(
        "INSERT INTO search_dirty (scope_key, queued_at) VALUES (?, ?) "
        "ON CONFLICT(scope_key) DO UPDATE SET queued_at = excluded.queued_at",
        [(k, now) for k in keys],
    )
    _notify()


def mark_thread(conn: sqlite3.Connection, *thread_ids: int | None) -> None:
    mark_dirty(conn, (thread_scope(t) for t in thread_ids if t is not None))


def mark_meeting(conn: sqlite3.Connection, *meeting_ids: int | None) -> None:
    mark_dirty(conn, (meeting_scope(m) for m in meeting_ids if m is not None))


def mark_email_thread(conn: sqlite3.Connection, email_row_id: int) -> None:
    """For writes that only know the email's row id (hydration)."""
    row = conn.execute(
        "SELECT thread_id FROM thread_emails WHERE id = ?", (email_row_id,)
    ).fetchone()
    if row is not None:
        mark_thread(conn, row["thread_id"])


# --------------------------------------------------------------------------- #
# Writing documents
# --------------------------------------------------------------------------- #


def _delete_fts_where(conn: sqlite3.Connection, where: str, params: tuple) -> int:
    ids = [r[0] for r in conn.execute(f"SELECT id FROM search_docs WHERE {where}", params)]
    if not ids:
        return 0
    conn.executemany("DELETE FROM search_fts WHERE rowid = ?", [(i,) for i in ids])
    conn.executemany("DELETE FROM search_docs WHERE id = ?", [(i,) for i in ids])
    return len(ids)


def upsert_doc(conn: sqlite3.Connection, doc: Doc, scope_key: str) -> None:
    """Write one document to both tables. Keyed on (kind, ref_id), so a note
    that moved threads is rewritten in place under its new scope."""
    now = utcnow()
    existing = conn.execute(
        "SELECT id FROM search_docs WHERE kind = ? AND ref_id = ?", (doc.kind, doc.ref_id)
    ).fetchone()
    values = (
        scope_key, doc.owner_id, doc.thread_id, doc.meeting_id, doc.start_sec,
        doc.label, doc.date, doc.fingerprint, now,
    )
    if existing is None:
        cur = conn.execute(
            "INSERT INTO search_docs (scope_key, owner_id, thread_id, meeting_id, start_sec, "
            "label, date, fingerprint, indexed_at, kind, ref_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*values, doc.kind, doc.ref_id),
        )
        doc_id = cur.lastrowid
    else:
        doc_id = existing["id"]
        conn.execute(
            "UPDATE search_docs SET scope_key = ?, owner_id = ?, thread_id = ?, meeting_id = ?, "
            "start_sec = ?, label = ?, date = ?, fingerprint = ?, indexed_at = ? WHERE id = ?",
            (*values, doc_id),
        )
        conn.execute("DELETE FROM search_fts WHERE rowid = ?", (doc_id,))
    conn.execute(
        "INSERT INTO search_fts (rowid, kind, ref_id, owner_id, thread_id, meeting_id, "
        "start_sec, title, body) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            doc_id, doc.kind, doc.ref_id, doc.owner_id, doc.thread_id, doc.meeting_id,
            doc.start_sec, doc.title, doc.body,
        ),
    )


def delete_doc(conn: sqlite3.Connection, kind: str, ref_id: str | int) -> None:
    """Synchronous single-document delete (detach, note delete)."""
    ref = str(ref_id)
    _delete_fts_where(conn, "kind = ? AND ref_id = ?", (kind, ref))
    conn.execute(
        "DELETE FROM search_embeddings WHERE kind = ? AND doc_ref = ?", (kind, ref)
    )


def _drop_scope(conn: sqlite3.Connection, scope_key: str) -> None:
    _delete_fts_where(conn, "scope_key = ?", (scope_key,))
    conn.execute("DELETE FROM search_embeddings WHERE scope_key = ?", (scope_key,))
    conn.execute("DELETE FROM search_scopes WHERE scope_key = ?", (scope_key,))
    conn.execute("DELETE FROM search_dirty WHERE scope_key = ?", (scope_key,))


def delete_thread_scope(conn: sqlite3.Connection, thread_id: int) -> None:
    """Call right before ``DELETE FROM threads``: drops the thread's scope and
    the scope of every meeting on it (their docs all carry this thread_id)."""
    meeting_ids = [
        r[0] for r in conn.execute("SELECT id FROM meetings WHERE thread_id = ?", (thread_id,))
    ]
    _drop_scope(conn, thread_scope(thread_id))
    for mid in meeting_ids:
        _drop_scope(conn, meeting_scope(mid))
    # Anything else still pointing at the thread (a doc whose scope moved
    # mid-flight) goes too -- the thread is about to not exist.
    _delete_fts_where(conn, "thread_id = ?", (thread_id,))
    conn.execute("DELETE FROM search_embeddings WHERE thread_id = ?", (thread_id,))


def delete_meeting_scope(conn: sqlite3.Connection, meeting_id: int) -> None:
    """Call right before ``DELETE FROM meetings``. The thread's notes, emails
    and events lose their meeting_id by ON DELETE SET NULL, so the thread
    scope is marked for a re-render too."""
    row = conn.execute("SELECT thread_id FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    _drop_scope(conn, meeting_scope(meeting_id))
    if row is not None:
        mark_thread(conn, row["thread_id"])


def index_thread_doc(conn: sqlite3.Connection, thread_id: int) -> None:
    """Synchronously (re)index just the thread document -- see module doc."""
    row = conn.execute(
        "SELECT id, owner_id, title, description, updated_at FROM threads WHERE id = ?",
        (thread_id,),
    ).fetchone()
    if row is None:
        return
    doc = _thread_doc(row)
    current = conn.execute(
        "SELECT fingerprint FROM search_docs WHERE kind = 'thread' AND ref_id = ?",
        (doc.ref_id,),
    ).fetchone()
    if current is None or current["fingerprint"] != doc.fingerprint:
        upsert_doc(conn, doc, thread_scope(thread_id))
    mark_thread(conn, thread_id)


# --------------------------------------------------------------------------- #
# Indexing a scope
# --------------------------------------------------------------------------- #


def index_scope(conn: sqlite3.Connection, scope_key: str) -> dict:
    """Re-render one scope and bring both tables in line with it.

    Caller owns the transaction (the indexer wraps this in BEGIN IMMEDIATE so
    the source read and the index write see the same state).
    """
    # Imported here: search_embed imports this module for Doc/scope helpers.
    from app.services import search_embed

    docs = render_scope(conn, scope_key)
    if docs is None:
        _drop_scope(conn, scope_key)
        return {"upserted": 0, "deleted": 0, "dropped": True}

    existing = {
        (r["kind"], r["ref_id"]): r["fingerprint"]
        for r in conn.execute(
            "SELECT kind, ref_id, fingerprint FROM search_docs WHERE scope_key = ?", (scope_key,)
        )
    }
    upserted = 0
    seen: set[tuple[str, str]] = set()
    for doc in docs:
        key = (doc.kind, doc.ref_id)
        seen.add(key)
        if existing.get(key) == doc.fingerprint:
            continue
        upsert_doc(conn, doc, scope_key)
        upserted += 1

    deleted = 0
    for kind, ref in existing.keys() - seen:
        deleted += _delete_fts_where(conn, "kind = ? AND ref_id = ?", (kind, ref))

    fp = source_fingerprint(conn, scope_key)
    owner_id, source_fp = fp if fp is not None else (docs[0].owner_id, "")
    chunk_count = len(search_embed.chunks_for_docs(docs))
    conn.execute(
        """
        INSERT INTO search_scopes (scope_key, owner_id, source_fp, chunk_count, indexed_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(scope_key) DO UPDATE SET
            owner_id = excluded.owner_id, source_fp = excluded.source_fp,
            chunk_count = excluded.chunk_count, indexed_at = excluded.indexed_at
        """,
        (scope_key, owner_id, source_fp, chunk_count, utcnow()),
    )
    conn.execute("DELETE FROM search_dirty WHERE scope_key = ?", (scope_key,))
    return {"upserted": upserted, "deleted": deleted, "dropped": False}


def drain_dirty(conn: sqlite3.Connection, *, limit: int = 200) -> int:
    """Index up to ``limit`` dirty scopes, each in its own short transaction.

    Returns how many scopes were processed. A scope whose render raises is
    logged and left dirty for the next pass rather than blocking the rest.
    """
    keys = [
        r[0]
        for r in conn.execute(
            "SELECT scope_key FROM search_dirty ORDER BY queued_at LIMIT ?", (limit,)
        )
    ]
    if conn.in_transaction:
        conn.commit()
    done = 0
    for key in keys:
        try:
            conn.execute("BEGIN IMMEDIATE")
            index_scope(conn, key)
            conn.commit()
            done += 1
        except Exception:
            conn.rollback()
            log.exception("search: indexing scope %s failed", key)
    return done


def index_all_now(conn: sqlite3.Connection) -> int:
    """Reconcile and drain until nothing is dirty. For tests and the CLI --
    the server does the same thing in the background."""
    reconcile(conn)
    total = 0
    while True:
        n = drain_dirty(conn, limit=500)
        total += n
        if n == 0:
            break
    return total


# --------------------------------------------------------------------------- #
# Reconcile + rebuild
# --------------------------------------------------------------------------- #


def reconcile(conn: sqlite3.Connection) -> dict:
    """Find every scope whose source changed without saying so, and every
    index row whose source is gone. Marks the former dirty, drops the latter.

    Also repairs a docs/FTS mismatch in either direction (a doc row with no
    text, or text with no doc row), which no fingerprint would notice.
    """
    current = all_source_fingerprints(conn)
    stored = {
        r["scope_key"]: r["source_fp"]
        for r in conn.execute("SELECT scope_key, source_fp FROM search_scopes")
    }
    dirty = {r[0] for r in conn.execute("SELECT scope_key FROM search_dirty")}

    stale = [k for k, (_, fp) in current.items() if stored.get(k) != fp and k not in dirty]

    # Docs present without their FTS text (or vice versa): re-render the scope.
    broken = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT d.scope_key FROM search_docs d "
            "WHERE NOT EXISTS (SELECT 1 FROM search_fts f WHERE f.rowid = d.id)"
        )
    ]
    if broken:
        conn.execute(
            "DELETE FROM search_docs WHERE NOT EXISTS "
            "(SELECT 1 FROM search_fts f WHERE f.rowid = search_docs.id)"
        )
    orphan_text = conn.execute(
        "DELETE FROM search_fts WHERE rowid NOT IN (SELECT id FROM search_docs)"
    ).rowcount

    to_mark = list(dict.fromkeys([*stale, *(k for k in broken if k in current)]))
    if to_mark:
        mark_dirty(conn, to_mark)

    gone = (set(stored) | {r[0] for r in conn.execute("SELECT DISTINCT scope_key FROM search_docs")}
            | dirty) - set(current)
    for key in gone:
        _drop_scope(conn, key)
    # Vectors under a scope that no longer exists at all.
    if gone:
        conn.executemany(
            "DELETE FROM search_embeddings WHERE scope_key = ?", [(k,) for k in gone]
        )

    from app.services import search_embed

    pruned = search_embed.prune_old_models(conn)

    result = {
        "scopes": len(current),
        "marked": len(to_mark),
        "dropped": len(gone),
        "repaired": len(broken) + orphan_text,
        "pruned_vectors": pruned,
    }
    if to_mark or gone or broken or orphan_text:
        log.info("search reconcile: %s", result)
    return result


def rebuild(conn: sqlite3.Connection) -> int:
    """Throw away the keyword index and queue every scope.

    Vectors are kept: each is keyed on its chunk's text hash, so the embedding
    pass that follows only re-checks them (and fixes their placement) rather
    than paying to embed everything again. A vector whose document no longer
    exists is invisible anyway -- semantic queries join through search_docs.
    """
    conn.execute("DELETE FROM search_fts")
    conn.execute("DELETE FROM search_docs")
    conn.execute("DELETE FROM search_scopes")
    conn.execute("DELETE FROM search_dirty")
    keys = list(all_source_fingerprints(conn).keys())
    mark_dirty(conn, keys)
    return len(keys)
