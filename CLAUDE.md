# CLAUDE.md

Working notes for this codebase. Read alongside `README.md`, which covers running it.

## Shape

FastAPI backend + Vite/React SPA, one container, port 4020. SQLite at `data/app.db`, audio on disk
at `data/audio/<meeting_id>/`. No ORM — raw `sqlite3` and hand-written SQL, matching the other
services in `~/src`.

```
app/
  config.py      Settings (pydantic-settings) + effective(): DB row overrides .env
  db.py          THE WHOLE SCHEMA in one idempotent init_db()
  deps.py        current_user / active_user / require_admin / owner_scope
  security.py    stdlib scrypt + opaque session tokens
  routers/       auth users integrations threads meetings transcripts summaries matching jobs
                 settings_api system
  services/      audio diarize llm prompts summarize transcript matching pipeline threads users
                 integrations secretstore mcpclient
  services/providers/   base registry loader tokens oauth query · google mcp   <- one file per backend
  jobs/          queue.py (asyncio pool) · registry.py (stages + weights)
  prompts/       summary_prompt.md · match_rank_prompt.md   <- EDITABLE, no deploy needed
web/src/         types api lib hooks player components pages routes
```

## Things that will bite you

**`include_text=true` on the diarization request.** Omit it and the service returns speaker turns
with no words in them. `diarize.py` raises a named error for this exact symptom because the failure
otherwise looks like "the model is broken".

**`stream: false` AND `include_reasoning: false` on every LLM call.** The omniroute proxy streams
SSE by default, which dies inside `.json()`; without `include_reasoning: false` the model spends its
whole budget on a thinking trace and returns empty content. Both are hardcoded in `llm.py`, not
caller-supplied, and `test_llm.py` asserts they reach the wire.

**FastMCP returns one `content[]` block per list item**, each a standalone JSON object — *not* one
block holding a JSON array. Reading `content[0]` as the list silently yields exactly one result.
See `mcpclient.parse_tool_result`.

**The two search date formats differ.** Calendar wants ISO-8601 (`2026-03-11`); Gmail wants
`after:2026/03/11`. Cross-wiring them returns nothing, silently. Separate helpers, separate tests.

**Gmail returns RFC 2822 timestamps.** Stored raw they sort lexically above every ISO date, so a
July 15 email lands above a July 20 meeting on the timeline. `matching.normalize_timestamp` coerces
on write, and the timeline sort normalises again for older rows.

**`diarizations.raw_json` is never UPDATEd.** Speaker names live in `speaker_map` and are applied at
render time in `transcript.py`. A byte-equality test guards this.

**MCP errors arrive wrapped in an anyio `ExceptionGroup`** whose own message is
"unhandled errors in a TaskGroup (1 sub-exception)". `MCPClient._describe` unwraps it; without that
the Test connection button reports nothing useful.

**A second `TestClient` created outside a `with` block gets its own event loop.** Its requests then
enqueue onto an `asyncio.Queue` whose waiter belongs to a different loop, and background jobs sit
queued forever. `tests/conftest.py` shares one client and authenticates alternate users with a
Bearer header instead.

## Integrations (calendar + email)

Per-user, never shared. `app/services/providers/` holds one module per backend behind the protocol
in `base.py`; `matching.py` talks to exactly one thing — `providers.loader.load_for_user` — so adding
a provider means writing a module and a registry entry and touching nothing in the pipeline.

Providers take **structured intent** (keywords + a window) and build their own native query. The old
design handed everyone a pre-built Gmail query string, which cannot work for IMAP or Zoho. Results
come back as frozen dataclasses, not free-form dicts: candidates get persisted three times per run
(`candidates_json`, `ranked_json`, `raw_json`), so a raw provider payload would be stored three times
over, and the fixed field set is what guarantees `attached_context` — and therefore the summarizer —
always sees populated columns.

**`uid` is app-owned, not the provider's.** Google with `singleEvents=true` returns the *same*
`iCalUID` for every occurrence of a recurrence, and CalDAV shares one `UID` across a recurrence set,
so keying on it collapses a weekly standup into one candidate. `uid` is
`{provider}:{integration_id}:{instance}`; the provider's own series identity lives in `source_uid`,
which is what cross-provider dedup keys on. Same rule for `message_id` vs `rfc_message_id`.

**The MCP adapter is the deliberate exception** — it emits `uid`/`message_id` verbatim. Everything
attached before the migration is stored under the bare MCP uid, so namespacing it would make each one
fail the already-attached check and re-attach as a duplicate.

**Aggregate errors only when every account of a kind failed.** `match_runs.calendar_error` /
`email_error` are derived; per-account detail is in `source_errors_json`. Setting the aggregate on any
single failure would put a warning banner on a healthy search, more often the more accounts you add.

