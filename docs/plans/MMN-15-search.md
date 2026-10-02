# MMN-15: FTS5 full-text index and embeddings-based semantic search (implementation plan)

**Status:** approved and implemented (2026-10-01). Blocks MMN-14.

**Reviewer decisions (Stone, 2026-10-01):** (1) The 60-minute periodic reconcile is **accepted** as the safety net. (2) The home bar's thread filter **switches to FTS** (see §10a). (3) CJK substring search (trigram index) is deferred to a **follow-up ticket**.

## 1. Summary

Add one search service, `app/services/search.py`, that covers everything a user owns. It returns
**keyword** hits from an FTS5 index and **semantic** hits from stored embeddings, and merges the two
with Reciprocal Rank Fusion. It is exposed as `GET /api/search`, adds a "Search everything" view to
the home search bar, and gets a Settings → Search status/rebuild page. The function signature is the
same one MMN-14's MCP `search` tool will call, so MMN-14 does not need its own search code.

Environment facts I checked: the bundled SQLite is 3.46.1 with FTS5. `services/embeddings.py`
already provides `embed_texts` (batched, ordered by `index`) and the fail-open contract. The
transcript page already supports `?t=` deep links (`TranscriptPage.tsx:585`), so a transcript hit
can link to `/meetings/{id}/transcript?t={start_sec}` with no new frontend plumbing.

## 2. New code

```
app/services/search_index.py   render docs per kind, fingerprint, upsert/delete FTS rows, reconcile
app/services/search_embed.py   chunking, embed backfill, vector pack/unpack, cosine over owner rows
app/services/search.py         query parsing/escaping, keyword(), semantic(), RRF hybrid search()
app/jobs/search_indexer.py     SearchIndexer: one always-started asyncio.Task (same shape as
                               AutoBackfillScheduler), drains the dirty set and embeds
app/routers/search.py          GET /api/search, GET /api/search/status, POST /api/search/rebuild (admin)
web/src/components/search/SearchResults.tsx   grouped-by-kind results view
web/src/components/settings/SearchIndexPanel.tsx   status + rebuild (mirrors EmailBackfillPanel)
tests/test_search_*.py, web/src/components/search/__tests__/SearchResults.test.tsx
```

## 3. Schema (`db.py`, in `SCHEMA`)

```sql
-- One row per indexed unit. Plain table = the source of truth for "what is indexed, and as of what".
CREATE TABLE IF NOT EXISTS search_docs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,   -- = search_fts.rowid
    kind        TEXT NOT NULL,      -- thread|meeting|segment|summary|action_item|note|email|event
    ref_id      TEXT NOT NULL,      -- source row id; for segment "{diarization_id}:{seg_idx}"
    owner_id    INTEGER NOT NULL,
    thread_id   INTEGER,
    meeting_id  INTEGER,
    start_sec   REAL,
    title       TEXT,
    date        TEXT,               -- ISO-8601, used by since/until and the result card
    fingerprint TEXT NOT NULL,      -- sha256 of rendered title+body+placement
    indexed_at  TEXT NOT NULL,
    UNIQUE(kind, ref_id)
);
CREATE INDEX idx_search_docs_owner ON search_docs(owner_id, kind);
CREATE INDEX idx_search_docs_thread ON search_docs(thread_id);
CREATE INDEX idx_search_docs_meeting ON search_docs(meeting_id);

CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    kind UNINDEXED, ref_id UNINDEXED, owner_id UNINDEXED, thread_id UNINDEXED,
    meeting_id UNINDEXED, start_sec UNINDEXED, title, body,
    tokenize = 'unicode61 remove_diacritics 2'
);   -- rowid = search_docs.id

CREATE TABLE IF NOT EXISTS search_embeddings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    ref_id      TEXT NOT NULL,      -- chunk key: e.g. "summary:42#p3", "transcript:17#w5"
    owner_id    INTEGER NOT NULL,
    thread_id   INTEGER, meeting_id INTEGER, start_sec REAL,
    model       TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    vector      BLOB NOT NULL,      -- array('f') float32, little-endian, L2-normalised at write
    text_sha256 TEXT NOT NULL,
    chunk_text  TEXT NOT NULL,      -- used as the semantic snippet
    created_at  TEXT NOT NULL,
    UNIQUE(kind, ref_id, model)
);
CREATE INDEX idx_search_emb_owner ON search_embeddings(owner_id, model);

CREATE TABLE IF NOT EXISTS search_dirty (       -- the work queue for the indexer task
    kind TEXT NOT NULL, ref_id TEXT NOT NULL, queued_at TEXT NOT NULL,
    PRIMARY KEY (kind, ref_id)
);
```

