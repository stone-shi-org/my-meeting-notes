# MMN-14 — MCP server for My Meeting Notes (implementation plan)

**Status:** revised after reviewer answers (2026-10-01). Waiting for explicit approval.

**Reviewer decisions:** (1) `mcp_enabled` defaults **on**. (2) Write tools are **in this ticket**. (3) ~~Search uses the app's current substring semantics~~. **Superseded:** search is delegated to **MMN-15** (FTS5 + embeddings, hybrid), which blocks this ticket and lands first. (4) OAuth goes to a follow-up ticket.
**Goal:** let MCP clients (Claude Code / Claude Desktop, Pocket Agent, …) read the meeting repository,
for example: *"get the transcript of last week's XX meeting"*.

## 1. Summary

Add a **streamable-HTTP MCP endpoint at `/mcp`** to the existing FastAPI app on port 4020. It runs in the
same process and container, with no new service. It uses the `mcp` SDK that is already a dependency
(`mcp==1.28.1` in the venv, `FastMCP` + `StreamableHTTPSessionManager`). Each tool is a thin wrapper over
existing **service-layer** functions, not over the HTTP routers. Every tool is **scoped to the
authenticated user**. Clients authenticate with a new **personal API token** (bearer), which users create
and revoke in Settings.

## 2. Transport & wiring

| Decision | Choice | Why |
|---|---|---|
| Endpoint | `POST/GET/DELETE /mcp` (exact path; `/mcp/` also accepted) | Matches the ticket. Registered **before** `_mount_spa`, because the SPA catch-all `/{full_path:path}` would otherwise shadow it. |
| Mode | `stateless_http=True`, `json_response=True` | No sticky sessions or SSE buffering through the reverse proxy. Every request carries its own auth. Works with the shared `TestClient` in tests. All tools are request/response, so there is nothing to stream. |
| Mounting | A Starlette `Route("/mcp", endpoint=ASGI wrapper around session_manager.handle_request)` instead of `app.mount("/mcp", mcp.streamable_http_app())` | `mount` + the SDK's own `/mcp` path gives `/mcp/mcp`, and a bare `/mcp` gets a 307 redirect that some clients don't follow on POST. |
| Lifecycle | `async with mcp_server.session_manager.run():` inside the existing `lifespan` | The session manager needs a task group. `run()` works only **once per instance**, so a fresh FastMCP is built inside each `create_app()` call (tests build many apps). |
| DNS-rebinding guard | Pass `TransportSecuritySettings` explicitly | **Gotcha:** FastMCP turns host-checking on by default when `host` is localhost, which 421s every request that comes in through the LAN IP or reverse proxy. The bearer token is the real gate. An optional `mcp_allowed_hosts` setting can turn the check back on. |
| On/off switch | `mcp_enabled` runtime setting (admin, `RUNTIME_KEYS`, default **on**; `/mcp` returns 404 when off) | Same pattern as the other runtime toggles. Reviewer: should it default to off? |

New code:

```
app/mcp_server/
  __init__.py      build_mcp_server() -> FastMCP ; asgi_endpoint(app)
  auth.py          bearer -> user resolution, contextvar holding CurrentUser
  tools_read.py    read-only tools (section 4)
  tools_write.py   optional write tools (section 5), behind token scope
  format.py        shared row -> dict serializers + size bounding
app/services/api_tokens.py   create / list / revoke / resolve
app/routers/api_tokens.py    /api/tokens CRUD for the Settings UI
```

## 3. Authentication: personal API tokens

Today's sessions expire after 14 days (`session_ttl_hours=336`) and are created by a password login. An
agent configured once should not break two weeks later.

* New table `api_tokens` (in `db.py` `SCHEMA`): `id, user_id FK ON DELETE CASCADE, name, token_hash
  (sha256, UNIQUE), prefix (first 8 chars for display), scope ('read'|'read_write'), created_at,
  last_used_at, expires_at NULL, revoked_at NULL`. Stored hashed, the same way `sessions.id` is. The raw
  token is shown **once**. Format: `mmn_<token_urlsafe(32)>`, so it is recognisable in logs and by secret
  scanners.
