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

**CJK goes to a second index (MMN-16).** ``unicode61`` does not segment
Han/Kana/Hangul, so a run of them is one token and a part of it never
matches. :func:`parse_query` therefore routes every word containing a CJK
character away from ``search_fts`` and into ``search_fts_tri`` (trigram), as
a *substring* term:

* 3+ characters -> a quoted trigram ``MATCH`` term (indexed);
* 1-2 characters -> ``LIKE '%term%'`` on the trigram table's stored text. The
  trigram index cannot serve a pattern shorter than three characters, so this
  is a per-row check -- but only over rows the owner filter (and any other
  arm) already selected, and two-character words are the *common* case in
  Chinese (会议), so refusing them was not an option.

Every arm is a condition on the same ``search_docs`` row, so a mixed query
(``Q3 会议``) is an AND across arms and the result is still *one* keyword list
-- RRF and ``matched_by`` cannot tell the difference. CJK words come out of
the same :func:`_words` splitter, so they are alphanumeric runs only: no
quote, ``%`` or ``_`` can reach either MATCH or LIKE.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.config import effective
from app.errors import ValidationError
from app.services import search_embed
from app.services.search_index import KINDS

TITLE_WEIGHT = 4.0
RRF_K = 60
CANDIDATE_SLACK = 50
SNIPPET_TOKENS = 16
# A trigram "token" is one character wide, so the same visual length needs more.
TRI_SNIPPET_TOKENS = 40
LIKE_SNIPPET_BEFORE = 24
LIKE_SNIPPET_AFTER = 64
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


# Han (incl. extensions and compatibility), Kana, Hangul, Bopomofo and the
# CJK symbols that are letters to str.isalnum() (々 〆 〇). Any word holding
# one of these is a run unicode61 will not split.
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x1100, 0x11FF),    # Hangul Jamo
    (0x2E80, 0x2FDF),    # CJK radicals, Kangxi radicals
    (0x3005, 0x3007),    # 々 〆 〇
    (0x3021, 0x3029),    # Hangzhou numerals
    (0x3031, 0x3035),    # Kana repeat marks
    (0x3038, 0x303C),
    (0x3040, 0x30FF),    # Hiragana, Katakana
    (0x3100, 0x31FF),    # Bopomofo, Hangul compat Jamo, Kanbun, Katakana ext
    (0x3400, 0x4DBF),    # CJK ext A
    (0x4E00, 0x9FFF),    # CJK unified
    (0xA960, 0xA97F),    # Hangul Jamo ext A
    (0xAC00, 0xD7FF),    # Hangul syllables, Jamo ext B
    (0xF900, 0xFAFF),    # CJK compatibility
    (0xFF66, 0xFFDC),    # half-width Katakana / Hangul
    (0x1B000, 0x1B16F),  # Kana supplement / ext
    (0x20000, 0x3134F),  # CJK ext B-G
)

# The trigram tokenizer's own limit: a MATCH term under three characters
# matches nothing at all (not an error -- silently nothing).
TRIGRAM_MIN_CHARS = 3


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def has_cjk(text: str) -> bool:
    return any(_is_cjk(ch) for ch in text)


@dataclass(frozen=True)
class ParsedQuery:
    """A query split across the two keyword indexes.

    ``match`` is the unicode61 expression for ``search_fts`` (None if no
    non-CJK word survived); ``cjk`` the substring terms for ``search_fts_tri``.
    """

    match: str | None
    cjk: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return self.match is None and not self.cjk

    @property
    def tri_match(self) -> str | None:
        """The 3+ character CJK terms as one trigram MATCH (ANDed)."""
        long = [t for t in self.cjk if len(t) >= TRIGRAM_MIN_CHARS]
        return " ".join(f'"{t}"' for t in long) or None

    @property
    def tri_like(self) -> list[str]:
        """The 1-2 character CJK terms, matched with LIKE."""
        return [t for t in self.cjk if len(t) < TRIGRAM_MIN_CHARS]


def parse_query(q: str | None) -> ParsedQuery:
    """User text -> safe expressions for both keyword indexes.

    * ``"exact phrase"`` -> an FTS phrase (an unbalanced quote closes at the end)
    * ``term*`` -> prefix query on its last word (needs >= 2 chars, else exact)
    * anything else -> each token quoted; tokens are ANDed by juxtaposition
    * any word containing CJK -> a substring term for the trigram index. A
      token or phrase that mixes CJK and other words degrades to an AND of its
      words: adjacency across the two indexes is not expressible, and a
      looser match beats a silent empty result. ``*`` on a CJK word is
      dropped -- a substring match already is a prefix match.
    """
    if not q:
        return ParsedQuery(None)
    text = unicodedata.normalize("NFC", q)
    parts: list[str] = []
    cjk: list[str] = []

    def add(words: list[str], *, prefix: bool) -> None:
        if not any(has_cjk(w) for w in words):
            phrase = '"' + " ".join(words) + '"'
            if prefix and len(words[-1]) >= 2:
                phrase += "*"
            parts.append(phrase)
            return
        for idx, w in enumerate(words):
            if has_cjk(w):
                cjk.append(w)
            else:
                star = "*" if prefix and idx == len(words) - 1 and len(w) >= 2 else ""
                parts.append(f'"{w}"{star}')

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
                add(words, prefix=False)
            i = end + 1
            continue
        j = i
        while j < n and not text[j].isspace() and text[j] != '"':
            j += 1
        raw = text[i:j]
        i = j
        words = _words(raw)
        if words:
            add(words, prefix=raw.endswith("*"))
    return ParsedQuery(" ".join(parts) or None, tuple(dict.fromkeys(cjk)))


