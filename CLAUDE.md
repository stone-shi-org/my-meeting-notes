# CLAUDE.md

Working notes for this codebase. Read alongside `README.md`, which covers running it.

## Documentation

This file (and README.md) is also mirrored, expanded, and organized by topic on Confluence —
space key **MMN** (`https://confluence.local.shifamily.com/spaces/MMN/`). Page tree: My Meeting
Notes → Getting Started, Architecture Overview, Meetings & Transcription Pipeline, Summaries & AI
Chat, Threads/Groups & Notes, Integrations Overview & Matching → Provider Details, Email
Conversations & Hydration, Search & Indexing, Testing, CI/CD & Release (Bamboo), Gotchas Reference.

**Whenever something in this file changes, update the matching Confluence page(s) too.** This file
is the source of truth for day-to-day agent work; Confluence is the durable, browsable copy for
humans. Letting them drift apart defeats the point of having both.

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
                 calendar settings_api system notes search api_tokens
  services/      audio diarize llm prompts summarize transcript matching upcoming pipeline
                 threads users integrations secretstore mcpclient followups notes
                 search search_index search_embed api_tokens chat_compare
  mcp_server/    tools.py (one plain function per tool) · auth.py · server.py (/mcp wiring)
  services/providers/   base registry loader tokens oauth query · google mcp   <- one file per backend
  jobs/          queue.py (asyncio pool) · registry.py (stages + weights)
                 scheduler.py (the auto-match timer) · search_indexer.py (MMN-15)
  prompts/       summary_prompt.md · match_rank_prompt.md · note_title_prompt.md
                 <- EDITABLE, no deploy needed
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

**`navigator.clipboard` is gated on a secure context too** — the same trap as `mediaDevices` above,
and it bites on the same plain-HTTP LAN address. The object is *undefined* there, so
`navigator.clipboard.writeText(…)` throws synchronously rather than returning a rejected promise and
a bare `.catch()` never fires. `lib/clipboard.ts` feature-detects, falls back to the ungated
`document.execCommand('copy')`, and returns `false` when neither works so the button can say so
instead of flashing a tick that lied.

## Notes

The third kind of document on a thread, alongside emails and calendar events — the only one this app
writes rather than fetches. `thread_notes` mirrors the other two attachment tables (thread FK,
nullable `meeting_id`, `ON DELETE SET NULL`) minus everything provider-shaped: no uid, no
`raw_json`, no relevance, and no unique index, because two notes with the same title are two notes.

Created by hand, or out of an AI chat reply via **Copy** / **Add to note** under every assistant
message in both panels. `routers/notes.py` exposes the same operations twice — once under
`/threads/{id}` and once under `/meetings/{id}` — because the thread page and the transcript page
hold different ids; only create and list branch, and everything else uses the `thread_id` the server
put on the note.

**A note is never unread.** `auto_attached`/`seen_at` exist because the sweep attaches things while
nobody is looking; every note is a button press, so `UNREAD_TABLES` deliberately does not list it.

**A note is not summarizer input.** `matching.attached_context` does not read this table, on purpose:
an AI-written note feeding the next summary of the meeting it was written from puts the model's own
prose back into its input. Notes reach the model through the thread chat digest and the next-step
payload instead, both of which say whose words they are (`source`) so a saved reply is not cited back
as evidence. A test asserts `attached_context` still returns only events and emails.

**The next-step fingerprint carries a note's `updated_at`, not just its id.** Emails and events are
immutable snapshots of something fetched; a note is rewritten in place, and rewriting one should make
a cached suggestion stale.

**`threads.next_step_enabled` gates only the automatic path.** `threads_svc.next_step_needs_refresh`
returns `False` immediately for a thread with it set to `0`, so `GET /threads`'s "generate what's
missing" pass skips it — but the manual `POST /threads/{id}/next-step` button (the "Refresh" button
on the thread page) always runs regardless, same manual-action-always-wins rule as the sweep's own
`auto_match_enabled` override next to it.

**Title generation cannot fail the save.** No title supplied means one LLM call
(`note_title_prompt.md`) run through `to_thread`; if it errors or comes back empty the note is filed
under `derive_title` — its own first line, markdown stripped — with `title_model` NULL to record that
nothing generated it. The body is the part worth keeping.

## Best of 2 (chat)

A "Best of 2" checkbox next to the model selector in all three chat panels (home, thread, meeting).
With it on, a second selector appears and one send goes to both models; the answers render side by
side (`BestOfTwoAnswers.tsx`), each with a close X. Closing one returns the panel to single-model mode
with the *surviving* model selected.

**There is no compare endpoint: it is two ordinary chat requests**, one per model, so each persists
its own question and answer. Closing a side that had already finished calls
`DELETE .../chat/messages/{id}` (`services/chat_compare.discard_reply`), which removes that assistant
row and the user row directly before it; a side still streaming is just aborted, and an aborted
request is never persisted. That "the row directly before" rule is sound only because `_produce`
inserts the pair in one synchronous block with no `await` between them. Only an *assistant* row can be
discarded, so the route can never delete a user's own words.

**Settling waits for the stream to close, not for `done`.** Follow-up chips arrive after `done`;
handing the answer back on `done` would unmount the component and abandon them.

The toggle and second model are per-panel `useState`, not remembered: it doubles the cost of every
send. Known limit: thread chat's `attach_*` tools run in *both* requests (attach is an idempotent
upsert), so the closed model may have attached something the kept one did not. Navigating away or
pressing Stop mid-compare leaves any side that had already finished saved as a normal pair.

## Groups

Collapsible folders over threads on the home screen. `thread_groups` is a name and an owner;
membership is `threads.group_id`, so filing a card is one UPDATE and there is nothing to keep in
sync.

**"Ungrouped" is not a row.** It is `group_id IS NULL`, which is why every thread that predates the
feature is already in the right place, and why deleting a group can release its threads instead of
deleting them (`ON DELETE SET NULL` — which only fires because `db.connect` sets
`PRAGMA foreign_keys=ON`; SQLite has them off by default). The section is rendered like any other but
cannot be renamed or removed.