* `/mcp` auth: `Authorization: Bearer <token>`. Resolution order: API token, then an existing session token
  (so a script already holding a session also works). Rejected cases: missing or unknown token, revoked or
  expired token, inactive user, and `must_change_password`. A rejection returns **401** with
  `WWW-Authenticate: Bearer` and the usual `{"error": {...}}` body.
* `last_used_at` is written at most once a minute, so each call doesn't cost a write.
* The resolved user is placed in a `contextvar` for the request. Tools never take a user id as an
  argument.
* **Out of scope for this ticket:** MCP OAuth 2.1 / dynamic client registration, which claude.ai *remote*
  connectors need. Claude Code, Claude Desktop (via `mcp-remote`) and Pocket Agent all accept a static
  header. OAuth can follow as its own ticket.

Settings UI: a new **Settings → MCP & API tokens** tab (`web/src/...`). It covers:

* list (name, prefix, scope, created, last used), create (name, scope, optional expiry), revoke;
* a show-once dialog with a Copy button through `lib/clipboard.ts` (secure-context gotcha);
* a ready-to-paste client snippet, e.g.
  `claude mcp add --transport http mmn https://<host>/mcp --header "Authorization: Bearer mmn_…"`.

Tokens are per-user, so every user manages their own. The admin view does not show other users' tokens.

## 4. Read-only tools (phase 1)

Shared conventions:

* Every query uses the token owner's `owner_id`. Someone else's id behaves exactly like a missing one
  (`not found`), following the 404-not-403 rule. Admins get **their own** data here. There is no `all`
  flag, because an agent should never browse other users' meetings.
* Results are JSON (`structuredContent` plus a text rendering). Timestamps are ISO-8601 UTC.
  `list_*` and `search` responses include `server_time`, so a client can turn "last week" into
  `since`/`until`. `since`/`until` accept either `YYYY-MM-DD` or a full ISO timestamp.
* Lists are paged (`limit` default 20, max 100, plus `offset`). Large text is bounded (`max_chars` plus
  `next_offset`), so a 60-minute transcript doesn't overflow the client's context.
* Read tools **never** write: no `touch_thread`, no `seen_at`/`mark_seen`, no email hydration, no LLM
  calls. Reading through an agent is not activity, the same rule hydration follows.