Why `search_docs` is separate from the FTS table: FTS5 cannot index or `UNIQUE` an UNINDEXED column,
so `owner_id = ?`, `thread_id` cascades and "is this row stale?" would each be a full scan. The
FTS table keeps the column set the ticket specifies, so a MATCH query can return everything without
a join, and `search_docs` gives the B-tree indexes and fingerprints. Every write goes through
`search_index.upsert_doc` / `delete_doc`, which updates both tables in one transaction. The ticket's
"rebuild from scratch" is `DELETE FROM search_docs; DELETE FROM search_fts;` followed by a reconcile.

## 4. What gets indexed (renderers in `search_index.py`)

| kind | ref_id | title | body | date |
|---|---|---|---|---|
| thread | threads.id | title | description | updated_at |
| meeting | meetings.id | title | (empty) | meeting_at ∥ created_at |
| segment | `{diar_id}:{idx}` of the **active** diarization | "{speaker} @ mm:ss" | text, speaker via `transcript.build_transcript` (merged_into + display_name honoured; non-speech markers skipped) | meeting date |
| summary | **current** summaries.id | title_suggestion ∥ meeting title | tldr, summary_md, decisions, topics, open questions | created_at |
| action_item | action_items.id (current summary only) | owner_label | text + due_text | created_at |
| note | thread_notes.id | title | body | updated_at |
| email | thread_emails.id | subject | sender, snippet, ai_summary, stored body (already text via html_text) | normalised date |
| event | thread_calendar_events.id | summary | description, location | start_at |

The renderers reuse `transcript.build_transcript`, so speaker names are rendered by the same code
the transcript page uses and cannot drift from it. They only **read**. Nothing writes
`raw_json`, `updated_at`, `seen_at`, or triggers hydration. Non-current summaries and their action
items are removed from the index; superseded diarizations are as well.

## 5. Keeping the index in sync

The ticket asks for "service layer on every write path, not triggers". I counted about 30 write sites
across 12 files (threads, meetings, transcripts routers; notes, summarize, pipeline, matching,
email_bodies, groups services). The design keeps each call site to a single line, and adds a
safety net so a missed call site cannot leave the index permanently stale:

1. **`search_index.mark_dirty(conn, kind, ref_id)`**. This is an `INSERT OR REPLACE` into
   `search_dirty` inside the caller's existing transaction, so it commits or rolls back with the
   write itself. Indexing does not happen on the request path, so a slow re-render of a 900-segment
   transcript never adds latency to a speaker rename.
   Convenience helpers:
   `mark_meeting_dirty(conn, meeting_id)`, which covers the meeting, its segments, current summary
   and action items, and `mark_thread_dirty(conn, thread_id)`.
2. **Deletes are synchronous.** `delete_for_thread(conn, thread_id)` and `delete_for_meeting(...)`
   are called right before the existing `DELETE FROM threads/meetings`, because cascades never reach
   a virtual table. Single-row deletes (email/event/note detach) call `delete_doc`. A delete must
   never wait on the background task: a deleted note should stop matching immediately.
3. **Moves** (`threads.py:524` meeting move, `notes.py:235` note move) mark the moved object dirty.
   `thread_id` is part of the fingerprint, so the re-index rewrites placement.
4. **Speaker rename/merge/is_me** (`routers/transcripts.py:235-295`) calls `mark_meeting_dirty`.
   The segments are re-rendered from `speaker_map`. `diarizations.raw_json` is never touched, and a
   byte-equality test asserts that.
