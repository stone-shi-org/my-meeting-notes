"""One search over everything a user owns (MMN-15).

``search()`` is the single entry point: the home page's "Search everything"
view calls it through ``GET /api/search``, and MMN-14's MCP ``search`` tool is
meant to call it directly. Two arms, merged with Reciprocal Rank Fusion:

* **keyword** -- FTS5 over ``search_fts``, ranked by ``bm25()`` with the title
  column weighted ``TITLE_WEIGHT`` times the body;
* **semantic** -- the query embedded once and scored against the owner's
  stored vectors (``search_embed``). Fails *open*: disabled, unreachable, or
  nothing embedded yet all mean "keyword results only", reported in the
  response as ``semantic.available = false`` with a reason, never an error.

**User input never reaches MATCH raw.** :func:`to_match_expr` rebuilds it from
alphanumeric words only, each one double-quoted, so ``"``, ``-``, ``*``, ``:``,
``(``, ``NEAR``/``OR``/``AND`` and emoji are all literal (or dropped) and a
stray character can never raise ``fts5: syntax error``. A bare multi-word
query is an AND of its words; ``"quoted phrases"`` and ``prefix*`` are kept.

Ownership is a WHERE clause on ``search_docs.owner_id`` in both arms -- never
a filter applied to results afterwards.

Known limitation: the ``unicode61`` tokenizer does not segment CJK text, so a
run of Han characters is one token and a substring inside it will not match.
A trigram side index is the follow-up (deferred by the reviewer).
"""

from __future__ import annotations

import sqlite3
import unicodedata
from datetime import date, datetime, timedelta

from app.config import effective
from app.errors import ValidationError
from app.services import search_embed
from app.services.search_index import KINDS

TITLE_WEIGHT = 4.0
RRF_K = 60
CANDIDATE_SLACK = 50
SNIPPET_TOKENS = 16
SEMANTIC_SNIPPET_CHARS = 240
MARK_OPEN = "\x02"
MARK_CLOSE = "\x03"

MODES = ("hybrid", "keyword", "semantic")


# --------------------------------------------------------------------------- #
# Query parsing
# --------------------------------------------------------------------------- #


def _words(text: str) -> list[str]:
    """Split the way unicode61 does closely enough: runs of letters/digits.

    Everything else (punctuation, symbols, emoji, ``_``) is a separator, which
    is exactly what makes the result safe to quote into MATCH.
    """
    out: list[str] = []
    current: list[str] = []
    for ch in text:
        if ch.isalnum():
            current.append(ch)
        elif current:
            out.append("".join(current))
            current = []
    if current:
        out.append("".join(current))
    return out


def to_match_expr(q: str | None) -> str | None:
    """User text -> a safe FTS5 MATCH expression, or None if nothing is left.

    * ``"exact phrase"`` -> an FTS phrase (an unbalanced quote closes at the end)
    * ``term*`` -> prefix query on its last word (needs >= 2 chars, else exact)
    * anything else -> each token quoted; tokens are ANDed by juxtaposition
    """
    if not q:
        return None
    text = unicodedata.normalize("NFC", q)
    parts: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch == '"':
            end = text.find('"', i + 1)
            if end == -1:
                end = n
            words = _words(text[i + 1 : end])
            if words:
                parts.append('"' + " ".join(words) + '"')
            i = end + 1
            continue
        j = i
        while j < n and not text[j].isspace() and text[j] != '"':
            j += 1
        raw = text[i:j]
        i = j
        prefix = raw.endswith("*")
        words = _words(raw)
        if not words:
            continue
        phrase = '"' + " ".join(words) + '"'
        if prefix and len(words[-1]) >= 2:
            phrase += "*"
        parts.append(phrase)
    return " ".join(parts) or None


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #


