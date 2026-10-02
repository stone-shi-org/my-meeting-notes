"""The semantic half of search: chunking, the embedding backfill, and scoring.

MMN-15. Vectors come from the existing OpenAI-compatible client in
``services/embeddings.py`` -- same settings, same fail-open contract: a broken
or disabled embedding endpoint means "keyword results only", never an error.

**Off the request path.** Only the *query* is embedded inside a request (one
short input). Documents are embedded by :func:`embed_pass`, which the
background indexer runs after each drain -- batched, at most
``EMBED_CONCURRENCY`` requests in flight, blocking HTTP inside
``asyncio.to_thread``, and no job-queue entry per chunk, for the same reasons
email hydration avoids the queue.

**Resumable for free.** A scope wants embedding when ``search_scopes.embed_fp``
differs from its ``source_fp`` or ``embed_model`` differs from the current
model; inside it, a chunk is re-sent only if its ``text_sha256`` changed or it
has no row under the current model. An interrupted pass simply finds the same
predicate true next time.

**Brute force, on purpose.** Scoring is a pure-Python dot product over every
one of the owner's vectors (they are L2-normalised at write, so dot = cosine).
At 1024 dimensions that is roughly 1 ms per 100 chunks in CPython, so it stays
under ~250 ms up to about ``SCALE_LIMIT`` chunks per owner. Past that, the
next step is sqlite-vec or numpy -- the Settings -> Search page shows the
count and warns when an owner crosses it.
"""

from __future__ import annotations

import array
import asyncio
import hashlib
import heapq
import math
import operator
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.config import effective
from app.db import get_conn, utcnow
from app.errors import EmbeddingError
from app.logging_config import get_logger
from app.services import embeddings as embeddings_svc
from app.services.search_index import Doc

log = get_logger("search_embed")

WINDOW_MAX_SEGMENTS = 8
WINDOW_MAX_CHARS = 800
WINDOW_OVERLAP = 2
PARAGRAPH_CHUNK_CHARS = 1200
SINGLE_CHUNK_CHARS = 2000
EMBED_BATCH = 32
EMBED_CONCURRENCY = 2
SCALE_LIMIT = 25_000

_PARAGRAPH_KINDS = ("summary", "note", "email")

# Last outcome of an embedding pass, for the status page. One process is
# assumed, as it is for the job queue and the schedulers.
_state: dict = {"last_error": None, "last_error_at": None, "last_success_at": None}


def state() -> dict:
    return dict(_state)