5. **Safety net: `reconcile(conn, owner_id=None)`.** For each kind it compares a cheap source-side
   fingerprint against `search_docs`. For speed it uses `updated_at`/`attached_at`/`body_fetched_at`
   plus the speaker_map `max(updated_at)`. It upserts what changed and deletes orphans, i.e. docs
   whose source row is gone. It runs once at startup (in the background task, not blocking boot),
   on "Rebuild index", and every `search_reconcile_interval_minutes` (default 60; approved by the
   reviewer). This is the
   "idempotent backfill … recorded by a per-row fingerprint" the ticket asks for, and it covers
   existing data on first deploy.

`SearchIndexer` loop: drain `search_dirty` in batches of 200 (one `to_thread` per batch, delete the
dirty rows only after a successful upsert, so a crash just retries). Then, if
`embedding_enabled`, run one embedding pass (section 6). Sleep 2s when idle, with an
`asyncio.Event` nudge from `mark_dirty` so a new note becomes searchable within about a second.
There is no job-queue entry, for the same reasons as email hydration.

## 6. Semantic layer

* **Chunking** (`search_embed.chunks_for(doc)`): transcripts use a sliding window over the rendered
  segments. Each window holds up to 8 segments or about 800 chars, overlaps the previous one by 2
  segments, and carries its first segment's `start_sec` and the meeting/thread ids. Summary, note,
  and email-body chunks split on blank lines and pack paragraphs up to 1,200 chars (a hard split for
  one oversized paragraph). Thread, meeting, action_item and event docs are one chunk each.
* **Backfill** (`embed_pending(conn, limit)`): candidates are the chunks whose `(kind, ref_id,
  current model)` row is missing or whose `text_sha256` differs. They are embedded in batches of 32
  via `embeddings.embed_texts` inside `asyncio.to_thread`, with at most 2 batches in flight
  (semaphore). Rows are upserted, and chunk rows that no longer exist for a doc are deleted.
  Unchanged text is never re-sent. On `EmbeddingError` it logs, backs off (exponential, capped at
  10 min), and leaves state untouched, so the next pass resumes from the same predicate.
* **Model change:** queries and backfill filter `model = current embedding_model`. Rows under an old
  model are never mixed in, and the reconcile pass deletes them once the new model reaches 100%
  coverage, so a model flip does not leave semantic search empty in the meantime.
* **Query:** embed the query once. Load `(id, vector)` for `owner_id = ? AND model = ?`, with the
  optional kind/thread/date filters in SQL. Score with a pure-Python dot product. Vectors are
  normalised at write, so cosine = dot. `array('f')` plus `sum(map(operator.mul, …))` is used, with no
  numpy. Results under `embedding_min_score` are dropped and the top-K is kept with `heapq.nlargest`.
  **Scale note (documented in CLAUDE.md):** at 1,024 dims this is about 1 ms per 100 rows in CPython,
  so it stays under about 250 ms up to roughly 25k chunks per owner. Above that, the next step is a
  `sqlite-vec` index or numpy. The status page shows the per-owner chunk count so this is visible.
* **Fails open:** if `embedding_enabled` is off, the endpoint errors, or there are zero rows under
  the current model, `semantic` returns `[]`, `hybrid` degrades to keyword, and the response carries
  `"semantic": {"available": false, "reason": …}`. `mode=semantic` returns the same empty list plus
  the reason, not a 5xx. No new endpoint settings: the embedding base URL, key, model and timeout
  are reused as they are.

## 7. Query parsing and keyword search

`search.to_match_expr(q)` is a small tokenizer, not regex escaping:
* `"exact phrase"` becomes an FTS phrase. An unbalanced quote closes at end of input.
* `term*` becomes the prefix `"term"*` (at least 2 chars before `*`, otherwise the `*` is dropped).
* Every other token is wrapped as `"tok"` with internal `"` doubled, so `-`, `:`, `(`, `NEAR`, `OR`
  and `AND` are literals. Bare multi-word queries mean an implicit AND.
* Tokens that are all punctuation or emoji (which unicode61 drops) are discarded. If nothing is left,
  the result is empty, not a MATCH error.
* CJK: unicode61 does not segment Han text, so one CJK run is one token. A substring of a run will
  not match. This is a known limitation, and the reviewer deferred it: a follow-up ticket will add
  a `trigram` side index. I'll file it when this ticket lands.