def to_match_expr(q: str | None) -> str | None:
    """The unicode61 half of :func:`parse_query` -- the ``search_fts`` MATCH
    expression, or None if nothing non-CJK is left."""
    return parse_query(q).match


def doc_select(query: ParsedQuery, *, columns: str, where: str) -> tuple[str, list]:
    """A ``SELECT {columns} FROM ... search_docs d`` over every doc matching
    ``query`` (all arms ANDed) and ``where``. Starts from whichever index
    can narrow first -- a MATCH when there is one -- rather than from
    ``search_docs``. Used by the home thread filter; :func:`keyword_candidates`
    uses the same pieces but also ranks and snippets.
    """
    if query.match:
        driver, expr = "search_fts", query.match
        extra, extra_params = _tri_conditions(query, "d", include_match=True)
    elif query.tri_match:
        driver, expr = "search_fts_tri", query.tri_match
        extra, extra_params = _tri_conditions(query, "d", include_match=False)
    else:
        extra, extra_params = _tri_conditions(query, "d", include_match=False)
        cond = " AND ".join([where, *extra])
        return f"SELECT {columns} FROM search_docs d WHERE {cond}", extra_params
    cond = " AND ".join([f"{driver} MATCH ?", where, *extra])
    return (
        f"SELECT {columns} FROM {driver} JOIN search_docs d ON d.id = {driver}.rowid "
        f"WHERE {cond}",
        [expr, *extra_params],
    )


def _tri_conditions(
    query: ParsedQuery, alias: str, *, include_match: bool
) -> tuple[list[str], list]:
    conds: list[str] = []
    params: list = []
    if include_match and query.tri_match:
        conds.append(
            f"{alias}.id IN (SELECT rowid FROM search_fts_tri WHERE search_fts_tri MATCH ?)"
        )
        params.append(query.tri_match)
    for term in query.tri_like:
        # A rowid lookup per candidate row, not a scan of the whole table:
        # LIKE under three characters cannot use the trigram index anyway.
        # ``term`` is an alphanumeric run (see _words), so it holds no LIKE
        # wildcard and needs no ESCAPE clause.
        conds.append(
            f"EXISTS (SELECT 1 FROM search_fts_tri x WHERE x.rowid = {alias}.id "
            "AND (x.title LIKE ? OR x.body LIKE ?))"
        )
        params.extend([f"%{term}%", f"%{term}%"])
    return conds, params


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
    query: ParsedQuery | str,
    filter_sql: str,
    filter_params: list,
    limit: int,
) -> list[dict]:
    """The keyword arm: one ranked list over both FTS tables.

    Driven by whichever index can rank: ``search_fts`` when the query has a
    non-CJK word (the CJK terms ride along as conditions), else
    ``search_fts_tri`` for a 3+ character CJK term, else -- only 1-2
    character CJK terms -- a LIKE pass with no bm25, ordered title-first then
    newest, and a snippet cut in Python.
    """
    if isinstance(query, str):
        query = ParsedQuery(query)
    if query.match:
        # bm25 takes one weight per column in declaration order; the six
        # UNINDEXED columns contribute nothing either way.
        weights = ", ".join(["0"] * 6 + [str(TITLE_WEIGHT), "1.0"])
        extra, extra_params = _tri_conditions(query, "d", include_match=True)
        return _ranked(
            conn, table="search_fts", weights=weights, tokens=SNIPPET_TOKENS,
            expr=query.match, owner_id=owner_id, extra=extra, extra_params=extra_params,
            filter_sql=filter_sql, filter_params=filter_params, limit=limit,
        )
    if query.tri_match:
        extra, extra_params = _tri_conditions(query, "d", include_match=False)
        return _ranked(
            conn, table="search_fts_tri", weights=f"{TITLE_WEIGHT}, 1.0",
            tokens=TRI_SNIPPET_TOKENS, expr=query.tri_match, owner_id=owner_id,
            extra=extra, extra_params=extra_params, filter_sql=filter_sql,
            filter_params=filter_params, limit=limit,
        )
    if not query.tri_like:
        return []
    extra, extra_params = _tri_conditions(query, "d", include_match=False)
    first = f"%{query.tri_like[0]}%"
    rows = conn.execute(
        f"""
        SELECT d.id AS doc_id, x.title AS title, x.body AS body
          FROM search_docs d
          JOIN search_fts_tri x ON x.rowid = d.id
         WHERE d.owner_id = ? {filter_sql} AND {" AND ".join(extra)}
         ORDER BY CASE WHEN x.title LIKE ? THEN 0 ELSE 1 END, d.date DESC, d.id DESC
         LIMIT ?
        """,
        [owner_id, *filter_params, *extra_params, first, limit],
    ).fetchall()
    return [
        {
            "doc_id": r["doc_id"],
            "snippet": like_snippet(r["title"], r["body"], query.tri_like),
            "bm25": None,
        }
        for r in rows
    ]


