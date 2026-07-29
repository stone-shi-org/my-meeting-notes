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
                 calendar settings_api system
  services/      audio diarize llm prompts summarize transcript matching upcoming pipeline
                 threads users integrations secretstore mcpclient followups
  services/providers/   base registry loader tokens oauth query · google mcp   <- one file per backend
  jobs/          queue.py (asyncio pool) · registry.py (stages + weights)
                 scheduler.py (the auto-match timer)
  prompts/       summary_prompt.md · match_rank_prompt.md   <- EDITABLE, no deploy needed
web/src/         types api lib hooks player components pages routes
  lib/recording.ts + hooks/useRecorder.ts   <- what each browser can capture, and the capture itself
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

**An all-day event's bare date is UTC midnight to `new Date()`.** `new Date('2026-07-30')` parses
date-only strings as UTC, so west of Greenwich every all-day event renders a day early. `lib/calendar.ts`
builds those in local time (`eventDate`) and everything on the upcoming list goes through it — the
frontend half of the same trap `_caldav._stamp` guards on the server.

**Gmail returns RFC 2822 timestamps.** Stored raw they sort lexically above every ISO date, so a
July 15 email lands above a July 20 meeting on the timeline. `matching.normalize_timestamp` coerces
on write, and the timeline sort normalises again for older rows.

**`navigator.mediaDevices` is *undefined* on a plain-HTTP LAN address** — the whole object, not just
a failing call, because it is gated on a secure context. `MediaRecorder` is **not** gated and is
still there, so feature-detecting on it alone reports a working recorder that throws "Cannot read
properties of undefined (reading 'getUserMedia')" on the first click. `hasMediaDevices` is the check
that matters, and `blockedReason` names the *origin* as the problem: told "unsupported", people go
and try a different browser. `http://localhost:4020` is a secure context; `http://192.168.1.20:4020`
is not.

**An AudioContext runs on the audio hardware's clock, not the wall clock.** With no usable output
device it renders *behind* real time — measured at 2.5s per 3s on a box with no sound card — so
anything recorded through the graph comes out silently time-compressed. `useRecorder` therefore
hangs the analyser off the sources as a tap and hands `MediaRecorder` the **raw track**, building a
mixed stream only when the user asked to mix their mic into a capture. Do not "simplify" it back to
one uniform graph.

**MediaRecorder writes WebM with no duration**, because a stream does not know how long it will be.
ffprobe reports nothing for the source file, so `_convert_stage` re-probes the converted wav and
backfills `audio_duration_sec`; without that every browser recording has a NULL length in the
player, the meeting card and the diarizer's progress estimate.
`tests/fixtures/browser_recording.webm` is a real one (`ffmpeg -f webm -live 1`), and a test asserts
it still declares no duration — the backfill test is vacuous the day that changes.

**A display capture stream carries a video track.** Handing it to `MediaRecorder` under an `audio/*`
mimeType is a `NotSupportedError`, so the recorder wraps the audio tracks in a fresh `MediaStream`.
The video track is deliberately left running: stopping it ends the share, and Chrome ends the audio
with it.

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

### Upcoming events (the other direction)

`services/upcoming.py` + `routers/calendar.py` run the match pipeline backwards: the home screen
lists the next fortnight across every calendar, and creating a meeting from one event prefills title,
time and speaker names and attaches the event in the same transaction. It reuses `matching`'s
`dedupe_events`, `aggregate_error` and `attach_event` on purpose — three rules that must not drift
between the two entry points, the last one because a column added to one INSERT and not the other is
how `attached_context` starts feeding the summarizer NULLs.

`MAX_DAYS` is **30, not 31**: the window starts at midnight this morning, so asking for 31 would push
past Zoho's hard 31-day range cap and fail that one account.

The create route takes the **event payload back from the client**. There is nothing to look up —
the listing is not persisted and no provider offers fetch-by-uid — and it is safe because
integrations are per-user: a forged payload only writes a meeting onto the caller's own thread with
a title they could have typed. Field lengths are bounded so it cannot become a way to write
megabytes into `raw_json`.

**`EventCandidate.attendees` is what "prefill the speakers" runs on.** Display names, organizer
first, rooms and decliners dropped. Google and CalDAV map their documented shapes explicitly;
Zoho and MCP go through `base.coerce_attendees`, which drops an unrecognised entry rather than
failing the search. `attendee_label` unpacks `first.last@` into "First Last" but leaves a
separator-less local part alone — "jsmith" is not improved by becoming "Jsmith".

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