Keyword query:
```sql
SELECT f.rowid, f.kind, f.ref_id, f.thread_id, f.meeting_id, f.start_sec,
       highlight(f, 6, '\x02', '\x03') AS title_hl,
       snippet(f, 7, '\x02', '\x03', '…', 16) AS snip,
       bm25(f, 0,0,0,0,0,0, 4.0, 1.0) AS rank, d.date, d.title
FROM search_fts f JOIN search_docs d ON d.id = f.rowid
WHERE search_fts MATCH ? AND d.owner_id = ?  [AND d.kind IN (...)] [AND d.thread_id = ?]
      [AND d.date >= ?] [AND d.date < ?]
ORDER BY rank LIMIT ?
```
The title weight is 4.0. The sentinel bytes `\x02`/`\x03` are turned into `<mark>` spans by the SPA
after HTML-escaping, so no server-produced HTML is trusted. Ownership is in the WHERE clause, never
a post-filter.

## 8. Hybrid and response shape

`search(conn, user, q, kinds=None, since=None, until=None, thread_id=None, mode='hybrid', limit=20,
offset=0) -> dict`
* Each arm fetches `offset + limit + 50` candidates. RRF score is `Σ 1/(60 + rank)` per arm. Semantic
  chunk hits collapse onto their doc key (the transcript window maps to its first segment's doc, and
  paragraph chunks map to the parent), so a doc that matches both ways appears once with
  `matched_by: ["keyword","semantic"]`.
* Hit: `{kind, id, thread_id, thread_title, meeting_id, start_sec, title, snippet, date, score,
  matched_by}`. `id` is the source row id (for a segment, `meeting_id` + `start_sec` are the useful
  handles). There is also a `url` field: the SPA deep link, which the MCP tool (MMN-14) can pass through.
* Response: `{hits, total_estimate, mode_used, semantic: {available, reason}}`.

## 9. API

| Route | Notes |
|---|---|
| `GET /api/search?q=&kinds=a,b&since=&until=&thread_id=&mode=&limit=&offset=` | `active_user`. `q` is 1–500 chars. `limit` ≤ 100. A `thread_id` owned by someone else returns **404** (per the conventions). Unknown kinds return 422. |
| `GET /api/search/status` | Per-kind `{indexed, pending}` for the caller, plus embedding coverage `{chunks, embedded, model, enabled}`. Admins also get global totals. |
| `POST /api/search/rebuild` | `require_admin`. It truncates both indexes and kicks the indexer. It returns immediately, and the panel polls status. |

## 10. UI

### 10a. Thread filter on FTS (reviewer decision 2)

`threads.list_threads(q=…)` replaces `title LIKE ? OR description LIKE ?` with
`t.id IN (SELECT d.thread_id FROM search_fts f JOIN search_docs d ON d.id = f.rowid
WHERE search_fts MATCH ? AND d.kind = 'thread' AND d.owner_id = ?)`. The match expression comes from
the same `to_match_expr`, so word splitting, implicit AND, phrases, `prefix*` and diacritic folding
behave the same as "Search everything". What stays the same: the scope (thread title + description
only), the sort order (last activity), per-group paging (`?group=`), and the URL/filter behaviour.
The filter narrows the list; it does not re-rank it.

Two details make this safe to switch:
* **Thread docs are indexed synchronously** on create and update (one row, cheap), rather than
  through the dirty queue. A thread you have just created or renamed is found immediately; the
  ~1s queue lag is not acceptable for the one filter people use straight after typing a title.
* **Fallback:** if `to_match_expr` leaves nothing (input of only punctuation or emoji), or the
  index has no thread docs for this owner yet (the first boot before reconcile finishes), it falls
  back to the old `LIKE`. That way the filter never goes blank during rollout.

Behaviour change to note in CLAUDE.md: `meet` no longer matches `meeting` (use `meet*`), and
mid-word substrings no longer match. Tests: word-order independence (`budget q3` ↔ `Q3 budget`),
diacritics, prefix, the punctuation-only fallback, the empty-index fallback, grouped paging still
counting correctly with `q`, and owner isolation through the subquery.

### 10b. Search everything