def _ranked(
    conn: sqlite3.Connection,
    *,
    table: str,
    weights: str,
    tokens: int,
    expr: str,
    owner_id: int,
    extra: list[str],
    extra_params: list,
    filter_sql: str,
    filter_params: list,
    limit: int,
) -> list[dict]:
    extra_sql = "".join(f" AND {c}" for c in extra)
    rows = conn.execute(
        f"""
        SELECT d.id AS doc_id,
               snippet({table}, -1, ?, ?, '…', {tokens}) AS snip,
               bm25({table}, {weights}) AS rank
          FROM {table}
          JOIN search_docs d ON d.id = {table}.rowid
         WHERE {table} MATCH ? AND d.owner_id = ? {filter_sql}{extra_sql}
         ORDER BY rank
         LIMIT ?
        """,
        [MARK_OPEN, MARK_CLOSE, expr, owner_id, *filter_params, *extra_params, limit],
    ).fetchall()
    return [{"doc_id": r["doc_id"], "snippet": r["snip"], "bm25": r["rank"]} for r in rows]


def like_snippet(title: str | None, body: str | None, terms: list[str]) -> str:
    """A snippet for a LIKE-only hit, marked like FTS5's ``snippet()``.

    The window is cut around the first hit in the body (or the title, when
    only the title matched), and every occurrence of every term inside it is
    wrapped in MARK_OPEN/MARK_CLOSE.
    """
    for text in (body or "", title or ""):
        text = " ".join(text.split())
        positions = [p for p in (text.find(t) for t in terms) if p >= 0]
        if not positions:
            continue
        pos = min(positions)
        start = max(0, pos - LIKE_SNIPPET_BEFORE)
        end = min(len(text), pos + LIKE_SNIPPET_AFTER)
        window = text[start:end]
        # One pass, longest alternative first, so a term inside another is
        # wrapped once rather than nested.
        pattern = "|".join(re.escape(t) for t in sorted(set(terms), key=len, reverse=True))
        window = re.sub(pattern, lambda m: f"{MARK_OPEN}{m.group(0)}{MARK_CLOSE}", window)
        return ("…" if start > 0 else "") + window + ("…" if end < len(text) else "")
    return ""


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
        parsed = parse_query(q)
        if not parsed.empty:
            keyword = keyword_candidates(
                conn, owner_id=owner_id, query=parsed, filter_sql=filter_sql,
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
            "index_bytes": index_sizes(conn),
        }
    return out


# FTS5's shadow tables, per virtual table. ``_idx`` and ``_config`` are
# WITHOUT ROWID, so they carry no separate autoindex to count.
_FTS_SHADOWS = ("data", "idx", "content", "docsize", "config")
_SIZE_CACHE: dict[str, tuple[float, dict]] = {}
SIZE_CACHE_SEC = 300.0


def index_sizes(conn: sqlite3.Connection) -> dict[str, int | None]:
    """Bytes on disk of each keyword index (MMN-16: the trigram one is the
    expensive one, roughly 3x the unicode61 index, so it is reported).

    Read from ``dbstat``, which walks every page of a table -- so the result
    is cached per database file for ``SIZE_CACHE_SEC``, because the Settings
    page polls status every two seconds while indexing. ``None`` when this
    SQLite build has no ``dbstat`` (it is a compile-time option).
    """
    import time

    db_file = conn.execute("PRAGMA database_list").fetchone()[2] or ":memory:"
    hit = _SIZE_CACHE.get(db_file)
    now = time.monotonic()
    if hit is not None and now - hit[0] < SIZE_CACHE_SEC:
        return hit[1]
    out: dict[str, int | None] = {}
    for label, table in (("keyword", "search_fts"), ("trigram", "search_fts_tri")):
        try:
            total = 0
            for shadow in _FTS_SHADOWS:
                row = conn.execute(
                    "SELECT pgsize FROM dbstat WHERE name = ? AND aggregate = 1",
                    (f"{table}_{shadow}",),
                ).fetchone()
                total += int(row[0] or 0) if row else 0
            out[label] = total
        except sqlite3.OperationalError:
            out[label] = None
    _SIZE_CACHE[db_file] = (now, out)
    return out