def parse_kinds(raw: str | list[str] | None) -> list[str] | None:
    if raw is None:
        return None
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    kinds = [k.strip() for k in items if k and k.strip()]
    if not kinds:
        return None
    unknown = [k for k in kinds if k not in KINDS]
    if unknown:
        raise ValidationError(
            f"Unknown kind(s): {', '.join(unknown)}. Expected any of: {', '.join(KINDS)}"
        )
    return list(dict.fromkeys(kinds))


def parse_bound(value: str | None, *, name: str, upper: bool) -> str | None:
    """ISO date or datetime -> a string comparable with stored ISO dates.

    A bare ``until`` date is inclusive (the whole day), so it becomes "before
    the next midnight".
    """
    if not value:
        return None
    raw = value.strip()
    try:
        if len(raw) == 10:
            d = date.fromisoformat(raw)
            return (d + timedelta(days=1)).isoformat() if upper else d.isoformat()
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
    except ValueError:
        raise ValidationError(f"{name} must be an ISO-8601 date or datetime") from None


def _filters(
    *, kinds: list[str] | None, since: str | None, until: str | None, thread_id: int | None
) -> tuple[str, list]:
    sql = ""
    params: list = []
    if kinds:
        sql += f" AND d.kind IN ({', '.join('?' for _ in kinds)})"
        params.extend(kinds)
    if thread_id is not None:
        sql += " AND d.thread_id = ?"
        params.append(thread_id)
    if since:
        sql += " AND d.date >= ?"
        params.append(since)
    if until:
        sql += " AND d.date < ?"
        params.append(until)
    return sql, params


# --------------------------------------------------------------------------- #
# The two arms
# --------------------------------------------------------------------------- #


def keyword_candidates(
    conn: sqlite3.Connection,
    *,
    owner_id: int,
    expr: str,
    filter_sql: str,
    filter_params: list,
    limit: int,
) -> list[dict]:
    # bm25 takes one weight per column in declaration order; the six
    # UNINDEXED columns contribute nothing either way.
    weights = ", ".join(["0"] * 6 + [str(TITLE_WEIGHT), "1.0"])
    rows = conn.execute(
        f"""
        SELECT d.id AS doc_id,
               snippet(search_fts, -1, ?, ?, '…', {SNIPPET_TOKENS}) AS snip,
               bm25(search_fts, {weights}) AS rank
          FROM search_fts
          JOIN search_docs d ON d.id = search_fts.rowid
         WHERE search_fts MATCH ? AND d.owner_id = ? {filter_sql}
         ORDER BY rank
         LIMIT ?
        """,
        [MARK_OPEN, MARK_CLOSE, expr, owner_id, *filter_params, limit],
    ).fetchall()
    return [{"doc_id": r["doc_id"], "snippet": r["snip"], "bm25": r["rank"]} for r in rows]