**Ungrouped hides itself when empty, and comes back for the length of a drag.** A heading over
nothing is noise once everything is filed — but it is also the only drop target for taking a thread
*out* of a group, so hiding it unconditionally would make that last drag impossible. `dragstart` and
`dragend` both bubble, so one pair of handlers on the list container tracks "a card is moving"
without threading callbacks through every card. It stays put when no group exists at all: that case
is the whole page, and the "no threads yet" panel lives in it.

**`group_id` is a `LATE_COLUMN`, so its index cannot live in `SCHEMA`.** `SCHEMA` runs first, so
naming the column there fails on the one boot that adds it. That is what `LATE_INDEXES` is for.

**Assignment is `PUT /threads/{id}/group`, not a field on `ThreadUpdateRequest`.** Every field in
that model treats `None` as "leave this alone", so a patch could never express "move this thread to
Ungrouped".

**Moving a thread does not bump `updated_at`.** Filing is not activity, and the default sort is last
activity — otherwise a tidy-up session sends every card you touched to the top.

**Each group pages on its own** (`?group=<id>|none` on the thread list, one query and one `page`
state per section), so a group is never split across a page boundary. Paging therefore left the URL;
the filters stayed, because they are what a shared link means.

**The drag payload is a custom MIME type.** A card wraps an `<a>`, so the browser has already put
the thread's *URL* on the drag before any handler runs — `application/x-mmn-thread-id` is how a card
is told from a link, a file, or a selection dragged in from another window. Only
`dataTransfer.types` is readable during `dragover`, which is why the accept check reads that and not
the data.

**The `<select>` on every card is not a redundant control.** HTML5 drag and drop emits nothing a
keyboard can trigger, so without it the whole feature is mouse-only.

**A group's colour comes from its id, not its list position.** `lib/groupColors.ts`, the same rule
and the same reason as `speakerColors.ts`: sections are ordered by name, so deriving the hue from
position would repaint half the page every time a group is renamed or deleted. `--group-0..7` are
`var()` aliases of the speaker slots rather than a second palette — the two never share a screen, and
one set of eight is one thing to keep contrast-checked. Ungrouped keeps `--entity-meeting`: unfiled
is the absence of a group colour, not a ninth one.

**The collapsed set is stored whole**, so it has exactly one owner in `GroupedThreadList`. Two
sections each holding their own copy would have the second one's write erase the first's.

## Email conversations

Attached emails are grouped into conversations and carry a `direction`, so the app can tell "they
are waiting on me" from "I already replied". Before this, every LLM surface saw `sender`, `date` and
a 200-char `snippet` and nothing else — which is why the next-step suggestion would cheerfully tell
you to send a mail you had already sent. `build_gmail_query` has no `-in:sent`, so **your own sent
mail was already being attached**; it just rendered identically to received mail.

**Chains are computed on read, never stored** (`services/email_chains.py`). Not merely because lazy
backfill would make a stored key stale: a chain key is a *global* property of the set, so attaching
one email can merge two chains and would require rewriting every row in *both* — a fan-out write on a
table four independent paths write to (`matching.attach_selected`, `followups.sweep_thread`,
`chat._tool_attach`, `routers/calendar.py`). Corollary: do **not** call `build_chains` from
`threads.compute_next_step_fingerprint`, which runs once per row on every home-page load.

**Three tiers, in descending authority**: the provider's own conversation id → the RFC 2822
`In-Reply-To`/`References` graph → normalized subject plus participant overlap. Tier 2 has a
non-obvious half: two replies citing an ancestor *nobody attached* still belong together, and that is
the common case, because people attach the replies and not the original.

**Tier 3 is a heuristic with five guards, and it only ever looks at rows the first two tiers left
alone.** That restriction is what makes lazy backfill *monotone*: hydrating one pair splits a
subject-guess into a real chain plus leftovers, and can never reshuffle a chain someone else is
already in — otherwise a half-backfilled thread reorders its cards on every page view. The guards:
generic subjects never merge; a bucket over 12 is a newsletter; **the only shared participant being
*you* is not evidence** (every message in your mailbox shares you, so `account_addresses` is
subtracted first); an undated row never merges; and a gap over 30 days is a new conversation.

**`normalize_message_id` returns `None`, never `""`,** for an absent header. An empty string is a
perfectly good dict key, so every header-less row on a thread would union into one chain on a value
that means "we don't know".

**`chain_addresses` filters empty headers *before* `getaddresses`.** Since the CVE-2023-27043
hardening it returns a single `[('', '')]` — discarding every address it did parse — if any element is
malformed, and `""` counts. Most attached rows have no To/Cc, so passing them through silently
reduced participant overlap to nothing and disabled tier 3 entirely.

**`direction` is `'outbound' | 'inbound' | NULL`, not a boolean.** Three states, and the third is
common (MCP, dev, Zoho without headers). Gmail's `SENT` label is authoritative; an address comparison
against the account is a guess; neither is available everywhere. A NULL rendered as inbound tells the
summarizer someone else asked a question the user asked themselves, so every surface — card, digest,
next-step payload, ranker — renders it as *unknown* and none of them guess.

**`conversation_id` goes through `uid_for`.** Gmail's `threadId` is unique per *mailbox*, so two
connected accounts holding one conversation would collide into a single chain. Same invariant as
`message_id` and `uid`. MCP emits `message_id`/`rfc_message_id` verbatim for back-compat but its
threading fields stay NULL — there is no back-compat hazard because nothing was ever stored, and
synthesising one would fabricate an authoritative-tier link out of nothing.

**Gmail gets all of this for free.** `google.py` already requested `threadId` in its `fields` mask and
threw it away; `To`/`Cc`/`In-Reply-To`/`References` and `labelIds` are more entries on the *same*
request. Those headers are only available at search time — recovering them later means re-downloading
every message. `labelIds` also exposed a pre-existing bug: Gmail's `q` returns **drafts**, which used
to attach as ordinary inbound mail with a meaningless date. Dropped now: as inbound a draft invents an
incoming request, and as outbound it claims you replied when the whole point is that you have not.