* **Home bar** (`ThreadsPage.tsx`): it keeps filtering the thread list (now via FTS, §10a). A "Search
  everything for "q"" row appears under the input (and on Enter), which opens `?search=q` on the
  home route. That route renders `SearchResults` in place of the grouped list, so a results link can
  be shared. Results are grouped by kind in a fixed order (Transcripts, Summaries, Notes, Emails,
  Events, Action items, Meetings, Threads), and each group shows 5 hits with "Show more". A transcript
  hit links to `/meetings/{id}/transcript?t={start_sec}`, a note/email/event to its thread page, and
  a summary to the meeting's summary tab. A small "keyword only" hint is shown when
  `semantic.available` is false. Colour tokens follow the conventions: snippet `--fg-muted`, meta
  `--fg-subtle`, `<mark>` with an `*-ink` token.
* **Settings → Search** (`/settings/search`, nav entry beside Email backfill): a table of per-kind
  indexed/pending counts, an embedding coverage bar (with "Off" when `embedding_enabled` is false),
  the chunk count with the scale note, and an admin-only "Rebuild index" button.

## 11. Must-nots (each has a test)

* `matching.attached_context` and the summarizer do not import or read any search table. The
  existing "returns only events and emails" test is extended to assert this.
* No `threads.updated_at` bump and no `seen_at` write. A test indexes a thread and compares the
  timestamps before and after.
* No hydration. The indexer reads `thread_emails.body` as stored. A test asserts that no provider
  `get_email_body` is called and that `body_fetched_at` is unchanged.

## 12. Tests (all offline; respx for `/embeddings`)

* `test_search_index.py`: for each of the 8 kinds, create → findable, update → old text gone and
  new text findable, delete → gone. Thread delete cascades all its docs, a meeting move rewrites
  `thread_id`, re-summarise drops the old summary and action items, and `reconcile` repairs a
  hand-corrupted index and deletes orphans.
* `test_search_speakers.py`: renaming a speaker re-indexes the segments (new name findable, old one
  gone), a merge is honoured, and `raw_json` stays byte-identical.
* `test_search_query.py`: an escaping fuzz over `" - * ( ) : NEAR OR AND ^ 🙂`, CJK, empty input, and
  lone `*`, using Hypothesis-style generated strings (or a parametrised corpus if hypothesis is not
  in deps) asserting no `OperationalError`. Also implicit AND, phrase, prefix, and diacritics
  (`café` ↔ `cafe`).
* `test_search_owner.py`: user B never sees A's docs in keyword or semantic results, and A's
  `thread_id` returns 404 for B.
* `test_search_rank.py`: the bm25 title boost puts a title hit above a body hit, RRF ordering is
  checked against a hand-computed fixture, and dual-matched docs are deduped.
* `test_search_embed.py`: backfill embeds missing rows, unchanged text sends no request on the
  second pass (respx call count), edited text re-embeds one chunk, a model change hides old vectors,
  and a respx 500 means hybrid returns keyword hits with `semantic.available == false`, as does
  disabled.
* `test_search_api.py`: the `/api/search` contract (shape, validation, pagination), status, and
  rebuild requiring admin.
* vitest `SearchResults.test.tsx`: grouping order, the transcript deep link carrying `?t=`, the
  `<mark>` rendering escaping HTML in snippets, and the "keyword only" hint.
* `./test.sh` passes in full.

## 13. Docs

A new CLAUDE.md section "Search & indexing" covers why `search_docs` sits beside the FTS table,
dirty-set and reconcile, synchronous deletes, the MATCH escaping rules, the CJK limitation, the
brute-force scale ceiling, and fail-open. There will also be a new Confluence page under the MMN
tree, **Search & Indexing**, and a cross-link from "Architecture Overview".

## 14. Rollout and risk

* First boot after deploy: the reconcile backfills everything in the background. On the production
  db (a few hundred meetings), FTS should finish in seconds. Embedding takes minutes, depends on
  the endpoint, and is resumable.
* Index size: segments are the bulk. Expect about 1.5–2× the raw transcript text for FTS, plus
  roughly 4 KB per chunk for 1,024-dim vectors.
* If a call site is missed, the result is staleness until the next reconcile (≤ 60 min), never wrong
  ownership, because ownership is resolved from the source rows at index time.

## 15. Reviewer decisions (resolved)

1. Periodic reconcile every 60 min: **yes**.
2. Home thread filter moves to FTS: **yes**, see §10a.
3. CJK trigram index: **follow-up ticket**.
