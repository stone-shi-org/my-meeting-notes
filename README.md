# Meeting Notes

Upload a recording → convert → diarize → summarize with action items → file it under a **thread**
that also carries the matching emails and calendar invites.

A thread is one ongoing piece of work. It holds many meetings, plus the calendar events and email
threads that surround them, on a single timeline.

---

## Quick start

```bash
cp .env.example .env      # fill in the API key and MCP tokens
mkdir -p data
docker compose up -d --build
```

Open <http://localhost:4020> and sign in with **admin / password**. You will be asked to choose a
new password immediately; nothing else in the app is reachable until you do.

## What it does

| | |
|---|---|
| **Upload** | Any audio ffmpeg can read. Already-conformant 16 kHz mono WAV is used as-is; anything else is converted. Both the original and the converted file are kept. |
| **Diarize** | Posts to an OpenAI-compatible `/v1/audio/diarization` endpoint and stores the response **verbatim**. |
| **Summarize** | An LLM writes a summary, decisions, open questions and action items. Runs automatically, and can be regenerated at any time. |
| **Rename speakers** | `SPEAKER_00` → `Donna`, applied at render time. The stored diarization is never rewritten. |
| **Match** | Searches your calendar and email over MCP, has the LLM rank what comes back, and you tick what to attach. |

## Configuration

Everything below is editable at runtime from **Settings**; `.env` only supplies the initial values.

| Variable | Purpose |
|---|---|
| `MMN_BOOTSTRAP_ADMIN_PASSWORD` | First-boot admin password. Ignored once a user exists. |
| `MMN_SESSION_COOKIE_SECURE` | Leave `false` on plain HTTP. Set `true` behind an HTTPS proxy — otherwise the browser will not send the cookie and login silently fails. |
| `MMN_DIARIZATION_URL` / `_MODEL` / `_API_KEY` | The transcription service. |
| `MMN_LLM_BASE_URL` / `_MODEL` / `_API_KEY` | Any OpenAI-compatible endpoint. |
| `MMN_MCP_CALENDAR_URL` / `_TOKEN` | Calendar MCP server (SSE). |
| `MMN_MCP_EMAIL_URL` / `_TOKEN` | Email MCP server (SSE). |
| `MMN_MCP_PROFILE` | Account profile passed to both MCP tools. |
| `MMN_JOB_CONCURRENCY` | Background workers. Default 2. |

Use **Settings → Integrations → Test connection** to verify each MCP server. It reports the failure
you actually hit — wrong token, wrong port, unreachable host, or a live server exposing the wrong
tools — rather than a generic error.

### Configuring per-user accounts

`MMN_MCP_PROFILE` / `MMN_MCP_CALENDAR_TOKEN` / `MMN_MCP_EMAIL_TOKEN` set the **shared** account —
what everyone searches by default. That's the right setup for a single-household deployment where
one set of real accounts sits behind many app logins.

If different app users have their own calendar/email accounts (e.g. each person runs their own
profile on the calendarmcp / email-triage servers), each of them can point their own meetings at
their own account instead:

1. Sign in as that user and go to **Settings → Integrations**.
2. Under **Your account**, click **Use my own account** next to Calendar or Email.
3. Enter the **profile name** your MCP administrator assigned you (e.g. your username on the
   calendarmcp/email-triage server).
4. If your profile also has its own bearer token (a separate `CALENDAR_PROFILE_TOKEN` /
   `X-Profile-Token` from the shared one), paste it too. Leave it blank to authenticate with the
   shared server token and only switch which account is searched.
5. Click **Test** to confirm before saving.

With no override, a user's meetings search the shared account. Clicking **Use shared account
instead** removes the override entirely.

An admin can also set this up on a user's behalf — useful since the profile/token usually has to be
provisioned first by whoever administers the calendarmcp/email-triage servers:

```bash
curl -X PUT http://localhost:4020/api/users/<user_id>/mcp-profiles/calendar \
  -H 'Content-Type: application/json' -H "Cookie: mmn_session=<admin session cookie>" \
  -d '{"profile": "jenny", "auth_token": "jenny-profile-token-or-omit-for-shared"}'
```

## Development

The app is built and run in Docker, but the fast loop runs the frontend on the host:

```bash
docker compose up -d          # backend on :4020
cd web && npm run dev         # Vite on :5173, proxying /api to :4020
```

Backend edits need a rebuild unless you add a `docker-compose.override.yml` that bind-mounts `./app`
and runs `uvicorn --reload`.

## Tests

```bash
./test.sh                 # backend (pytest) + frontend (vitest)
./test.sh --backend-only
./test.sh --docker        # run both suites in containers
```

JUnit XML lands in `test-reports/` for the Atlassian Bamboo JUnit Parser task
(result pattern `**/test-reports/*.xml`). Reports are written even when tests fail, and the script
exits non-zero — add the parser as a **final** task so it runs regardless.

## Release

```bash
./build.sh          # build + tag latest and <commit>
./build.sh -p       # also push to registry.example.com/homestack
```

`build.sh` writes `version.txt` into the image, which `/api/version` reports — so a running
container can always be traced back to a commit.

## Notes

- **Diarization is slow.** A 22-minute recording takes minutes. Uploads return `202` with a job id
  immediately; the job survives navigation, refresh and a container restart.
- **Audio is served with HTTP Range**, which is what makes the player's seek bar work.
- `data/` holds the SQLite database and the audio archive. It is bind-mounted, so back it up by
  copying the directory. The container runs as root, so you may need `sudo` to delete it from the host.