| Tool | Args | Returns / reuses |
|---|---|---|
| `list_threads` | `query?`, `group?` (id/name/`none`), `include_archived=false`, `limit`, `offset` | id, title, description, group, meeting count, last activity, unread count. Reuses `threads.list_threads` (it already supports `q` LIKE and `group`). |
| `get_thread` | `thread_id` | Thread metadata, group, its meetings (id/title/date/status/has_summary), counts of notes/emails/events, and the cached next-step suggestion if one exists (`threads.row_to_thread`). |
| `get_thread_timeline` | `thread_id`, `limit` | The merged meetings/emails/events/notes timeline, in the same order as `GET /threads/{id}/timeline` (`matching.normalize_timestamp` sort). |
| `list_meetings` | `thread_id?`, `since?`, `until?`, `query?` (title), `status?`, `limit`, `offset` | id, title, `meeting_at`, thread, duration, status, `has_transcript`, `has_summary`, speakers. Newest first. Undated meetings fall back to `created_at`. |
| `search` | `query`, `kinds?` (⊆ `threads, meetings, transcripts`; default all), `since?`, `until?`, `thread_id?`, `limit` | **The same matching rule as the app's search today, no more:** the whole query is matched as **one case-insensitive substring** (not split into words, no ranking, no FTS, no embeddings). The fields are what the UI searches: **threads** → `title` or `description` (identical to the home-screen search bar, which reuses `threads.list_threads(q=…)`); **transcripts** → segment text (identical to the transcript page's filter, `text.toLowerCase().includes(query)`), returning each matching line with speaker name and `start_sec`; **meetings** → `title`, the same rule applied to the one thing the ticket asks for that the UI has no box for. Results are newest first. Summaries, notes and emails are **not** searched; they are reached through `get_meeting_summary` / `list_notes` / `list_emails`. |
| `get_meeting` | `meeting_id` | Metadata, thread, audio duration, pipeline status, speakers (display names, "me" flag), attached event/email/note counts, summary availability and version. |
| `get_meeting_transcript` | `meeting_id`, `format` (`text`\|`markdown`\|`vtt`, default `markdown`), `start_sec?`, `end_sec?`, `speaker?`, `include_nonspeech=false`, `offset=0`, `max_chars=40000` | Rendered through `transcript.get_transcript` + `render_*`, so **speaker names are applied at render time** (`raw_json` is never touched). Returns `{text, total_chars, next_offset, segment_count, speakers}`. With no diarization yet it returns a clear "no transcript yet (status=…)" result rather than an error. |
| `get_meeting_summary` | `meeting_id`, `version?` | The current (or given) summary: `tldr`, `summary_md`, decisions, topics, open questions, participants, action items, model, created_at. Says plainly when no summary exists, or when the latest attempt failed (`status='error'`). Never regenerates. |
| `list_action_items` | `status=open`\|`done`\|`all`, `thread_id?`, `meeting_id?`, `since?`, `limit` | Items from current summaries: text, owner, due, priority, and the meeting/thread they came from. (*"What did I commit to last week?"*) |
| `list_notes` / `get_note` | `thread_id?` or `meeting_id?` / `note_id` | Notes from `notes.list_notes` / `row_to_note`, including `source` (so an AI-written note is labelled as one). |
| `list_emails` | `thread_id`, `meeting_id?`, `group_by_conversation=true`, `include_body=false` | Attached emails with `direction` (outbound/inbound/**unknown**, never guessed), `ai_summary`, snippet. Grouped through `email_chains.build_chains`, which is safe here because the scope is a whole thread. A body is included only if it is already stored. Hydration is never triggered. |
| `get_email` | `email_id` | One email, with its stored body if any (`body_fetched_at` set + NULL body = "provider cannot supply"). |
| `list_calendar_events` | `thread_id` or `meeting_id` | Attached calendar events: summary, start/end (all-day kept as a bare date), location, attendees. |
| `get_upcoming_events` | `days=7` (max 30) | **Live** read of the user's connected calendars through `upcoming.collect`, including which events are already attached to a thread. The only tool that calls a provider, and it is read-only. Provider errors come back as an `error` field, not an exception. |

Additional suggestions (the ticket's "other tools you think might be important"): `get_thread_timeline`,
`list_action_items`, `get_upcoming_events`, `get_email`, and `search` with transcript-line snippets.
**MCP resources:** `mmn://meetings/{id}/transcript` and `mmn://meetings/{id}/summary`, so clients that
support resources can attach them directly. **One MCP prompt:** `meeting_brief(meeting_id)`.
Resources and the prompt are small and can be dropped if the reviewer prefers tools only.

## 5. Write tools (in this ticket, per reviewer)

These are available only to `scope='read_write'` tokens. They are hidden from `tools/list` for read
tokens, not merely refused.

* `create_note(thread_id | meeting_id, body, title?)`. Reuses `notes.create_note`, including the "title
  generation cannot fail the save" path, with `source='mcp'`.
* `append_to_note(note_id, body)`. Reuses `notes.append_to_note`.
* `set_action_item_status(item_id, status)`. Same rules as `PATCH /action-items/{id}`.

Deliberately **excluded**: uploading audio, deleting anything, regenerating summaries (LLM spend),
triggering matching or hydration, and changing settings or integrations.


## 6. Errors

Tool failures return MCP tool errors (`isError: true`) with the app's error `code` and `message`
(`not_found`, `validation_error`, …), mapped from `AppError`. Unexpected exceptions are logged and
reported as a generic `internal_error`, without a stack trace. Auth failures happen at the HTTP layer
(401), before MCP dispatch.

## 7. Tests (`tests/test_mcp_server.py`, `tests/test_api_tokens.py`)

These use the shared `TestClient` from `conftest.py`, with JSON-RPC `POST /mcp` (stateless JSON mode),
and stay offline.

* Auth: no header, bad token, revoked token, expired token, inactive user and `must_change_password` all
  give 401. A session bearer works. `last_used_at` is throttled.
* Protocol: `initialize`, `tools/list` (read token does not see write tools), `tools/call` round trip.
  Both `/mcp` and `/mcp/` work. A request through a non-localhost `Host` is not 421'd.
  `mcp_enabled=false` returns 404.
* Isolation: user B's token gets `not_found` for every id-taking tool pointed at user A's objects
  (parametrised over all tools). An admin sees only their own data.
* Content: transcript renders `speaker_map` names, and the test asserts the `raw_json` bytes are unchanged.
  Transcript paging (`next_offset`) and time windowing. Summary: absent / error / current / specific
  version. `search` finds a transcript line and returns `start_sec`, matches case-insensitively, treats a multi-word query as one phrase (same as the home bar), and does not match summary, note or email text. `since`/`until` filtering, including
  undated meetings. Email `direction=NULL` is rendered as `unknown`.
* No side effects: after a full read sweep, `threads.updated_at`, `seen_at` and `body_fetched_at` are
  unchanged and no LLM route was hit (respx asserts zero calls).
* `get_upcoming_events` uses the monkeypatched provider at `providers.loader.load_for_user`.
* Write tools: scope enforcement, note created with `source='mcp'`, title fallback when the
  LLM fails.
* Frontend: vitest for the tokens tab (create shows the token once, revoke, copy fallback).

Verification: `./test.sh` (pytest under xdist + vitest), then a Bamboo build of the branch, then a manual
smoke test with `claude mcp add --transport http …` against a dev instance, plus the MCP Inspector.

## 8. Docs

* `CLAUDE.md`: new **MCP server** section (stateless JSON mode and why, route-before-SPA, the
  one-`run()`-per-instance lifecycle, the DNS-rebinding default, read tools never write, token hashing).
  Update the shape tree.
* `README.md`: how to create a token and connect Claude Code / Desktop / Pocket Agent.
* Confluence space **MMN**: a new page "MCP Server & API Tokens" under My Meeting Notes, plus updates to
  Architecture Overview and Gotchas Reference, following the CLAUDE.md "keep Confluence in sync" rule.

## 9. Decisions (answered 2026-10-01)

1. `mcp_enabled` defaults **on**.
2. Write tools (`create_note`, `append_to_note`, `set_action_item_status`) ship **in this ticket**, behind `read_write` tokens.
3. Search = a thin wrapper over **MMN-15**'s `services/search.py`. It exposes `query`, `kinds`, `mode` (hybrid|keyword|semantic), `since`/`until`/`thread_id`, `limit`/`offset`, across threads, meetings, transcript segments (with `start_sec`), summaries, action items, notes, emails and events. MMN-15 must be merged before this ticket starts.
4. OAuth 2.1 for claude.ai remote connectors goes to a **follow-up ticket**.

## 10. Rough sequence

1. `api_tokens` table + service + `/api/tokens` router + tests.
2. `app/mcp_server` skeleton: auth wrapper, lifespan, route registration, `mcp_enabled`, protocol tests.
3. Read tools + isolation and no-side-effect tests.
4. Write tools.
5. Settings tab (tokens + connection snippet) + vitest.
6. Docs (CLAUDE.md, README, Confluence), `./test.sh`, rebase on main, commit `[MMN-14] Feature: MCP server …`.