**`body` is not on `EmailCandidate`.** Candidates are persisted three times per match run and nothing
prunes `match_runs` — and no provider has a body at search time anyway (Gmail is `format=metadata`,
IMAP fetches header fields, Zoho's search payload and the MCP result carry no content), so the field
would be permanently None on every path. Bodies live only in `thread_emails.body`, written by
hydration.

**`ai_summary` is deliberately not `summary`.** That column is the email-triage MCP server's own
field, and a triage field must never be synthesised.

### Hydration

`services/email_bodies.py`, on demand, **not a queued job**. Both reasons the sweep avoids the queue
apply harder: hydration fires on every thread open rather than on a timer, so the dock would fill with
one-second jobs while a 40-minute diarization scrolled out of sight; and restart survival is free,
because `body IS NULL AND body_fetched_at IS NULL` *is* the resume predicate. The one honest argument
for the queue is bounding LLM spend, and the queue is an unbounded FIFO with no per-user limit — the
guards that work are `next_step`'s: a per-event-loop semaphore and a "we already tried" stamp.

**`body_fetched_at` is stamped on every *attempt*, success or failure** — the `next_step_checked_at`
vs `next_step_generated_at` split. `body IS NULL AND body_fetched_at IS NOT NULL` therefore means
"asked, and this account cannot", which is what stops a provider with no fetch-by-id tool being
re-asked on every page view. The SPA renders that state as terminal with **no retry button**: a retry
that cannot succeed is a lie.

**Hydration fetches bodies. It never calls the LLM.** Summarising is a separate route
(`POST /emails/summarise`) behind a per-conversation button, because one model call per message is
spend and latency nobody asked for on a page load. The bulk body fetch returns `remaining`, and the
SPA loops until the thread is done — the first version stopped after one screenful per *page visit*,
so a 40-email thread needed four visits to fill in.

**Settings → Email backfill is where lazy stops being invisible.** `routers/email_backfill.py` +
`email_bodies.account_stats`. Lazy hydration is the right default and a bad status report: an account
with two hundred attached emails across threads nobody revisits stays mostly un-backfilled, and
nothing in the app said so. The page shows the counts and drives the same bounded per-thread calls in
a loop, so it needs no job and no new state — the existing predicates *are* the resume point.

**Automatic backfill (`jobs/email_backfill_scheduler.py`) is a second, opt-in ticker over that same
button.** Same shape as `AutoMatchScheduler` (see Automatic follow-ups below): one always-started
`asyncio.Task`, gated on `auto_backfill_enabled` re-read every cycle, off by default. It operates at
*user* granularity, not thread granularity — `due_users()` sweeps every active user whose
`users.auto_backfill_at` is NULL or older than `auto_backfill_interval_minutes`, oldest-first, capped
at `auto_backfill_max_users_per_cycle` per cycle — because the manual routes it drives
(`next_thread_needing_bodies`/`hydrate_thread_emails` and their summary equivalents) already operate
per-user, across all of that user's threads at once. One consequence worth remembering in a test: the
bootstrap admin is itself always a due candidate (active, never swept, no threads of its own), so it
counts toward `swept` as a true no-op on every cycle. Per user, one bounded round alternates a bodies
batch and a summaries batch — the exact same two calls the manual buttons already make — until both
report nothing left, a batch makes no progress, a summary batch `stalled`s (the same "LLM is down,
attach/summarise nothing" interlock as the follow-up sweep's `rank_sync` returning `None`), or
`auto_backfill_max_rounds_per_user` is hit. `auto_backfill_at` is stamped **even on failure**, same
rule as `threads.auto_match_at`: a broken account must not turn into a request storm retried every
tick. No job queue, for the identical reasons hydration itself already avoids one. The Settings UI
lives on the same `/settings/email-backfill` page as the manual buttons — a `SettingsForm` block
(exported from `SettingsPage.tsx` for this reuse) identical in shape to "Automatic follow-ups" on the
Matching tab, admin-gated the same way since these are global `app_settings` rows, not per-user.

Two things it must keep getting right. **"Unavailable" is a third bucket, not pending**, or the bar
can never reach 100% on an account with an MCP or Zoho row in it. And **the summaries bar is measured
against messages that have a body**, not against every attached email — measured against the total it
would show "Up to date" beside a two-thirds-full bar the moment the outstanding count hit zero. The
run loop also stops on a `stalled` batch (work requested, none completed): a failed summary stays
eligible *by design*, so without that flag the client would loop to its cap re-spending on every pass.

**`pending_summaries` is a second predicate, not a flag on the first.** `pending` requires
`body IS NULL`, so once a body is stored the row is invisible to it forever — including under
`force=True`, which only relaxes the `body_fetched_at` half. That silently made a failed or skipped
summary *permanent*. "Which rows want a summary?" is a different question from "which rows want a
body?" and needs its own query. Deliberately no attempt-stamp on the summary side, unlike
`body_fetched_at`: summarising is a button press, so pressing it again *is* the retry.

**Hydration is the threading backfill for Gmail and IMAP only.** `format=full` and
`FETCH BODY.PEEK[]` both return the whole message, so `get_email_message` can fill the header columns
too; Zoho's content endpoint and MCP's `fetch_full_email` return content alone. That is why
`get_email_message` is a *sibling* of `get_email_body` rather than a widening of it — widening would
make it a promise three of five providers cannot keep. Write-back is COALESCE-guarded, so a provider
with no headers never blanks what a search already stored.

**Bodies are converted to text on the way in** (`services/html_text.py`, stdlib only). The SPA then
needs no HTML sanitizer, and SQLite reads whole rows so markup would be dragged off disk by every
read that never shows it. The detector requires **two** well-formed tags or a document marker,
because the failure directions are not symmetrical: mistaking markup for text leaves tags visible,
while mistaking text for markup *deletes characters* — `a<b and b>c` is a comparison, and
`<priya@acme.com>` is an address.

Hydration must not `touch_thread` (opening a thread is not activity — the same rule as filing one) and
must not write `seen_at` (owned by `mark_seen`; this is the app fetching, not a person reading).

**`thread_emails.integration_id` is a plain INTEGER, not a foreign key.** It is copied out of a match
run's persisted `ranked_json`, which can name an account disconnected between running the match and
confirming it — with `foreign_keys=ON` a REFERENCES clause makes that a hard IntegrityError, turning
"attach these emails" into a 500. A dangling id is harmless: the lookup is owner-scoped, finds
nothing, and takes the "cannot supply a body" path. The column also *fixes* a bug — `integration_id`
used to be recovered by parsing the composite `message_id`, which silently fails for MCP's bare ids,
so every MCP-sourced email was unfetchable.

### What reads chains, and what must not

**`attach_email`'s `ON CONFLICT` clause is the most dangerous line in the feature.** Re-attaching is
the only way a pre-migration row acquires these columns, so the clause has to update them — but every
new field must be `COALESCE(excluded.x, x)`, never bare `excluded.x`, or a second attach from a
headerless provider *erases* threading the first one stored. The clause could not lose data while it
only touched `meeting_id` and the relevance pair; extending it can.

**`attached_context` gets `direction` and `ai_summary`, but never a body and never chains.** It feeds
the summarizer, whose input is a transcript — a full inbox alongside it changes what the minutes are
written from. And it is scoped *per meeting*, so two messages of one exchange can be attached under
different meetings (or one under a meeting and one under NULL from the sweep); grouping there would
render a fragment and present it as the whole conversation. Chains belong only where the scope is the
whole thread: `next_step._payload`, `chat._format_attachments`, and the chains route.

**The chat digest budgets its attachments block.** It used to add attachments unconditionally and
truncate only meetings — harmless while an email was a 200-char snippet, a real bug once it can carry
a full body, because one long thread could produce an attachments-only digest with a single meeting
glued on. Attachments take at most `ATTACHMENT_BUDGET_SHARE`, split per section, and say what they
dropped: a silent cap reads as "you saw everything".

**`next_step` selects every email and caps the *chains*.** Slicing the messages first would cut
conversations in half and then report the remainder as if it were the whole exchange. Its payload key
is `email_chains`, renamed from `recent_emails` in the same commit as the prompt bump — a stale prompt
reading the old key would find nothing and quietly decide the thread has no email on it.

**iCloud/IMAP search is `INBOX`-only, so your own sent mail is never found there.** Not fixed:
`_imap.search` selects exactly one mailbox, the sent folder's name is localized, and widening it
changes what gets matched and auto-attached. Surfaced in the UI instead — an all-inbound chain on an
Apple account says outbound mail may be missing, because a fragment presented as whole is the failure
mode this whole feature exists to remove.

**`--fg-faint` is not for email text.** The snippet and the timeline `<time>` used to use it; both are
content, and that token is annotated `DECORATIVE ONLY` and fails AA by design. Body → `--fg`,
preview/summary → `--fg-muted`, meta → `--fg-subtle`.

## Search & indexing

`services/search.py` is the one search over everything a user owns: thread title and description,
meeting titles, transcript segments, the current summary, its action items, notes, emails, and
calendar events. Keyword search uses FTS5 (`search_fts`, `unicode61 remove_diacritics 2`, `bm25()`
with the title weighted 4×). Semantic search uses stored embeddings (`search_embeddings`). The two
are merged with Reciprocal Rank Fusion (k=60). `GET /api/search` serves the home page's "Search
everything" view, and the MCP `search` tool (MMN-14) calls `search()` directly. Settings →
Search shows coverage and has the admin "Rebuild index" button.

**`search_docs` is the plain-table twin of `search_fts`** (same id = rowid). FTS5 cannot index an
UNINDEXED column, so `owner_id = ?`, thread cascades and the per-row fingerprint would each be a full
scan of the virtual table. Every write goes through `search_index.upsert_doc` / `_delete_fts_where`,
which keep the two in step. Nothing in this group of tables has a foreign key, because all of it is
derived and can be rebuilt from scratch.

**Work is tracked per scope, not per row.** `t:<thread_id>` covers the thread doc, notes, emails and
events. `m:<meeting_id>` covers the meeting, the *active* transcript's segments, the *current*
summary and its action items. A write path calls `search_index.mark_thread` / `mark_meeting`, which
is one INSERT into `search_dirty` inside the caller's own transaction. The background indexer then
re-renders the scope and writes only the docs whose fingerprint changed. Rows aren't tracked
individually because `attach_email`'s ON CONFLICT upsert never says which row id it touched, and
there are about 30 write sites. `touch_thread` marks its thread, so most child writes are covered
already.

**Two things are synchronous, on purpose.**
- **Deletes:** `delete_thread_scope` / `delete_meeting_scope` before the DELETE, `delete_doc` on a
  detach or note delete. A cascade never reaches a virtual table, and a deleted note must stop
  matching now.
- **The thread doc itself** (`index_thread_doc` on create and PATCH), because the home list's filter
  runs on it.

**The home "Search threads…" filter is FTS now** (`threads._apply_text_filter`). Its scope (title and
description), sort and per-group paging are unchanged, but it matches whole words: `meet` no longer
finds "meeting", while `meet*` does. CJK words are the exception: they match as substrings (see "CJK
goes to a second index" below). It falls back to the old `LIKE` when the query has no words at all
(`%`, emoji), and per row for any thread with no index doc yet (the first boot after deploy).

**`reconcile()` is the safety net, and it is cheap.** One SQL fingerprint per scope (every column a
renderer reads, plus `updated_at`, plus a `RENDER_VERSION` constant) is compared against
`search_scopes.source_fp`. It runs at startup, on Rebuild, and every
`search_reconcile_interval_minutes` (60, env-only). A write path that forgets to mark therefore
leaves the index stale for at most an hour. It is never wrong about ownership, because ownership is
read from the source rows on every render. It also repairs a docs/FTS mismatch in either direction.
**Bump `RENDER_VERSION` whenever a renderer's output changes**, or old rows keep looking current.

**Indexing only reads.** It never writes `raw_json` (speaker renames re-render segments from
`speaker_map` through the same `transcript.build_transcript` the page uses), `updated_at` or
`seen_at`. It indexes only email bodies that are already stored and never hydrates. Search tables
are not summarizer input: `attached_context` does not read them, and a test asserts it.

**User input never reaches MATCH raw.** `parse_query` rebuilds the query from alphanumeric runs
only, each one double-quoted, so `"`, `-`, `*`, `:`, `(` and `NEAR`/`OR`/`AND` are literal, and emoji
are dropped. Bare words are ANDed. `"phrases"` and `prefix*` (≥2 chars) survive.

**CJK goes to a second index, `search_fts_tri` (trigram), MMN-16.** unicode61 does not segment
Han/Kana/Hangul, so a run is one token and a part of it never matched. `parse_query` now sends every
word containing a CJK character (`has_cjk`) to the trigram table as a *substring* term, and
everything else to `search_fts` as before. Each arm is a condition on the same `search_docs` row, so
`Q3 会议` is an AND across both, and the result is still one keyword list: RRF and `matched_by` see
no difference. Latin words stay whole-word, so `meet` still does not find "meeting".
- **≥3 characters → trigram `MATCH`** (indexed, bm25-ranked, `snippet()`).
- **1–2 characters → `LIKE '%term%'` on the trigram table's own stored text.** This was chosen over
  "require ≥3" because two-character words are the *common* case in Chinese (会议). The trigram
  index cannot serve a pattern under three characters (a `MATCH` that short silently matches
  nothing), so it is a rowid lookup per candidate row after the owner filter. When a query has only
  short terms there is no bm25: title hits come first, then newest, with a snippet cut in Python
  (`like_snippet`). CJK terms come out of the same `_words` splitter, so `%` and `_` can never reach
  the `LIKE`: they are separators.
- A token or phrase that mixes CJK and other words degrades to an AND of its words, because
  adjacency across two indexes cannot be expressed. `*` on a CJK word is dropped, because a
  substring match already covers a prefix.
- The table is **not contentless**, because the `LIKE` path needs the text. Its size is about 2–3× the
  unicode61 index (production at deploy: 376 KB keyword, 736 KB trigram, 1,287 docs). Settings → Search shows both sizes to admins (`dbstat`, cached 5 minutes,
  `null` on a SQLite build without it).
- Everything that writes, deletes, reconciles or rebuilds walks `search_index.FTS_TABLES`.
  Reconcile checks each table independently, so an install from before MMN-16 is backfilled as a
  "doc missing its text" repair. `RENDER_VERSION` 2 also re-renders everything once.
- The home thread filter uses the same routing through `search.doc_select`, so `会议` finds a
  thread titled `会议记录…`.

**Semantic search fails open, and its brute force is deliberate.** The query is embedded once and
dot-producted in pure Python against the owner's L2-normalised float32 vectors. That is fine up to
about `SCALE_LIMIT` (25k chunks per owner, ~250 ms); past that the status page warns, and the next
step is sqlite-vec or numpy. Disabled, unreachable, or nothing embedded yet all return keyword
results with `semantic: {available: false, reason}`, never a 5xx.

**Embeddings are keyed on `(kind, chunk ref, model)` with a `text_sha256`.** Changing
`embedding_model` hides the old vectors at once. They are pruned only after the new model covers
every scope, so semantic search is never empty mid-migration. Unchanged text is never re-sent, which
is also why Rebuild keeps vectors and only re-checks them. Transcripts are embedded in windows of 8
segments / ~800 chars overlapping by 2, carrying the first segment's `start_sec`. Notes, summaries
and email bodies are packed by paragraph up to 1,200 chars. A semantic hit resolves to its document
through a join on `search_docs`, which is also what hides a deleted document's vectors.

**The indexer is not a job.** It is one always-started task (`jobs/search_indexer.py`) that is
nudged by write paths through `search_index.set_waker`. It waits `SETTLE_SEC` after a nudge, because
the nudge can arrive before the writer commits. It is off in the test suite
(`MMN_SEARCH_INDEXER_ENABLED=0` in `conftest.py`), and tests drive `search_index.index_all_now`
instead, so nothing races an assertion. The reasons for not using the job queue are the same as for
email hydration.

## MCP server (what this app serves)

Not to be confused with the MCP *client* under Integrations below (this app calling calendar/email
servers). `/mcp` is the other direction: a streamable-HTTP MCP server so Claude Code, Claude
Desktop or Pocket Agent can read the repository — "get last week's Atlas standup transcript".
`app/mcp_server/tools.py` holds every tool as a plain function (`(conn, principal, **args) -> dict`)
plus the `TOOL_SPECS` registry; `server.py` turns them into MCP tools; `auth.py` decides who is
calling. Settings → MCP server (`/settings/mcp-server` — `/settings/mcp` is an old redirect to
Integrations) shows the endpoint, the tools, the on/off switch and the caller's own tokens.

**Stateless, JSON responses.** `stateless=True, json_response=True`: every request carries its own
bearer and gets plain JSON, so there is no `Mcp-Session-Id` to keep sticky behind a proxy and no
SSE stream for one to buffer. Every tool is request/response, so nothing is lost.

**Every protocol revision, negotiated by the SDK — which is why this needs `mcp` 2.x.** /mcp speaks
2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25 and 2026-07-28. 2026-07-28 is a different wire, not
a new number: no `initialize` handshake, the version and client capabilities in every request's
`params._meta` (mirrored into `MCP-Protocol-Version` / `Mcp-Method` / `Mcp-Name` headers),
`server/discover` instead of a handshake, `resultType` on results. v1 of the SDK cannot speak it, and
appending the string to v1's `SUPPORTED_PROTOCOL_VERSIONS` (a brief workaround on main, now removed)
made `initialize` echo "2026-07-28" while still speaking the 2025 wire — a claim, not support.
The v2 session manager routes on the `MCP-Protocol-Version` header: absent or a handshake revision →
the legacy stateless path, where `initialize` agrees to any of the four (an unknown offer gets
2025-11-25 back, per the handshake rules); anything else → the per-request path, which refuses an
unsupported version with `-32022` and `data.supported`, and a header/envelope disagreement with
`-32020`. `server/discover` lists only `2026-07-28` — the versions usable *on that wire*; older
clients negotiate through `initialize` instead. The app re-implements none of this.
`server.SUPPORTED_PROTOCOL_VERSIONS` is read from `mcp_types.version` so it can never advertise a
revision the installed transport does not speak; Settings and the tests read it from there.

**mcp 2.x moved things.** `FastMCP` is `mcp.server.mcpserver.MCPServer`; there is no
`get_context()`, so the tool wrapper declares a `Context`-typed `mcp_context` parameter (injected,
kept out of the schema) and reads `request_context.request`; token scope is a `ServerMiddleware`
(`ScopeMiddleware`), which sees both eras before params validation. Wire types are snake_case in
Python — **`CallToolResult.is_error`, not `isError`** — and `mcpclient.call_tool` used to read only
`isError`, which under v2 silently turns every calendar/email tool failure into an empty result
("no events"). `mcpclient.is_tool_error` checks both, and a test uses the SDK's real result type
rather than the fake. The SDK brings its own HTTP stack (`httpx2`/`httpcore2`) alongside, not
instead of, the `httpx` the app and respx use.

**Advertise only tools — the SDK's default capabilities are promises this server never keeps.**
Capabilities are derived from *registered handlers*, and `MCPServer` registers `subscriptions/listen`,
prompts and resources by default, so `server/discover` claimed `listChanged` on tools/prompts/
resources plus `resources.subscribe`. A client that believes it opens a `subscriptions/listen`
stream — which by design never completes — and lists prompts and resources for nothing. From a
browser that stream holds one of the six HTTP/1.1 connections per origin forever; with the rest
busy (an open app tab polling is enough), every later request queues until it times out. That is
exactly the MCP Inspector's "Request timed out … 5 requests are unanswered: subscriptions/listen,
resources/list, tools/list, prompts/list, resources/templates/list … Last response received
(server/discover)". `server.UNUSED_METHODS` unregisters those handlers, so both eras advertise
`{"tools": {"listChanged": false}}` and a stray listen gets an immediate `-32601`. If a tool list
ever becomes dynamic, re-register listen *and* actually publish change events — never advertise one
without the other.

**Two raw `Route`s, not `app.mount`, and before the SPA catch-all.** Mounting the SDK's Starlette
app at `/mcp` gives `/mcp/mcp`, and a mount 307-redirects bare `/mcp` to `/mcp/` — a redirect
several clients do not follow on POST. Registered after `_mount_spa` it would be answered with
`index.html`. A test asserts `/mcp` comes back 401, not the SPA.

**FastMCP turns DNS-rebinding protection on by itself** when constructed with its default
`host="127.0.0.1"`, allowing only localhost `Host` headers — which 421s every request arriving via
the LAN address or the reverse proxy. `server.py` passes `TransportSecuritySettings` explicitly with
it off; the bearer token is the gate, and a rebinding page in someone's browser cannot attach it.
A test sends `Host: 192.168.1.20:4020`.

**The session manager's `run()` may be entered once per instance**, so `create_app()` builds a fresh
`MCPEndpoint` every time (the suite builds an app per test) and `lifespan` enters it around `yield`.

**The principal rides on the ASGI scope, not a contextvar.** `MCPEndpoint` authenticates before the
SDK sees the request and stores the principal in `scope["state"]`; the SDK passes each tool call the
originating Starlette request, which is how `current_principal()` finds it. A contextvar would have
to survive the session manager's task-group hop.

**Personal API tokens, not sessions** (`api_tokens` table, `services/api_tokens.py`). A session
expires after `session_ttl_hours` and needs a password login — wrong for an agent configured once.
`mmn_` + 32 random bytes, stored as sha256 like `sessions.id`, shown once, `prefix` kept for the
list. `scope` is `read` or `read_write`. **Accepted by `/mcp` only**: the REST routes know nothing
about scope, so a read token there would be a write token — and `/api/tokens` itself is
session-only, so a token can never mint a token. A session *bearer* is also accepted on `/mcp`
(as `read_write`, since it can already do everything via REST); a session *cookie* is not, so a
logged-in browser tab cannot be driven cross-origin. Every rejection is the same opaque 401.
`last_used_at` is written at most once a minute.

**Write tools are hidden, not just refused**, from read tokens: `list_tools` filters on scope and
`call_tool` answers "Unknown tool" — what it is to that caller. Three exist (`create_note`,
`append_to_note`, `set_action_item_status`); nothing deletes, uploads, regenerates (LLM spend) or
touches settings.

**Read tools never write.** No `touch_thread`, no `seen_at`, no email hydration, no LLM call — the
same "reading is not activity" rule hydration follows. A test snapshots `updated_at`/`seen_at`/
`body_fetched_at`/bodies around a call to every read tool and asserts zero LLM requests. The only
network a read tool makes is `get_upcoming_events` (a live calendar read, run on the main loop
because `providers/tokens.py`'s refresh locks are per-event-loop) and `search`'s query embedding.

**Owner-only, admins included.** No `all` flag: an agent should never browse other users'
meetings. Someone else's id is `not_found`, exactly like a missing one; a parametrised test points
another user's token at every id-taking tool.

**Bounded output.** Lists page (`limit` ≤ 100); transcripts page with `next_offset`
(`max_chars` ≤ 200k); note/email bodies in lists are previews. `list_meetings`'s `query` matches the
meeting title *or its thread's title*, because "the Atlas meeting" usually means a meeting filed
under an Atlas thread. Responses carry `server_time` so a client can turn "last week" into
`since`/`until`. Email `direction` NULL is `"unknown"`; a per-meeting email slice is returned flat,
not grouped, for the reason `attached_context` never groups (a fragment presented as a whole).

**`source='mcp'` notes are AI prose.** `notes.source_origin` words them "added by an AI assistant
via MCP" in both chat digests, and `next_step_prompt` (v4), `chat_prompt` (v3) and
`meeting_chat_prompt` (v2) say to treat them like `ai_chat` — never evidence. The SPA shows the
same AI badge.

**`mcp_enabled` defaults on** (reviewer's call): with no tokens nobody can use it anyway. Off makes
`/mcp` a 404, re-read on every request. OAuth for claude.ai *remote* connectors is out of scope —
every target client accepts a static header.

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

**A meeting can exist before its audio does**, which is what `POST /meetings/{id}/audio` is for.
Uploading through `/meetings/upload` instead creates a *second* meeting and leaves the calendar
event, the attendee-derived speaker hints and the timeline position on the empty one. That route
replaces the audio of a failed attempt but refuses to overwrite a **transcript**, because the
diarization, its speaker names and every summary belong to the audio being replaced.

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

### Development (fake data)

A provider whose calendar and inbox you write yourself, from **Settings → Development**. It exists
so matching, the threshold and the sweep can be exercised without a real account, and it is a
provider rather than a test double precisely so the thing being exercised is the real path.

**Off unless `MMN_DEV_PROVIDER_ENABLED` is set**, and env-only — deliberately not in `RUNTIME_KEYS`.
Same reasoning as `diarize_fake`, plus one more: what it produces gets attached to real threads for
good. Three gates, all reading the flag lazily: `registry.all_specs` hides it from the picker,
`loader.build_provider` returns None so an existing row goes inert, and every `/api/dev/*` route
404s.

**`dev_emails`/`dev_events` are a *source*, not attachments.** The tempting shortcut — writing fake
rows straight into `thread_emails` — skips ranking, dedupe, the threshold and attach, which is the
entire pipeline the fake data exists to exercise.

**An item's date is an offset, not a date.** `date_mode` is `absolute` | `relative` (now + offset) |
`anchored` (a meeting's `meeting_at` + offset). Only the last two survive contact with time: a
pinned date falls out of the 60/60 match window within a couple of months and the fixture silently
stops testing anything. An anchored item whose meeting was deleted falls back to relative rather
than vanishing — an item that quietly stops appearing is the harder failure to diagnose.

**It filters on keywords and window like a real provider.** One that returned its whole table
regardless of the query would mean ranking never sees a plausible non-match, and near-misses are
the fixtures worth authoring. `expected_relevant` is the answer key for judging a run; nothing
branches on it.

**`rfc2822_date`, `all_day` and `repeat_weekly` exist to reproduce documented traps** — lexical
sorting of raw RFC 2822 dates, the UTC-midnight all-day shift, and `dedupe_events` over a series
sharing one `source_uid`. The last is the cheap stand-in for `icloud_recurring.ics`.

**`auth_type` is `"none"`, and its `account_key` is a slug of the label** — the one place a key
comes from something renameable. Snapshotted at create time and never rewritten, so the unique
index still stops repeated Connect clicks piling up rows while two labelled accounts stay distinct
(which is what makes the "every account of a kind failed" aggregate reproducible).

**Generated items are returned, never written.** `POST .../generate` responds with drafts; accepting
one POSTs it back through the ordinary create route. One write path, and a model that returns
nonsense costs a click rather than a cleanup. `dev_seed_prompt.md` asks for decoys and near-misses
on purpose: email generated *from* a meeting's summary and then matched *against* it makes a test
that cannot fail — the same circularity the `attached_context` note warns about for AI-written notes.

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

`range` is mandatory on the events call and capped at 31 days — a wider request is narrowed to the
middle 31 days of what was asked for rather than sent as-is or chunked, since the calendar match
window (`match_window_calendar_days_before/after`, 60/60 by default) routinely exceeds it and a
provider-level failure there would silently drop Zoho out of every match. Zoho is regional
(`.com`/`.eu`/`.in`/`.com.au`/`.jp`) and the wrong data centre authenticates fine and returns
nothing, so the DC is pinned onto the integration row at connect time. Mail search takes only an
upper time bound, so the lower edge of the window is enforced client-side.

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

**Three independent gates decide whether a thread is swept, and they `AND` together.** The global
`auto_match_enabled` setting; the thread's own `auto_match_enabled` (`NULL`/`1` = watched, `0` =
excluded from `jobs/scheduler.due_threads`); and, orthogonally, its `auto_match_calendar_enabled` /
`auto_match_email_enabled`, which gate individual sources rather than the sweep as a whole. Both the
global switch and the thread's own `auto_match_enabled` only ever gate the *scheduled* sweep — the
manual "Check now" button (`POST /threads/{id}/follow-ups`) calls `followups.sweep_thread` directly
and always runs, on purpose: a deliberate button press should not be silently vetoed by a background
setting. The per-source flags are different — they gate `matching.gather_candidates` itself, so they
apply to "Check now" too, because "this thread has nothing to match on this source" is true no
matter who triggered the search. A thread with both source flags off degrades through the existing
`NoIntegrationsError` → `skipped: "no_integrations"` path, the same one a user with no integrations
at all already hits — there is no separate "notes only" column; the SPA's "Notes only" checkbox is
just a shortcut that flips both source flags off together, since `thread_notes` were never fetched
by this pipeline to begin with.

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

**A recording past `diarize_chunk_threshold_sec` (50 min by default) is diarized in pieces, not one
request.** vibevoice-cpp-asr has an output-token budget, not a duration one — confirmed on meeting 24,
a real ~59 minute recording that came back as a single degenerate segment holding a truncated JSON
dump of its own attempted transcript instead of real turns (`diarize.py`'s
`looks_like_embedded_turns_dump` now catches that shape and fails the job loudly rather than silently
storing it). `pipeline._diarize_in_chunks` splits the wav with `audio.split_into_chunks`, diarizes each
piece, and `_stitch_chunk_payloads` merges the results into one payload shaped like a normal
diarization response. Each chunk gets fresh `SPEAKER_nn` numbering from the model with no memory of the
chunk before it, so segment/speaker ids are namespaced by chunk (`c1:SPEAKER_00`) rather than risking a
silent merge of two different people — reconciling them afterward is the same "merge speakers" move
already used for a same-chunk over-split. Fake mode (`MMN_DIARIZE_FAKE`) is checked before the duration
threshold and never chunks: it replaces the whole request-to-a-model step, so there's no real budget to
overrun, and every existing fake-diarization test keeps exercising the single-call path unchanged.

**"Diarization only" (`diarize_only`) bypasses chunking entirely, ahead of the duration check above.**
For a diarization backend that only ever produces speaker turns with empty text by design — confirmed
on `pyannote/speaker-diarization-community-1` — chunking is pointless: tested against meeting 24's
full ~59 minute recording, both it and a separate transcription service (its own URL/model/api key,
`transcribe_url`/`_model`/`_api_key`/`_timeout_sec`, mirroring the diarization settings exactly) handled
the whole file in one request each, in under three minutes combined. `diarize.diarize_sync`'s
`expect_text=False` skips the two checks that assume a combined ASR+diarization backend
(`include_text` missing / an embedded-turns-dump) — on a backend that was never asked for text, "every
segment is empty" is the correct shape, not a failure symptom. `pipeline._combine_diarization_and_transcript`
aligns the two by timestamp overlap (segment-level, not word-level — that's the resolution both services
actually offer) and **drops any transcribed segment with zero overlap with a real speaker turn**: on
meeting 24, 48 of 862 whisper-large-turbo-q8_0 segments had none, and the ones inspected were
hallucinated text ("Thank you." ×4) during two minutes of real pre-meeting silence pyannote correctly
saw as nothing. The combined result is stored under a model label of `"{diar_model}+{transcribe_model}"`
so flipping the toggle on/off never lets a stale run from the other mode look reusable at the
checkpoint. `diarize_chunk_threshold_sec`/`diarize_chunk_size_sec` are runtime-editable from Settings
now (previously env-only) — read via `effective()`, not `settings.diarize_chunk_*_sec`, inside
`_diarize_stage`, or a DB override would be silently ignored.

**`diarization_backend` (`"live_stt"` default | `"pyannote_ai"`) picks the wire protocol, checked in
`_diarize_stage` ahead of even `diarize_only`.** `"live_stt"` is every diarize_only/chunking/settings
behaviour described above, kept as the default so an existing deployment needs no migration — the
name is a historical accident (that self-hosted service happens to be hosted at a host literally
named `live-stt` on at least one deployment) and is unrelated to `live_caption_backend`'s own
`"live_stt"` value, a completely different gRPC streaming protocol Live Captions uses;
`diarize.test_connection`'s `backend` parameter already means that second thing, which is why
`/api/diarization/test` dispatches pyannote.ai itself rather than ever passing `backend="pyannote_ai"`
into it. `"pyannote_ai"` is the real `api.pyannote.ai` cloud service (`services/pyannote_cloud.py`) —
an async job-submission REST API, not a multipart POST to a configurable URL: upload the file via a
presigned `media://` URL (`POST /media/input` then a raw `PUT`), submit the job
(`POST /diarize {"url": "media://...", "transcription": true}`), then poll `GET /jobs/{id}` until it
leaves "running". `transcription: true` returns a speaker-attributed transcript
(`turnLevelTranscription`) in the same job, so this backend needs no pairing with `diarize_only` or
`transcribe_*` and bypasses both entirely, along with chunking — pyannote.ai's own job handles any
length. No URL setting, since there is exactly one hosted endpoint — but two model choices, both
pyannote.ai's own, sent explicitly on every job rather than only when non-default: `pyannote_ai_model`
(`"precision-2"` default | `"community-1"`, pyannote.ai's `model` field) and
`pyannote_ai_transcribe_model` (`"parakeet-tdt-0.6b-v3"` default | `"faster-whisper-large-v3-turbo"`,
their `transcriptionConfig.model`) — the STT model run *alongside* diarization, not a choice about the
diarization model itself. Neither is a free-text field/dropdown backed by a `/v1/models` catalog like
`diarization_model`: pyannote.ai has no such catalog, just these two fixed values each. Checkpointed
under `"pyannote_ai:{model}+{transcribe_model}"` (not `chosen_model`, which only means something for
the local backend) so switching backends, or either of pyannote.ai's own two model choices, never
makes a stale run from before the switch look reusable.

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

**Backend pytest runs under `pytest-xdist` (`-n auto`) with `hashlib.scrypt` patched down to a
trivial cost.** `tests/conftest.py`'s `_cheap_scrypt` autouse fixture forces `n=2,r=1,p=1` on every
real scrypt call the suite makes; `app/security.py` itself is untouched and still records the real
`n=16384,...` cost in `password_params`, so `test_params_are_recorded_so_cost_can_be_raised_later`
and the rest of `test_security.py` see production values. Without it, every `admin_client`/
`user_client` fixture build pays a real login (and, for admin, a real password change) at production
cost, and most of the ~1190 backend tests depend on one of those fixtures — `--durations` showed the
time sitting almost entirely in fixture "setup", not test bodies. xdist is safe here because every
test already gets its own `tmp_path` and sqlite db (`isolated_settings`); together the two cut a
~1490-test run from ~5 minutes to under 30 seconds on a 6-core box. (MMN-2.)

Everything network-facing is faked at two seams: `respx` for `httpx` (diarizer, LLM, Google, and
CalDAV once it lands — respx routes arbitrary methods, which is why CalDAV must go over httpx rather
than the `requests`-based `caldav` package) and a monkeypatched provider. The suite runs offline.

Provider fakes go in at `providers.loader.load_for_user`, one level above the transport — so
`test_matching.py` describes "a calendar account failed" rather than "an SSE handshake failed" and
stays true whichever backend the account uses. `test_providers_loader.py` deliberately fakes lower,
at `MCPClient`, to exercise the real loader → registry → provider → config chain.

`tests/fixtures/diarization_sample.json` is a real captured response — 79 segments, two speakers,
and three non-speech markers (two `[Music]`, one `[Environmental Sounds]`).