@dataclass(frozen=True)
class Chunk:
    kind: str       # the document kind a hit on this chunk resolves to
    ref_id: str     # the chunk's own key, unique per kind
    doc_ref: str    # search_docs.ref_id of that document
    start_sec: float | None
    text: str

    @property
    def sha(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #


def _hard_split(text: str, size: int) -> list[str]:
    out: list[str] = []
    while len(text) > size:
        cut = text.rfind(" ", 0, size)
        if cut < size // 2:
            cut = size
        out.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        out.append(text)
    return out


def _paragraph_chunks(text: str, size: int = PARAGRAPH_CHUNK_CHARS) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        pieces = _hard_split(para, size) if len(para) > size else [para]
        for piece in pieces:
            if current and len(current) + 2 + len(piece) > size:
                chunks.append(current)
                current = piece
            else:
                current = f"{current}\n\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks


def _segment_windows(segments: list[Doc]) -> list[Chunk]:
    segs = sorted(segments, key=lambda d: (d.start_sec or 0.0, d.ref_id))
    out: list[Chunk] = []
    i = 0
    while i < len(segs):
        lines: list[str] = []
        j = i
        size = 0
        while j < len(segs) and j - i < WINDOW_MAX_SEGMENTS:
            line = f"{segs[j].title}: {segs[j].body}" if segs[j].title else segs[j].body
            if lines and size + len(line) > WINDOW_MAX_CHARS:
                break
            lines.append(line)
            size += len(line) + 1
            j += 1
        first = segs[i]
        out.append(
            Chunk(
                kind="segment",
                ref_id=f"w:{first.ref_id}:{j - i}",
                doc_ref=first.ref_id,
                start_sec=first.start_sec,
                text="\n".join(lines)[: WINDOW_MAX_CHARS * 2],
            )
        )
        if j >= len(segs):
            break
        i = max(i + 1, j - WINDOW_OVERLAP)
    return out


def chunks_for_docs(docs: list[Doc]) -> list[Chunk]:
    """Every chunk one scope's documents produce. Deterministic, so the same
    docs always give the same keys -- which is what makes resume-by-hash work."""
    chunks: list[Chunk] = []
    segments = [d for d in docs if d.kind == "segment"]
    if segments:
        chunks.extend(_segment_windows(segments))

    for doc in docs:
        if doc.kind == "segment":
            continue
        if doc.kind in _PARAGRAPH_KINDS:
            text = "\n\n".join(p for p in (doc.title, doc.body) if p)
            for i, piece in enumerate(_paragraph_chunks(text)):
                chunks.append(Chunk(doc.kind, f"{doc.ref_id}#p{i}", doc.ref_id, None, piece))
        else:
            text = "\n".join(p for p in (doc.title or doc.label, doc.body) if p).strip()
            if text:
                chunks.append(
                    Chunk(doc.kind, doc.ref_id, doc.ref_id, doc.start_sec, text[:SINGLE_CHUNK_CHARS])
                )
    return [c for c in chunks if c.text.strip()]


def stored_docs(conn: sqlite3.Connection, scope_key: str) -> list[Doc]:
    """Rebuild a scope's Doc objects from the index itself, so the embed pass
    chunks exactly the text the keyword index holds."""
    rows = conn.execute(
        """
        SELECT d.kind, d.ref_id, d.owner_id, d.thread_id, d.meeting_id, d.start_sec,
               d.label, d.date, f.title, f.body
          FROM search_docs d JOIN search_fts f ON f.rowid = d.id
         WHERE d.scope_key = ?
        """,
        (scope_key,),
    ).fetchall()
    return [
        Doc(
            kind=r["kind"], ref_id=r["ref_id"], owner_id=r["owner_id"], thread_id=r["thread_id"],
            meeting_id=r["meeting_id"], start_sec=r["start_sec"], title=r["title"] or "",
            body=r["body"] or "", label=r["label"] or "", date=r["date"],
        )
        for r in rows
    ]


# --------------------------------------------------------------------------- #
# Vectors
# --------------------------------------------------------------------------- #


def normalise(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return [0.0] * len(vec)
    return [x / norm for x in vec]


def pack(vec: list[float]) -> bytes:
    return array.array("f", normalise(vec)).tobytes()


def unpack(blob: bytes) -> array.array:
    out = array.array("f")
    out.frombytes(blob)
    return out


def dot(a, b) -> float:
    return sum(map(operator.mul, a, b))


# --------------------------------------------------------------------------- #
# The backfill
# --------------------------------------------------------------------------- #


def current_model(conn: sqlite3.Connection) -> str:
    return str(effective(conn, "embedding_model") or "")


def is_enabled(conn: sqlite3.Connection) -> bool:
    return bool(effective(conn, "embedding_enabled")) and bool(current_model(conn))


def _pending_scopes_sql(owner_filter: bool) -> str:
    sql = (
        "FROM search_scopes WHERE (embed_fp IS NULL OR embed_fp != source_fp "
        "OR embed_model IS NULL OR embed_model != ?)"
    )
    if owner_filter:
        sql += " AND owner_id = ?"
    return sql


def pending_scope_count(conn: sqlite3.Connection, model: str, owner_id: int | None = None) -> int:
    params: list = [model]
    if owner_id is not None:
        params.append(owner_id)
    return conn.execute(
        f"SELECT COUNT(*) {_pending_scopes_sql(owner_id is not None)}", params
    ).fetchone()[0]


async def _embed_all(config, texts: list[str]) -> list[list[float]]:
    sem = asyncio.Semaphore(EMBED_CONCURRENCY)
    batches = [texts[i : i + EMBED_BATCH] for i in range(0, len(texts), EMBED_BATCH)]

    async def run(batch: list[str]) -> list[list[float]]:
        async with sem:
            return await asyncio.to_thread(embeddings_svc.embed_texts, config, batch)

    results = await asyncio.gather(*(run(b) for b in batches))
    return [v for batch in results for v in batch]


def _write_scope_vectors(
    conn: sqlite3.Connection,
    *,
    scope_key: str,
    source_fp: str,
    model: str,
    docs_by_ref: dict[tuple[str, str], Doc],
    chunks: list[Chunk],
    fresh: dict[tuple[str, str], list[float]],
) -> None:
    now = utcnow()
    for chunk in chunks:
        doc = docs_by_ref.get((chunk.kind, chunk.doc_ref))
        if doc is None:
            continue
        key = (chunk.kind, chunk.ref_id)
        if key in fresh:
            vec = fresh[key]
            conn.execute(
                """
                INSERT INTO search_embeddings (kind, ref_id, doc_ref, scope_key, owner_id,
                    thread_id, meeting_id, start_sec, model, dim, vector, text_sha256,
                    chunk_text, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(kind, ref_id, model) DO UPDATE SET
                    doc_ref = excluded.doc_ref, scope_key = excluded.scope_key,
                    owner_id = excluded.owner_id, thread_id = excluded.thread_id,
                    meeting_id = excluded.meeting_id, start_sec = excluded.start_sec,
                    dim = excluded.dim, vector = excluded.vector,
                    text_sha256 = excluded.text_sha256, chunk_text = excluded.chunk_text,
                    created_at = excluded.created_at
                """,
                (
                    chunk.kind, chunk.ref_id, chunk.doc_ref, scope_key, doc.owner_id,
                    doc.thread_id, doc.meeting_id, chunk.start_sec, model, len(vec),
                    pack(vec), chunk.sha, chunk.text, now,
                ),
            )
        else:
            # Unchanged text: keep the vector, refresh where it lives (a note
            # that moved threads keeps its embedding but not its old address).
            conn.execute(
                "UPDATE search_embeddings SET doc_ref = ?, scope_key = ?, owner_id = ?, "
                "thread_id = ?, meeting_id = ?, start_sec = ? "
                "WHERE kind = ? AND ref_id = ? AND model = ?",
                (
                    chunk.doc_ref, scope_key, doc.owner_id, doc.thread_id, doc.meeting_id,
                    chunk.start_sec, chunk.kind, chunk.ref_id, model,
                ),
            )

    wanted = {(c.kind, c.ref_id) for c in chunks}
    for row in conn.execute(
        "SELECT id, kind, ref_id FROM search_embeddings WHERE scope_key = ? AND model = ?",
        (scope_key, model),
    ).fetchall():
        if (row["kind"], row["ref_id"]) not in wanted:
            conn.execute("DELETE FROM search_embeddings WHERE id = ?", (row["id"],))

    conn.execute(
        "UPDATE search_scopes SET embed_fp = ?, embed_model = ?, embedded_at = ? "
        "WHERE scope_key = ? AND source_fp = ?",
        (source_fp, model, now, scope_key, source_fp),
    )


async def embed_pass(db_path: Path | str | None = None, *, max_scopes: int = 10) -> dict:
    """Embed whatever up to ``max_scopes`` scopes are missing.

    Raises :class:`EmbeddingError` when the endpoint fails -- the indexer
    backs off on that -- after recording it for the status page. Returns
    ``{"enabled", "scopes", "embedded"}``; ``scopes == 0`` means caught up.
    """
    with get_conn(db_path) as conn:
        if not is_enabled(conn):
            return {"enabled": False, "scopes": 0, "embedded": 0}
        config = embeddings_svc.EmbeddingConfig.from_db(conn)
        model = config.model
        scopes = conn.execute(
            f"SELECT scope_key, source_fp {_pending_scopes_sql(False)} "
            "ORDER BY indexed_at LIMIT ?",
            (model, max_scopes),
        ).fetchall()

    embedded = 0
    for scope in scopes:
        scope_key, source_fp = scope["scope_key"], scope["source_fp"]
        with get_conn(db_path) as conn:
            docs = stored_docs(conn, scope_key)
            chunks = chunks_for_docs(docs)
            have = {
                (r["kind"], r["ref_id"]): r["text_sha256"]
                for r in conn.execute(
                    "SELECT kind, ref_id, text_sha256 FROM search_embeddings "
                    "WHERE scope_key = ? AND model = ?",
                    (scope_key, model),
                )
            }
            # A chunk that moved scopes (a moved note) is found by key instead.
            for chunk in chunks:
                key = (chunk.kind, chunk.ref_id)
                if key not in have:
                    row = conn.execute(
                        "SELECT text_sha256 FROM search_embeddings "
                        "WHERE kind = ? AND ref_id = ? AND model = ?",
                        (chunk.kind, chunk.ref_id, model),
                    ).fetchone()
                    if row is not None:
                        have[key] = row["text_sha256"]

        todo = [c for c in chunks if have.get((c.kind, c.ref_id)) != c.sha]
        fresh: dict[tuple[str, str], list[float]] = {}
        if todo:
            try:
                vectors = await _embed_all(config, [c.text for c in todo])
            except EmbeddingError as exc:
                _state["last_error"] = exc.message
                _state["last_error_at"] = utcnow()
                raise
            fresh = {(c.kind, c.ref_id): v for c, v in zip(todo, vectors)}
            embedded += len(fresh)

        with get_conn(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            _write_scope_vectors(
                conn,
                scope_key=scope_key,
                source_fp=source_fp,
                model=model,
                docs_by_ref={(d.kind, d.ref_id): d for d in docs},
                chunks=chunks,
                fresh=fresh,
            )

    if scopes:
        _state["last_error"] = None
        _state["last_success_at"] = utcnow()
    return {"enabled": True, "scopes": len(scopes), "embedded": embedded}


def prune_old_models(conn: sqlite3.Connection) -> int:
    """Drop vectors from a previous ``embedding_model`` -- but only once the
    current one covers everything, so a model change never leaves semantic
    search empty while the new vectors are still being computed."""
    if not is_enabled(conn):
        return 0
    model = current_model(conn)
    if pending_scope_count(conn, model) > 0:
        return 0
    return conn.execute("DELETE FROM search_embeddings WHERE model != ?", (model,)).rowcount


# --------------------------------------------------------------------------- #
# Query
# --------------------------------------------------------------------------- #


class SemanticUnavailable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def embed_query(conn: sqlite3.Connection, q: str) -> tuple[list[float], str]:
    """Embed the query, or raise SemanticUnavailable with a human reason."""
    if not effective(conn, "embedding_enabled"):
        raise SemanticUnavailable("Semantic search is turned off")
    config = embeddings_svc.EmbeddingConfig.from_db(conn)
    if not config.model:
        raise SemanticUnavailable("No embedding model is configured")
    if conn.execute(
        "SELECT 1 FROM search_embeddings WHERE model = ? LIMIT 1", (config.model,)
    ).fetchone() is None:
        raise SemanticUnavailable("Nothing has been embedded yet")
    try:
        vectors = embeddings_svc.embed_texts(config, [q])
    except EmbeddingError as exc:
        log.warning("search: query embedding failed, falling back to keyword: %s", exc.message)
        raise SemanticUnavailable(f"The embedding service is unavailable: {exc.message}") from exc
    if not vectors or not vectors[0]:
        raise SemanticUnavailable("The embedding service returned no vector")
    return normalise(vectors[0]), config.model


def semantic_candidates(
    conn: sqlite3.Connection,
    *,
    owner_id: int,
    query_vec: list[float],
    model: str,
    filter_sql: str,
    filter_params: list,
    limit: int,
    min_score: float,
) -> list[dict]:
    """Best-scoring documents, one entry per document (its best chunk wins).

    Ownership is in the WHERE clause on both tables, never a post-filter; the
    join through search_docs also hides vectors of anything deleted.
    """
    rows = conn.execute(
        f"""
        SELECT e.vector, e.dim, e.chunk_text, e.start_sec AS chunk_start,
               d.id AS doc_id
          FROM search_embeddings e
          JOIN search_docs d ON d.kind = e.kind AND d.ref_id = e.doc_ref
         WHERE e.owner_id = ? AND d.owner_id = ? AND e.model = ? {filter_sql}
        """,
        [owner_id, owner_id, model, *filter_params],
    )
    dim = len(query_vec)
    best: dict[int, tuple[float, str, float | None]] = {}
    for row in rows:
        if row["dim"] != dim:
            continue
        score = dot(query_vec, unpack(row["vector"]))
        if score < min_score:
            continue
        prev = best.get(row["doc_id"])
        if prev is None or score > prev[0]:
            best[row["doc_id"]] = (score, row["chunk_text"], row["chunk_start"])
    top = heapq.nlargest(limit, best.items(), key=lambda kv: kv[1][0])
    return [
        {"doc_id": doc_id, "score": s, "chunk_text": text, "chunk_start": start}
        for doc_id, (s, text, start) in top
    ]