def rrf_merge(*ranked: list[int], k: int = RRF_K) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion: sum of 1 / (k + rank) over every list a doc is
    in (rank is 1-based). Ties keep first-list order, so keyword wins a tie."""
    scores: dict[int, float] = {}
    order: dict[int, tuple[int, int]] = {}
    for li, ids in enumerate(ranked):
        for pos, doc_id in enumerate(ids, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + pos)
            order.setdefault(doc_id, (li, pos))
    return sorted(scores.items(), key=lambda kv: (-kv[1], order[kv[0]]))


# --------------------------------------------------------------------------- #
# Hits
# --------------------------------------------------------------------------- #


def hit_url(kind: str, thread_id: int | None, meeting_id: int | None, start_sec) -> str:
    if kind == "segment" and meeting_id is not None:
        t = f"{start_sec:.1f}".rstrip("0").rstrip(".") if start_sec is not None else "0"
        return f"/meetings/{meeting_id}?t={t}"
    if kind in ("meeting", "summary", "action_item") and meeting_id is not None:
        return f"/meetings/{meeting_id}"
    return f"/threads/{thread_id}"


def _hit_id(kind: str, ref_id: str) -> int | None:
    raw = ref_id.split(":")[-1] if kind == "segment" else ref_id
    try:
        return int(raw)
    except ValueError:
        return None


def _semantic_snippet(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= SEMANTIC_SNIPPET_CHARS:
        return text
    return text[: SEMANTIC_SNIPPET_CHARS - 1].rstrip() + "…"


def _load_docs(conn: sqlite3.Connection, owner_id: int, doc_ids: list[int]) -> dict[int, sqlite3.Row]:
    if not doc_ids:
        return {}
    marks = ", ".join("?" for _ in doc_ids)
    rows = conn.execute(
        f"""
        SELECT d.*, t.title AS thread_title, m.title AS meeting_title
          FROM search_docs d
          LEFT JOIN threads t ON t.id = d.thread_id
          LEFT JOIN meetings m ON m.id = d.meeting_id
         WHERE d.owner_id = ? AND d.id IN ({marks})
        """,
        [owner_id, *doc_ids],
    ).fetchall()
    return {r["id"]: r for r in rows}


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def search(
    conn: sqlite3.Connection,
    *,
    owner_id: int,
    q: str,
    kinds: list[str] | str | None = None,
    since: str | None = None,
    until: str | None = None,
    thread_id: int | None = None,
    mode: str = "hybrid",
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """Search ``owner_id``'s documents. The caller has already checked that
    ``thread_id`` (if given) belongs to the owner -- the router 404s otherwise."""
    if mode not in MODES:
        raise ValidationError(f"mode must be one of: {', '.join(MODES)}")
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    kind_list = parse_kinds(kinds)
    filter_sql, filter_params = _filters(
        kinds=kind_list,
        since=parse_bound(since, name="since", upper=False),
        until=parse_bound(until, name="until", upper=True),
        thread_id=thread_id,
    )
    want = offset + limit + CANDIDATE_SLACK

    keyword: list[dict] = []
    semantic: list[dict] = []
    available, reason = False, None

    if mode in ("hybrid", "keyword"):
        expr = to_match_expr(q)
        if expr:
            keyword = keyword_candidates(
                conn, owner_id=owner_id, expr=expr, filter_sql=filter_sql,
                filter_params=filter_params, limit=want,
            )

    if mode in ("hybrid", "semantic"):
        try:
            query_vec, model = search_embed.embed_query(conn, q.strip())
            semantic = search_embed.semantic_candidates(
                conn, owner_id=owner_id, query_vec=query_vec, model=model,
                filter_sql=filter_sql, filter_params=filter_params, limit=want,
                min_score=float(effective(conn, "embedding_min_score") or 0.0),
            )
            available = True
        except search_embed.SemanticUnavailable as exc:
            reason = exc.reason
    else:
        reason = None

    if mode == "keyword":
        mode_used = "keyword"
    elif mode == "semantic":
        mode_used = "semantic"
    else:
        mode_used = "hybrid" if available else "keyword"

    merged = rrf_merge([h["doc_id"] for h in keyword], [h["doc_id"] for h in semantic])
    page = merged[offset : offset + limit]
    kw_by_id = {h["doc_id"]: h for h in keyword}
    sem_by_id = {h["doc_id"]: h for h in semantic}
    docs = _load_docs(conn, owner_id, [doc_id for doc_id, _ in page])

    hits: list[dict] = []
    for doc_id, score in page:
        row = docs.get(doc_id)
        if row is None:  # pragma: no cover - deleted between the arms and here
            continue
        matched_by = []
        if doc_id in kw_by_id:
            matched_by.append("keyword")
        if doc_id in sem_by_id:
            matched_by.append("semantic")
        if doc_id in kw_by_id and kw_by_id[doc_id]["snippet"]:
            snippet = kw_by_id[doc_id]["snippet"]
        elif doc_id in sem_by_id:
            snippet = _semantic_snippet(sem_by_id[doc_id]["chunk_text"])
        else:
            snippet = ""
        hits.append(
            {
                "kind": row["kind"],
                "id": _hit_id(row["kind"], row["ref_id"]),
                "ref_id": row["ref_id"],
                "thread_id": row["thread_id"],
                "thread_title": row["thread_title"],
                "meeting_id": row["meeting_id"],
                "meeting_title": row["meeting_title"],
                "start_sec": row["start_sec"] if row["kind"] == "segment" else None,
                "title": row["label"],
                "snippet": snippet,
                "date": row["date"],
                "score": round(score, 6),
                "matched_by": matched_by,
                "url": hit_url(row["kind"], row["thread_id"], row["meeting_id"], row["start_sec"]),
            }
        )

    return {
        "query": q,
        "mode": mode,
        "mode_used": mode_used,
        "semantic": {
            "available": available,
            "reason": None if available or mode == "keyword" else reason,
        },
        "limit": limit,
        "offset": offset,
        "has_more": len(merged) > offset + limit,
        "hits": hits,
    }


# --------------------------------------------------------------------------- #
# Status (Settings -> Search)
# --------------------------------------------------------------------------- #


def status(conn: sqlite3.Connection, *, owner_id: int, is_admin: bool) -> dict:
    counts = dict(
        conn.execute(
            "SELECT kind, COUNT(*) FROM search_docs WHERE owner_id = ? GROUP BY kind", (owner_id,)
        ).fetchall()
    )
    # Pending = this owner's threads/meetings that are queued, or that have
    # never been indexed at all (e.g. the first boot before reconcile ran).
    pending = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM threads t WHERE t.owner_id = :o AND (
              NOT EXISTS (SELECT 1 FROM search_scopes s WHERE s.scope_key = 't:' || t.id)
              OR EXISTS (SELECT 1 FROM search_dirty q WHERE q.scope_key = 't:' || t.id)))
          +
          (SELECT COUNT(*) FROM meetings m WHERE m.owner_id = :o AND (
              NOT EXISTS (SELECT 1 FROM search_scopes s WHERE s.scope_key = 'm:' || m.id)
              OR EXISTS (SELECT 1 FROM search_dirty q WHERE q.scope_key = 'm:' || m.id)))
        """,
        {"o": owner_id},
    ).fetchone()[0]
    last_indexed = conn.execute(
        "SELECT MAX(indexed_at) FROM search_scopes WHERE owner_id = ?", (owner_id,)
    ).fetchone()[0]

    enabled = search_embed.is_enabled(conn)
    model = search_embed.current_model(conn)
    chunks = conn.execute(
        "SELECT COALESCE(SUM(chunk_count), 0) FROM search_scopes WHERE owner_id = ?", (owner_id,)
    ).fetchone()[0]
    embedded = conn.execute(
        "SELECT COUNT(*) FROM search_embeddings WHERE owner_id = ? AND model = ?",
        (owner_id, model),
    ).fetchone()[0]
    state = search_embed.state()

    out = {
        "kinds": [{"kind": k, "indexed": int(counts.get(k, 0))} for k in KINDS],
        "pending_scopes": int(pending),
        "last_indexed_at": last_indexed,
        "embedding": {
            "enabled": enabled,
            "model": model or None,
            "chunks": int(chunks),
            "embedded": int(min(embedded, chunks) if chunks else embedded),
            "pending_scopes": search_embed.pending_scope_count(conn, model, owner_id) if enabled else 0,
            "scale_warning": int(chunks) > search_embed.SCALE_LIMIT,
            "scale_limit": search_embed.SCALE_LIMIT,
            "last_error": state.get("last_error") if enabled else None,
        },
        "is_admin": is_admin,
    }
    if is_admin:
        out["global"] = {
            "docs": conn.execute("SELECT COUNT(*) FROM search_docs").fetchone()[0],
            "scopes": conn.execute("SELECT COUNT(*) FROM search_scopes").fetchone()[0],
            "pending_scopes": conn.execute("SELECT COUNT(*) FROM search_dirty").fetchone()[0],
            "chunks": conn.execute(
                "SELECT COUNT(*) FROM search_embeddings WHERE model = ?", (model,)
            ).fetchone()[0],
        }
    return out