### Apple

No OAuth exists for iCloud — Apple ID plus an app-specific password is the only route, and it is the
overwhelmingly common support question when someone uses their account password instead.

CalDAV needs **two** discovery hops: `PROPFIND` for `current-user-principal`, then `PROPFIND` for
`calendar-home-set`, which points at a *different* shard host (`p34-caldav…`). Everything after that
goes to the shard.

**Recurrence is expanded locally, on purpose.** iCloud's `<C:expand>` support is erratic — the same
request has been reported returning expanded instances sometimes and the bare master other times.
What iCloud does honour is RFC 4791 §9.9: a `time-range` filter is evaluated against the *expanded*
set, so a series whose master `DTSTART` is a year earlier still comes back, just unexpanded with its
`RECURRENCE-ID` overrides alongside. `recurring-ical-events` does the expansion, including in the
event's own TZID — expanding in UTC drifts every instance by an hour after a DST change.

`tests/fixtures/icloud_recurring.ics` is the highest-value fixture in the suite: a weekly series
starting six months before the window, one `EXDATE`, one moved `RECURRENCE-ID` override, and an
all-day `VALUE=DATE` event. All-day events keep a bare date — coercing to midnight puts them on the
wrong day.

IMAP is stdlib `imaplib` behind `asyncio.to_thread` (it blocks), opened read-only so searching never
marks mail as read. `OR` in IMAP SEARCH is **binary and prefix**, so three terms nest as
`OR t1 (OR t2 t3)`; a flat `OR a b c` is not an error, it just returns the wrong set.

### Zoho

Three quirks, all of which fail quietly rather than loudly:

- The auth header is `Zoho-oauthtoken`, **not** `Bearer`.
- Mail has no `me` alias; every path needs a numeric `accountId` fetched first.
- `Accept: application/json+large` is required or event descriptions come back empty.

`range` is mandatory on the events call and capped at 31 days — we clamp with a `ValidationError`
rather than chunking, since the default window is ten days. Zoho is regional (`.com`/`.eu`/`.in`/
`.com.au`/`.jp`) and the wrong data centre authenticates fine and returns nothing, so the DC is
pinned onto the integration row at connect time. Mail search takes only an upper time bound, so the
lower edge of the window is enforced client-side.

Watch `parse_stamp`: an all-day `20260318` is *all digits*, so it must not be mistaken for an epoch
millisecond stamp — that bug lands the event in 1970.

### Automatic follow-ups (the sweep)

`jobs/scheduler.py` ticks every minute and hands due threads to `services/followups.py`, which is
the same match pipeline pointed at a thread instead of a meeting. Off unless
`auto_match_enabled` is set. Everything it does that is *not* obvious:

**It attaches with `meeting_id = NULL`, deliberately.** `attached_context` is scoped to one meeting
and is what the summarizer reads. An item nobody confirmed must never become an input to the next
summary of a meeting that has already been written up — so a sweep result belongs to the thread
until a human moves it.

**`rank_sync` returning `relevance_score = None` is the safety interlock, not a degradation.** None
never clears the threshold, so "the LLM is down" and "attach nothing" are the same code path. Do not
"fix" this by falling back to the keyword score.

**Only the sweep can create an unread row.** `seen_at` is stamped at write time for anything a
person attached (`matching._unread_flags`), so `auto_attached = 1 AND seen_at IS NULL` is the whole
definition of unread and the thread's `unread_count` is derived from it. `mark_seen` only ever
writes `seen_at` where it is still NULL: re-reading must not move the timestamp.

**Threads are swept sequentially and stamped even on failure.** Concurrency here competes with the
interactive match the user is waiting on, for work nobody is waiting on; and a thread whose provider
is down still gets `auto_match_at` written, or it is due again on every tick and one broken account
becomes a request storm.

**It does not use the job queue.** A sweep per thread per half hour would bury the user's own
uploads in the progress dock, and restart survival — the other reason to use the queue — buys
nothing for work that is due again in thirty minutes anyway.

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
  `DIARIZATION_UNREACHABLE`, `LLM_AUTH_FAILED`, `NO_INTEGRATIONS`, `NEEDS_REAUTH`, `provider_error`
  and `MCP_TIMEOUT` make the SPA deep-link to the relevant settings tab. A failing account raises
  `ProviderError` (with `kind`); `MCPError` is only for the MCP transport, since a CalDAV problem
  reported as `mcp_error` misleads both the user and whoever debugs it next.
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
