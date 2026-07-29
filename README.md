# Meeting Notes

Upload a recording → convert → diarize → summarize with action items → file it under a **thread**
that also carries the matching emails and calendar invites.

A thread is one ongoing piece of work. It holds many meetings, plus the calendar events and email
threads that surround them, on a single timeline.

---

## Quick start

```bash
cp .env.example .env      # fill in the diarization + LLM endpoints
mkdir -p data
docker compose up -d --build
```

Open <http://localhost:4020> and sign in with **admin / password**. You will be asked to choose a
new password immediately; nothing else in the app is reachable until you do.

## What it does

| | |
|---|---|
| **Upload** | Any audio ffmpeg can read. Already-conformant 16 kHz mono WAV is used as-is; anything else is converted. Both the original and the converted file are kept. |
| **Record** | Record straight into the page instead — microphone, or the audio of a browser tab with your own mic mixed in. See below for what each browser can actually capture. |
| **Diarize** | Posts to an OpenAI-compatible `/v1/audio/diarization` endpoint and stores the response **verbatim**. |
| **Summarize** | An LLM writes a summary, decisions, open questions and action items. Runs automatically, and can be regenerated at any time. |
| **Rename speakers** | `SPEAKER_00` → `Donna`, applied at render time. The stored diarization is never rewritten. |
| **Match** | Searches the calendar and email accounts *you* connected, has the LLM rank what comes back, and you tick what to attach. |
| **Upcoming** | The home screen lists the next two weeks across every connected calendar. Create a meeting from any event and its title, time and invitees are prefilled and the event is attached. |

## Configuration

Everything below is editable at runtime from **Settings**; `.env` only supplies the initial values.

| Variable | Purpose |
|---|---|
| `MMN_BOOTSTRAP_ADMIN_PASSWORD` | First-boot admin password. Ignored once a user exists. |
| `MMN_SESSION_COOKIE_SECURE` | Leave `false` on plain HTTP. Set `true` behind an HTTPS proxy — otherwise the browser will not send the cookie and login silently fails. |
| `MMN_DIARIZATION_URL` / `_MODEL` / `_API_KEY` | The transcription service. |
| `MMN_LLM_BASE_URL` / `_MODEL` / `_API_KEY` | Any OpenAI-compatible endpoint. |
| `MMN_SECRET_KEY` | Encrypts connected-account credentials. Optional: one is generated into `data/secret.key` on first boot. |
| `MMN_PUBLIC_BASE_URL` | Where the app is reachable, used to build OAuth redirect URIs. |
| `MMN_GOOGLE_CLIENT_ID` / `_SECRET` | Google OAuth client, if you want Gmail/Calendar. |
| `MMN_JOB_CONCURRENCY` | Background workers. Default 2. |
| `MMN_AUTO_MATCH_ENABLED` | Watch threads for follow-ups on a timer. Off by default — see below. |

Each connected account has a **Test** button in **Settings → Integrations**. It reports each leg
separately — an account can reach a calendar while its mailbox login is rejected — and names the
failure you actually hit rather than a generic error.

### Recording in the browser

**New meeting → Record now.** The clip goes through exactly the same pipeline as an upload.

What you can capture depends on the browser and the OS, and not in an obvious way:

| Source | Chrome / Edge | Firefox | Safari |
|---|---|---|---|
| **Microphone**, with a device picker | ✅ | ✅ | ✅ |
| **A browser tab's audio** — the Meet/Zoom/Teams web call | ✅ **including macOS** | ✕ video only | ✕ video only |
| **Whole desktop audio** | ✅ Windows, ChromeOS · **✕ macOS** | ✕ | ✕ |
| **One native app's audio** | ✕ nowhere — no web API exists for it | ✕ | ✕ |

Two consequences worth knowing:

- **On a Mac, tab audio is the one screen-share audio that works.** Chrome gets nothing from macOS for
  "Entire screen" or a single window, so those record silence. Tab capture is unaffected, and it is
  what you want for a browser-based call anyway.
- **To record a native app on macOS** — the desktop Zoom or Teams client — install a loopback device
  (BlackHole, Loopback, Audio Hijack), send the app's output to it, and select it under
  **Microphone**. It shows up there like any other input.

When capturing a tab, leave **Also record my microphone** ticked, or you capture everyone except
yourself. Watch the level meter for the first few seconds: a flat meter means the share was set up
without its audio, which is the usual mistake and produces a perfectly valid recording of silence.

### Automatic follow-ups