Credentials are Fernet-encrypted (`services/secretstore.py`). Refresh has exactly one owner
(`providers/tokens.py`): per-integration `asyncio.Lock`, a double-checked re-read inside it, a DB
lease, and a `secret_version` CAS on write-back — **if the CAS is lost the new token is discarded**,
because writing it would orphan whichever token the database already holds.

### MCP servers

Still supported, now as two providers (`mcp_calendar`, `mcp_email`) configured per user.
`services/mcpclient.py` is only the wire protocol; which server and whose account comes from the
integration row.

| | URL | Tool |
|---|---|---|
| calendar | `http://calendar-mcp.internal.example:4006` | `search_events` (ISO-8601 dates) |
| email | `http://email-mcp.internal.example:4003` | `search_emails` (Gmail query syntax) |

`:4004` is **arr-mcp**, not calendar — and the calendarmcp build on `:4006` has no `/version` route
and 401s without a token, so a port scan looking for `/version` misidentifies it.

stdio is implemented and tested but **cannot be selected inside the container**: `calendarmcp/venv`
pins the host's `/usr/bin/python3.14`, which the image does not have. Host-side debugging only.

Handshake, if you need to reproduce it by hand: `GET {base}/sse` → first event is
`event: endpoint` / `data: /messages/?session_id=…`; POST JSON-RPC there; replies come back on the
SSE stream, not the POST response.

### Google

`gmail.readonly` is a **restricted** scope and `gmail.metadata` is not a lighter substitute — it
forbids the `q` parameter the whole feature depends on. An OAuth app left in *Testing* status issues
refresh tokens that expire after **7 days**; setting the consent screen to "In production"
(unverified is enough) is the fix, and it is an operator action the code cannot enforce — hence
first-class `reauth_required` handling.

Gmail is an N+1: `messages.list` returns bare ids. Bound it at the *list* call (`maxResults`), fetch
`format=metadata` with a `fields` mask, and cap concurrency at 8.

## Jobs

In-process `asyncio.Queue` plus N workers, started in the FastAPI lifespan. No broker: Redis would
mean a second container and a second failure mode for a single-box app.

Blocking work goes through `asyncio.to_thread` — ffmpeg, the diarization POST, every sqlite write —
so the event loop stays free for the progress endpoints the SPA polls every two seconds.

Stages are checkpointed: each one first asks whether its output already exists. That is what lets
`recover()` re-queue a job after a restart without re-spending minutes on the GPU. An interrupted
diarization is genuinely lost (the service has no job handle) and does re-run; everything else skips.

Progress is polled via `GET /api/jobs/{id}/events?after_id=` — works through every proxy. SSE at
`/stream` is an opt-in upgrade with automatic client-side fallback.

## Conventions

- Timestamps: ISO-8601 UTC `TEXT` via `db.utcnow()`. Never `datetime.now()` bare.
- Errors: subclass `AppError`; everything renders as `{"error": {"code", "message"}}`.
  `DIARIZATION_UNREACHABLE`, `LLM_AUTH_FAILED` and `MCP_TIMEOUT` make the SPA deep-link to the
  relevant settings tab.
- Ownership: `owner_scope()` in every list query, `assert_can_access()` on every object route.
  Someone else's row is **404, not 403** — a 403 confirms it exists.
- Secrets are masked to `••••1234` on read; sending a masked value back means "leave unchanged".
- Prompts: substitute with `str.replace("{{key}}", v)`, **never** `str.format` — transcripts are
  full of literal braces.
- Colour: only `--fg`, `--fg-muted`, `--fg-subtle` and the `*-ink` tokens may carry text.
  `--fg-faint` and the bare status/speaker marks are decoration and fail AA by design.

## Tests

`./test.sh` runs both suites and writes JUnit XML for Bamboo. Deliberately no `set -e`: a failing
run must still leave its report on disk, or Bamboo says "no test results" instead of naming the
failure. `test-reports/` is wiped first, because Bamboo fails builds on stale result files.

Everything network-facing is faked at two seams: `respx` for `httpx` (diarizer, LLM, Google, and
CalDAV once it lands — respx routes arbitrary methods, which is why CalDAV must go over httpx rather
than the `requests`-based `caldav` package) and a monkeypatched provider. The suite runs offline.

Provider fakes go in at `providers.loader.load_for_user`, one level above the transport — so
`test_matching.py` describes "a calendar account failed" rather than "an SSE handshake failed" and
stays true whichever backend the account uses. `test_providers_loader.py` deliberately fakes lower,
at `MCPClient`, to exercise the real loader → registry → provider → config chain.

`tests/fixtures/diarization_sample.json` is a real captured response — 79 segments, two speakers,
and three non-speech markers (two `[Music]`, one `[Environmental Sounds]`).
