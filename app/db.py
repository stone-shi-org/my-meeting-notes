"""SQLite access and schema.

The whole schema lives in :func:`init_db` as idempotent ``CREATE TABLE IF NOT EXISTS``
statements plus best-effort ``ALTER TABLE`` for columns added later — the same
append-only pattern as ~/src/email-triage/db.py. There is no migration tool.

Connections are short-lived: open one, do the work, close it. Never hold a
transaction across an ``await`` or an HTTP call.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.config import get_settings


def utcnow() -> str:
    """ISO-8601 UTC timestamp. The only way timestamps are produced in this app."""
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    if db_path is None:
        db_path = get_settings().db_path
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def get_conn(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA: tuple[str, ...] = (
    # ---------------------------------------------------------------- users
    """
    CREATE TABLE IF NOT EXISTS users (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        username              TEXT NOT NULL UNIQUE COLLATE NOCASE,
        display_name          TEXT,
        password_hash         TEXT NOT NULL,
        password_salt         TEXT NOT NULL,
        password_algo         TEXT NOT NULL DEFAULT 'scrypt',
        password_params       TEXT NOT NULL DEFAULT 'n=16384,r=8,p=1,dklen=32',
        is_admin              INTEGER NOT NULL DEFAULT 0,
        is_active             INTEGER NOT NULL DEFAULT 1,
        must_change_password  INTEGER NOT NULL DEFAULT 0,
        created_at            TEXT NOT NULL,
        updated_at            TEXT NOT NULL,
        last_login_at         TEXT
    )
    """,
    # ------------------------------------------------------------- sessions
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id            TEXT PRIMARY KEY,          -- sha256 of the raw token
        user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at    TEXT NOT NULL,
        expires_at    TEXT NOT NULL,
        last_seen_at  TEXT,
        user_agent    TEXT,
        ip            TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_exp ON sessions(expires_at)",
    # -------------------------------------------------------- thread_groups
    # A folder over threads, owned by one user and named by them. Membership is
    # a nullable `threads.group_id`, so "Ungrouped" is not a row here -- it is
    # the absence of one, which is what makes it the default for every thread
    # that has ever existed and the fallback when a group is deleted.
    #
    # UNIQUE on the name because the name *is* how a group is identified on the
    # home screen: two folders both called "Clients" is a bug report, not a
    # feature. NOCASE for the same reason -- "clients" is not a second folder.
    """
    CREATE TABLE IF NOT EXISTS thread_groups (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name       TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_thread_group_name "
    "ON thread_groups(owner_id, name COLLATE NOCASE)",
    # -------------------------------------------------------------- threads
    """
    CREATE TABLE IF NOT EXISTS threads (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id     INTEGER NOT NULL REFERENCES users(id),
        title        TEXT NOT NULL,
        description  TEXT,
        archived     INTEGER NOT NULL DEFAULT 0,
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_threads_owner_updated ON threads(owner_id, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_threads_title ON threads(title)",
    # ------------------------------------------------------------- meetings
    """
    CREATE TABLE IF NOT EXISTS meetings (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        thread_id             INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
        owner_id              INTEGER NOT NULL REFERENCES users(id),
        title                 TEXT NOT NULL,
        meeting_at            TEXT,
        status                TEXT NOT NULL DEFAULT 'new',
        original_filename     TEXT,
        original_path         TEXT,
        original_mime         TEXT,
        original_bytes        INTEGER,
        audio_path            TEXT,
        audio_converted       INTEGER NOT NULL DEFAULT 0,
        audio_duration_sec    REAL,
        audio_sample_rate     INTEGER,
        audio_channels        INTEGER,
        active_diarization_id INTEGER REFERENCES diarizations(id),
        active_summary_id     INTEGER REFERENCES summaries(id),
        notes                 TEXT,
        created_at            TEXT NOT NULL,
        updated_at            TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_meetings_thread ON meetings(thread_id, meeting_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_meetings_owner ON meetings(owner_id, created_at DESC)",
    # --------------------------------------------------------- diarizations
    # raw_json is written once and never updated. Speaker renames live in
    # speaker_map and are applied at render time.
    """
    CREATE TABLE IF NOT EXISTS diarizations (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id    INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
        provider_url  TEXT NOT NULL,
        model         TEXT NOT NULL,
        raw_json      TEXT NOT NULL,
        json_path     TEXT,
        duration_sec  REAL,
        num_speakers  INTEGER,
        segment_count INTEGER,
        request_ms    INTEGER,
        created_at    TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_diar_meeting ON diarizations(meeting_id, created_at DESC)",
    # ---------------------------------------------------------- speaker_map
    """
    CREATE TABLE IF NOT EXISTS speaker_map (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id   INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
        speaker_id   TEXT NOT NULL,
        label        TEXT,
        display_name TEXT,
        color        TEXT,
        sort_order   INTEGER,
        source       TEXT DEFAULT 'user',
        updated_at   TEXT
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_speaker_map ON speaker_map(meeting_id, speaker_id)",
    # ------------------------------------------------------------ summaries
    """
    CREATE TABLE IF NOT EXISTS summaries (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id          INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
        version             INTEGER NOT NULL,
        is_current          INTEGER NOT NULL DEFAULT 0,
        status              TEXT NOT NULL DEFAULT 'ok',
        model               TEXT NOT NULL,
        llm_base_url        TEXT,
        temperature         REAL,
        prompt_name         TEXT NOT NULL,
        prompt_version      TEXT,
        prompt_sha256       TEXT NOT NULL,
        prompt_text         TEXT NOT NULL,
        diarization_id      INTEGER REFERENCES diarizations(id),
        transcript_sha256   TEXT,
        tldr                TEXT,
        summary_md          TEXT,
        title_suggestion    TEXT,
        key_decisions_json  TEXT,
        topics_json         TEXT,
        open_questions_json TEXT,
        participants_json   TEXT,
        raw_response        TEXT,
        prompt_tokens       INTEGER,
        completion_tokens   INTEGER,
        duration_sec        REAL,
        error               TEXT,
        created_by          INTEGER REFERENCES users(id),
        created_at          TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_summary_version ON summaries(meeting_id, version)",
    "CREATE INDEX IF NOT EXISTS idx_summary_current ON summaries(meeting_id, is_current)",
    # --------------------------------------------------------- action_items
    """
    CREATE TABLE IF NOT EXISTS action_items (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        summary_id       INTEGER NOT NULL REFERENCES summaries(id) ON DELETE CASCADE,
        meeting_id       INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
        idx              INTEGER NOT NULL,
        text             TEXT NOT NULL,
        owner_label      TEXT,
        owner_speaker_id TEXT,
        due_text         TEXT,
        due_date         TEXT,
        priority         TEXT,
        confidence       REAL,
        status           TEXT NOT NULL DEFAULT 'open',
        done_at          TEXT,
        created_at       TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ai_meeting ON action_items(meeting_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_ai_summary ON action_items(summary_id, idx)",
    # ----------------------------------------------------------------- jobs
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id               TEXT PRIMARY KEY,
        type             TEXT NOT NULL,
        status           TEXT NOT NULL,
        stage            TEXT,
        progress         REAL NOT NULL DEFAULT 0,
        meeting_id       INTEGER REFERENCES meetings(id) ON DELETE CASCADE,
        thread_id        INTEGER REFERENCES threads(id) ON DELETE CASCADE,
        user_id          INTEGER NOT NULL REFERENCES users(id),
        payload_json     TEXT,
        result_json      TEXT,
        error            TEXT,
        error_stage      TEXT,
        error_trace      TEXT,
        attempts         INTEGER NOT NULL DEFAULT 0,
        max_attempts     INTEGER NOT NULL DEFAULT 1,
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        created_at       TEXT NOT NULL,
        started_at       TEXT,
        finished_at      TEXT,
        updated_at       TEXT,
        heartbeat_at     TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_meeting ON jobs(meeting_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id, created_at DESC)",
    # ----------------------------------------------------------- job_events
    """
    CREATE TABLE IF NOT EXISTS job_events (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id   TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        ts       TEXT NOT NULL,
        stage    TEXT,
        level    TEXT NOT NULL DEFAULT 'info',
        message  TEXT NOT NULL,
        progress REAL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_job_events ON job_events(job_id, id)",
    # -------------------------------------------------------- thread_emails
    """
    CREATE TABLE IF NOT EXISTS thread_emails (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        thread_id         INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
        meeting_id        INTEGER REFERENCES meetings(id) ON DELETE SET NULL,
        mcp_id            TEXT,
        message_id        TEXT,
        sender            TEXT,
        subject           TEXT,
        date              TEXT,
        snippet           TEXT,
        account           TEXT,
        triage_level      INTEGER,
        tag               TEXT,
        reason            TEXT,
        summary           TEXT,
        score             REAL,
        raw_json          TEXT NOT NULL,
        relevance_score   REAL,
        relevance_reason  TEXT,
        attached_by       INTEGER REFERENCES users(id),
        attached_at       TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_thread_email ON thread_emails(thread_id, message_id)",
    # ----------------------------------------------- thread_calendar_events
    """
    CREATE TABLE IF NOT EXISTS thread_calendar_events (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        thread_id         INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
        meeting_id        INTEGER REFERENCES meetings(id) ON DELETE SET NULL,
        uid               TEXT NOT NULL,
        url               TEXT,
        summary           TEXT,
        description       TEXT,
        location          TEXT,
        start_at          TEXT,
        end_at            TEXT,
        calendar_name     TEXT,
        account           TEXT,
        event_type        TEXT,
        raw_json          TEXT NOT NULL,
        relevance_score   REAL,
        relevance_reason  TEXT,
        attached_by       INTEGER REFERENCES users(id),
        attached_at       TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_thread_event ON thread_calendar_events(thread_id, uid)",
    "CREATE INDEX IF NOT EXISTS idx_tce_timeline ON thread_calendar_events(thread_id, start_at)",
    # --------------------------------------------------------- thread_notes
    #
    # The third kind of document on a thread, alongside emails and calendar
    # events -- but authored here rather than fetched from a provider, so there
    # is no uid/raw_json/relevance and no unique index to dedupe against: two
    # notes with the same title are two notes.
    #
    # `meeting_id` follows the same rule as the other two: NULL means the note
    # belongs to the thread as a whole, and ON DELETE SET NULL keeps a note
    # alive when the meeting it was taken on is deleted.
    """
    CREATE TABLE IF NOT EXISTS thread_notes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        thread_id   INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
        meeting_id  INTEGER REFERENCES meetings(id) ON DELETE SET NULL,
        title       TEXT NOT NULL,
        body        TEXT NOT NULL,
        source      TEXT NOT NULL,
        model       TEXT,
        title_model TEXT,
        created_by  INTEGER REFERENCES users(id),
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_notes_thread ON thread_notes(thread_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_notes_meeting ON thread_notes(meeting_id)",
    # ----------------------------------------------------------- match_runs
    """
    CREATE TABLE IF NOT EXISTS match_runs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id      INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
        thread_id       INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
        user_id         INTEGER NOT NULL REFERENCES users(id),
        job_id          TEXT REFERENCES jobs(id) ON DELETE SET NULL,
        status          TEXT NOT NULL,
        query_json      TEXT,
        candidates_json TEXT,
        ranked_json     TEXT,
        model           TEXT,
        prompt_sha256   TEXT,
        email_error     TEXT,
        calendar_error  TEXT,
        error           TEXT,
        created_at      TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_match_meeting ON match_runs(meeting_id, created_at DESC)",
    # -------------------------------------------------------- chat_messages
    """
    CREATE TABLE IF NOT EXISTS chat_messages (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        thread_id          INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
        owner_id           INTEGER NOT NULL REFERENCES users(id),
        role               TEXT NOT NULL,
        content            TEXT NOT NULL,
        model              TEXT,
        prompt_tokens      INTEGER,
        completion_tokens  INTEGER,
        created_at         TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_chat_thread ON chat_messages(thread_id, created_at)",
    # -------------------------------------------------------- meeting_chat_messages
    """
    CREATE TABLE IF NOT EXISTS meeting_chat_messages (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id         INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
        owner_id           INTEGER NOT NULL REFERENCES users(id),
        role               TEXT NOT NULL,
        content            TEXT NOT NULL,
        model              TEXT,
        prompt_tokens      INTEGER,
        completion_tokens  INTEGER,
        created_at         TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_meeting_chat_meeting ON meeting_chat_messages(meeting_id, created_at)",
    # -------------------------------------------------------- home_chat_messages
    """
    CREATE TABLE IF NOT EXISTS home_chat_messages (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id           INTEGER NOT NULL REFERENCES users(id),
        role               TEXT NOT NULL,
        content            TEXT NOT NULL,
        model              TEXT,
        prompt_tokens      INTEGER,
        completion_tokens  INTEGER,
        created_at         TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_home_chat_owner ON home_chat_messages(owner_id, created_at)",
    # ---------------------------------------------------------- mcp_servers
    """
    CREATE TABLE IF NOT EXISTS mcp_servers (
        name                  TEXT PRIMARY KEY,
        kind                  TEXT NOT NULL,
        transport             TEXT NOT NULL,
        enabled               INTEGER NOT NULL DEFAULT 1,
        base_url              TEXT,
        auth_token            TEXT,
        command               TEXT,
        args_json             TEXT,
        cwd                   TEXT,
        env_json              TEXT,
        tool_name             TEXT NOT NULL,
        default_profile       TEXT,
        timeout_sec           INTEGER NOT NULL DEFAULT 60,
        last_test_at          TEXT,
        last_test_ok          INTEGER,
        last_test_error       TEXT,
        last_test_tools_json  TEXT,
        created_at            TEXT NOT NULL,
        updated_at            TEXT NOT NULL
    )
    """,
    # --------------------------------------------------------- app_settings
    """
    CREATE TABLE IF NOT EXISTS app_settings (
        key        TEXT PRIMARY KEY,
        value      TEXT,
        value_type TEXT NOT NULL DEFAULT 'str',
        is_secret  INTEGER NOT NULL DEFAULT 0,
        updated_by INTEGER REFERENCES users(id),
        updated_at TEXT
    )
    """,
    # ------------------------------------------- legacy MCP config (unread)
    # Superseded by `integrations`. Nothing but the one-time migration in
    # services/integrations.py reads these now, and they are kept rather than
    # dropped for two reasons: the migration has to keep working for anyone
    # upgrading from before the provider refactor, and DROP buys nothing on a
    # single-file SQLite database while costing the rollback path.
    # ---------------------------------------------------- user_mcp_profiles
    # Per-user override of *which account* a shared MCP server searches.
    # profile/auth_token here take precedence over mcp_servers.default_profile
    # /auth_token for that one user; absent a row, everyone shares the server's
    # default account, matching single-household deployments where the app has
    # many logins but one calendar/email behind it.
    """
    CREATE TABLE IF NOT EXISTS user_mcp_profiles (
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        server_name TEXT NOT NULL REFERENCES mcp_servers(name) ON DELETE CASCADE,
        profile     TEXT NOT NULL,
        auth_token  TEXT,
        updated_at  TEXT NOT NULL,
        PRIMARY KEY (user_id, server_name)
    )
    """,
    # --------------------------------------------------------- integrations
    # One row per connected *account*, not per capability: a credential has a
    # single lifecycle (expiry, rotation, revoke, reauth), so splitting one
    # Google OAuth grant into a calendar row and an email row would mean two
    # copies of one rotating refresh token and two independent refresh races.
    # calendar_enabled/email_enabled say which capabilities to actually use.
    #
    # UNIQUE is on account_key -- a stable provider-supplied id -- and never on
    # account_label: labels are renameable, and SQLite treats NULLs as distinct,
    # so a nullable label would let repeated "Connect" clicks pile up rows.
    """
    CREATE TABLE IF NOT EXISTS integrations (
        id                        INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id                   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        provider                  TEXT NOT NULL,
        account_key               TEXT NOT NULL,
        account_label             TEXT,
        calendar_enabled          INTEGER NOT NULL DEFAULT 0,
        email_enabled             INTEGER NOT NULL DEFAULT 0,
        enabled                   INTEGER NOT NULL DEFAULT 1,
        auth_type                 TEXT NOT NULL,
        config_json               TEXT NOT NULL DEFAULT '{}',
        secret_json               TEXT,
        secret_version            INTEGER NOT NULL DEFAULT 1,
        scopes                    TEXT,
        token_expires_at          TEXT,
        refresh_token_obtained_at TEXT,
        refresh_lease_until       TEXT,
        status                    TEXT NOT NULL DEFAULT 'unverified',
        last_test_at              TEXT,
        last_test_ok              INTEGER,
        last_test_error           TEXT,
        created_at                TEXT NOT NULL,
        updated_at                TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_integration_account "
    "ON integrations(user_id, provider, account_key)",
    "CREATE INDEX IF NOT EXISTS idx_integrations_user ON integrations(user_id, enabled)",
    # ------------------------------------------------- dev_emails/dev_events
    # The Development provider's hand-authored inbox and calendar. These are a
    # *source*, upstream of matching -- deliberately not thread_emails /
    # thread_calendar_events, which hold what has already been attached. Writing
    # fake data straight into those would skip the ranking, threshold and attach
    # logic that is the whole reason for having a fake source.
    #
    # Scoped to an integration rather than a user, so two Development accounts
    # have two separate inboxes -- which is what makes "aggregate an error only
    # when every account of a kind failed" testable. CASCADE because an inbox
    # belonging to no account means nothing; app/services/dev_data.py has a JSON
    # export for keeping authored fixtures across a disconnect.
    #
    # When each item happens is `date_mode`, and it is the reason these are not
    # a JSON fixture file:
    #   absolute  -- `at` verbatim, for pinning an exact reproduction
    #   relative  -- now + offset_minutes, e.g. "an email from yesterday"
    #   anchored  -- a meeting's meeting_at + offset_minutes
    # Only the last two survive contact with time. An absolute date drops out of
    # the 60/60 match window within a couple of months and the fixture quietly
    # stops testing anything.
    """
    CREATE TABLE IF NOT EXISTS dev_emails (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        integration_id    INTEGER NOT NULL REFERENCES integrations(id) ON DELETE CASCADE,
        subject           TEXT NOT NULL,
        sender            TEXT,
        snippet           TEXT,
        account           TEXT,
        date_mode         TEXT NOT NULL DEFAULT 'relative',
        at                TEXT,
        offset_minutes    INTEGER,
        anchor_meeting_id INTEGER REFERENCES meetings(id) ON DELETE SET NULL,
        -- Emit the date as RFC 2822 the way Gmail does, rather than ISO-8601.
        -- Stored raw those sort lexically above every ISO date; this is how you
        -- exercise matching.normalize_timestamp on purpose.
        rfc2822_date      INTEGER NOT NULL DEFAULT 0,
        -- Ground truth: should a correct matcher pick this up? Nothing branches
        -- on it -- it is there to judge a run by, and for a scoring report later.
        expected_relevant INTEGER NOT NULL DEFAULT 1,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dev_emails_integration ON dev_emails(integration_id)",
    """
    CREATE TABLE IF NOT EXISTS dev_events (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        integration_id    INTEGER NOT NULL REFERENCES integrations(id) ON DELETE CASCADE,
        summary           TEXT NOT NULL,
        description       TEXT,
        location          TEXT,
        attendees_json    TEXT,
        calendar_name     TEXT,
        event_type        TEXT,
        duration_minutes  INTEGER NOT NULL DEFAULT 60,
        date_mode         TEXT NOT NULL DEFAULT 'relative',
        at                TEXT,
        offset_minutes    INTEGER,
        anchor_meeting_id INTEGER REFERENCES meetings(id) ON DELETE SET NULL,
        -- A bare YYYY-MM-DD with no time, which is what a real all-day event
        -- carries and what puts it on the wrong day west of Greenwich if
        -- anything coerces it to midnight.
        all_day           INTEGER NOT NULL DEFAULT 0,
        -- Expand into N weekly instances sharing one source_uid. The cheap
        -- stand-in for icloud_recurring.ics: it is what exercises dedupe_events.
        repeat_weekly     INTEGER NOT NULL DEFAULT 1,
        expected_relevant INTEGER NOT NULL DEFAULT 1,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dev_events_integration ON dev_events(integration_id)",
)

# Columns added after the initial release go here as (table, column, ddl_fragment).
# Applied best-effort; "duplicate column name" is the expected no-op outcome.
# Note SQLite's ALTER TABLE ADD COLUMN cannot add NOT NULL without a default, and
# cannot add UNIQUE -- anything needing those has to go in SCHEMA instead.
LATE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # A provider-owned deep link to the message. Gmail and Zoho have one; plain
    # IMAP does not. Without this the SPA falls back to a Gmail-only URL builder,
    # which would point iCloud and Zoho mail at a Gmail search that finds nothing.
    ("thread_emails", "url", "TEXT"),
    # The real RFC 2822 Message-ID. Distinct from `message_id`, which is now an
    # app-owned composite; this is what cross-provider dedup and the Gmail
    # rfc822msgid fallback link need.
    ("thread_emails", "rfc_message_id", "TEXT"),
    # Which integration surfaced the row, for display and provenance.
    ("thread_emails", "provider", "TEXT"),
    ("thread_calendar_events", "provider", "TEXT"),
    # The provider's own recurrence-set identity (Google iCalUID, CalDAV UID).
    # `uid` is app-owned and instance-scoped, so identifying "the same real
    # event seen through two providers" needs this stored separately.
    ("thread_calendar_events", "source_uid", "TEXT"),
    # Per-source failures, one entry per integration that errored. The older
    # calendar_error/email_error columns remain as derived aggregates.
    ("match_runs", "source_errors_json", "TEXT"),
    # Attached by the periodic sweep rather than by a person, and not yet opened.
    # Only an auto-attached row can be unread: anything a user ticked themselves
    # they have by definition already seen, so seen_at is stamped on write there.
    # The pair is what the blue dot on a thread and the bold row inside it read.
    ("thread_emails", "auto_attached", "INTEGER NOT NULL DEFAULT 0"),
    ("thread_emails", "seen_at", "TEXT"),
    ("thread_calendar_events", "auto_attached", "INTEGER NOT NULL DEFAULT 0"),
    ("thread_calendar_events", "seen_at", "TEXT"),
    # When the sweep last looked at this thread, and why it last failed. Kept on
    # the thread rather than in match_runs because that table is keyed to a
    # meeting (NOT NULL) and a sweep belongs to the whole thread.
    ("threads", "auto_match_at", "TEXT"),
    ("threads", "auto_match_error", "TEXT"),
    # Cached "what's next" suggestion, one LLM call. next_step_fingerprint is
    # what threads_svc.compute_next_step_fingerprint returned when it was
    # generated -- a mismatch on read is what "stale" means, no invalidation
    # call needed at every attach/create site.
    ("threads", "next_step", "TEXT"),
    ("threads", "next_step_generated_at", "TEXT"),
    ("threads", "next_step_fingerprint", "TEXT"),
    ("threads", "next_step_model", "TEXT"),
    # Stamped on every generation attempt, success or failure -- unlike
    # next_step_generated_at, which only moves on success. This is what lets
    # the thread list back off after a failed attempt instead of retrying the
    # same broken LLM call on every poll, the same request-storm risk the
    # sweep's auto_match_at guards against.
    ("threads", "next_step_checked_at", "TEXT"),
    # Which group the thread sits in on the home screen. NULL is "Ungrouped",
    # which is why this is nullable rather than pointing at a real default row:
    # every thread that predates groups is already in the right place.
    # ON DELETE SET NULL because deleting a folder must not delete the work --
    # its threads fall back to Ungrouped.
    ("threads", "group_id", "INTEGER REFERENCES thread_groups(id) ON DELETE SET NULL"),
    # Zoho's message-content endpoint needs a folder id, not just a message id.
    # Snapshotted at attach time so a later "fetch the full body" tool call can
    # reach the right endpoint without re-searching to relocate it.
    ("thread_emails", "folder_id", "TEXT"),
    # Display-only: hides a speaker's lines from the transcript view. Never
    # read by the summarizer or chat -- hiding a speaker to declutter your own
    # screen must not silently degrade a summary.
    ("speaker_map", "hidden", "INTEGER NOT NULL DEFAULT 0"),
    # At most one speaker per meeting. Enforced in the router (clearing every
    # other row on write), not a DB constraint, same as other single-writer
    # SQLite invariants in this codebase.
    ("speaker_map", "is_me", "INTEGER NOT NULL DEFAULT 0"),
    # Points at another speaker_id in the same meeting when the diarizer split
    # one person into two ids. Always one hop -- the router reparents any
    # existing followers when a merge target itself gets merged, so
    # transcript.py never has to walk a chain.
    ("speaker_map", "merged_into", "TEXT"),
)

# Indexes over columns that LATE_COLUMNS adds. They cannot live in SCHEMA: that
# runs first, so naming group_id there would fail on the boot that adds it.
LATE_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_threads_group ON threads(owner_id, group_id)",
)


def init_db(db_path: Path | str | None = None) -> None:
    """Create every table and index. Safe to run repeatedly."""
    with get_conn(db_path) as conn:
        for statement in SCHEMA:
            conn.execute(statement)

        for table, column, ddl in LATE_COLUMNS:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise

        for statement in LATE_INDEXES:
            conn.execute(statement)