Matching is normally something you ask for: upload a recording, press **Find matches**, tick what
belongs. With **Settings → Matching → Watch threads for follow-ups** switched on, the app also does
it on a timer — every 30 minutes by default, per thread — and attaches anything it is confident
about without asking. A thread that gained something shows a blue dot; inside it, the new email or
event is bold and marked *New* until you open it, and opening the link clears both.

Three properties make that safe to leave on:

- **It attaches at 0.8, not the 0.6 it suggests at.** The threshold is configurable; below about 0.7
  expect noise.
- **It never touches a meeting.** Auto-attached items belong to the thread, so a summary is never
  regenerated from something nobody confirmed.
- **No language model, no attaching.** If the LLM is unavailable the sweep finds candidates, cannot
  score them, and does nothing.

It is off by default because it spends LLM tokens and provider quota on its own schedule. Threads
that are archived, or that nobody has touched in 30 days, stop being watched. **Check now** on a
thread runs exactly the same sweep immediately.

### Connecting a calendar and inbox

Calendars and inboxes are **per-user**. Nothing is connected by default and no account is shared:
each person adds their own under **Settings → Integrations**, and one user can never see or search
another's. A user with nothing connected simply has the match feature greyed out.

Credentials are encrypted at rest with the key in `MMN_SECRET_KEY`, or one generated into
`data/secret.key` on first boot. **Back that file up.** Losing it does not break the app, but every
connected account flips to "reconnect needed". Be clear-eyed about what it buys you: unless you set
`MMN_SECRET_KEY` yourself, the key sits in the same `data/` volume as the database, so it protects a
stolen database copy and not much else. The app refuses to start if both exist and disagree, rather
than silently picking one and making every stored credential unreadable.

#### Google (Gmail + Calendar)

An admin registers one OAuth client for the whole app; each user then authorises their own Google
account against it.

1. In [Google Cloud Console](https://console.cloud.google.com/apis/credentials), create an **OAuth
   client ID** of type *Web application*.
2. Add an authorised redirect URI of `<public base url>/api/integrations/oauth/google/callback`.
   Google accepts `https://` or `http://localhost` only — a LAN address like
   `http://10.0.0.5:4020` is **rejected**, so either reach the app over localhost (an SSH tunnel is
   fine) or put it behind HTTPS.
3. Enable the **Google Calendar API** and the **Gmail API** for the project.
4. Set the OAuth consent screen's publishing status to **In production**. This matters: while it is
   in *Testing*, Google expires refresh tokens after 7 days and everyone has to reconnect weekly.
   Publishing unverified is enough to stop that clock — you will click past an "unverified app"
   warning when connecting, which is expected for a self-hosted tool.
5. Paste the client ID and secret into **Settings → Integrations → Google sign-in**, along with the
   public base URL.

Users then click **Add integration → Google → Continue**. The app asks for read-only Calendar and
Gmail access. `gmail.readonly` is a *restricted* scope; there is no lighter alternative, because the
metadata-only scope forbids the search query the feature is built on.

#### Apple iCloud (Calendar + Mail)

Apple offers no OAuth for iCloud, so this uses your Apple ID with an
**app-specific password** — generate one at [appleid.apple.com](https://appleid.apple.com/account/manage)
under *Sign-In and Security*. Your actual Apple ID password will be rejected.

Add it under **Add integration → Apple iCloud**. Calendar goes over CalDAV and mail over IMAP, and
the Test button reports each leg separately, because one commonly works while the other does not.

Two limits worth knowing: iCloud has no per-message or per-event web URL, so attached items are not
clickable through to a web UI, and mail is fetched headers-only, so emails have no snippet.

#### Zoho (Mail + Calendar)

Like Google, an admin registers one client and each user authorises their own account.

1. Create a client at [api-console.zoho.com](https://api-console.zoho.com/) of type
   *Server-based Application*, with the callback
   `<public base url>/api/integrations/oauth/zoho/callback`.
2. Paste the client ID and secret into **Settings → Integrations → Zoho sign-in**, and set the
   **data centre** to match where the accounts live — `com`, `eu`, `in`, `com.au` or `jp`. Getting
   this wrong is unhelpfully quiet: requests authenticate fine and simply return nothing.

Zoho caps how many refresh tokens a client may hold at once, so repeatedly disconnecting and
reconnecting eventually invalidates the oldest ones silently.

#### MCP calendar/email servers

If you run the calendarmcp / email-triage MCP servers, add them under **Add integration → Calendar
MCP server** (or Email). Each user supplies the server URL, their own profile name on it, and a
token. Upgrading from a version before per-user integrations migrates existing shared config onto
every existing account automatically, so nothing stops working.

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
