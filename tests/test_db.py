"""Schema creation, pragmas, and cascade behaviour."""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.db import get_conn, init_db, utcnow

EXPECTED_TABLES = {
    "users",
    "sessions",
    "thread_groups",
    "threads",
    "meetings",
    "diarizations",
    "speaker_map",
    "summaries",
    "action_items",
    "jobs",
    "job_events",
    "thread_emails",
    "thread_calendar_events",
    "match_runs",
    "mcp_servers",
    "user_mcp_profiles",
    "integrations",
    "app_settings",
    "dev_emails",
    "dev_events",
}

EXPECTED_INDEXES = {
    "idx_sessions_user",
    "idx_sessions_exp",
    "idx_threads_owner_updated",
    "idx_threads_title",
    "uq_thread_group_name",
    "idx_threads_group",
    "idx_meetings_thread",
    "idx_meetings_owner",
    "idx_diar_meeting",
    "uq_speaker_map",
    "uq_summary_version",
    "idx_summary_current",
    "idx_ai_meeting",
    "idx_ai_summary",
    "idx_jobs_status",
    "idx_jobs_meeting",
    "idx_jobs_user",
    "idx_job_events",
    "uq_thread_email",
    "uq_thread_event",
    "idx_tce_timeline",
    "idx_match_meeting",
    "uq_integration_account",
    "idx_integrations_user",
    "idx_dev_emails_integration",
    "idx_dev_events_integration",
}


def _tables(conn) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r["name"] for r in rows}


def _indexes(conn) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    return {r["name"] for r in rows}


def test_init_db_creates_every_table(conn):
    assert EXPECTED_TABLES <= _tables(conn)


def test_init_db_creates_every_index(conn):
    assert EXPECTED_INDEXES <= _indexes(conn)


def test_init_db_is_idempotent(db_path):
    init_db(db_path)
    init_db(db_path)
    init_db(db_path)
    with get_conn(db_path) as conn:
        assert EXPECTED_TABLES <= _tables(conn)


def test_pragmas_are_applied(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


# --------------------------------------------------------------------------- #
# Late columns and integrations
# --------------------------------------------------------------------------- #


def _columns(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


@pytest.mark.parametrize(
    "table,column",
    [
        ("thread_emails", "url"),
        ("thread_emails", "rfc_message_id"),
        ("thread_emails", "provider"),
        ("thread_calendar_events", "provider"),
        ("thread_calendar_events", "source_uid"),
        ("match_runs", "source_errors_json"),
        ("threads", "group_id"),
        ("thread_emails", "folder_id"),
    ],
)
def test_late_columns_are_applied(conn, table, column):
    """LATE_COLUMNS was an empty tuple until this refactor, so the ALTER TABLE
    loop in init_db had never actually run. Assert it does."""
    assert column in _columns(conn, table)


def test_late_columns_survive_a_second_init(db_path):
    """The second run hits "duplicate column name", which must be swallowed."""
    init_db(db_path)
    init_db(db_path)
    with get_conn(db_path) as conn:
        assert "url" in _columns(conn, "thread_emails")


def test_late_indexes_are_created(conn):
    """An index over a LATE_COLUMN cannot live in SCHEMA -- that runs before the
    ALTER TABLE that adds the column, so it would fail on the boot that adds it."""
    assert "idx_threads_group" in _indexes(conn)


class TestInsightTypePromptUpgrade:
    """init_db's seed block only ever runs once (see its own comment), so an
    already-initialized DB needs a separate path to pick up a stock prompt
    edit -- without it, every DB created before this file's ai_answer_points
    wording change would keep the old wording forever. Covers both hops: the
    original single-shape prompt, and the intermediate combined-shape one
    this repo's own dev DB could be sitting on."""

    def _prompt(self, db_path, slug: str) -> str:
        with get_conn(db_path) as conn:
            return conn.execute(
                "SELECT prompt FROM insight_types WHERE slug = ?", (slug,)
            ).fetchone()[0]

    def _set_prompt(self, db_path, slug: str, prompt: str) -> None:
        with get_conn(db_path) as conn:
            conn.execute("UPDATE insight_types SET prompt = ? WHERE slug = ?", (prompt, slug))

    def test_upgrades_the_original_legacy_prompt(self, initialised_db):
        from app.db import _DEFAULT_GENERAL_PROMPT, _LEGACY_DEFAULT_GENERAL_PROMPT

        self._set_prompt(initialised_db, "general", _LEGACY_DEFAULT_GENERAL_PROMPT)
        init_db(initialised_db)
        assert self._prompt(initialised_db, "general") == _DEFAULT_GENERAL_PROMPT

    def test_upgrades_the_intermediate_combined_shape_prompt(self, initialised_db):
        from app.db import _COMBINED_V1_DEFAULT_INTERVIEW_PROMPT, _DEFAULT_INTERVIEW_PROMPT

        self._set_prompt(initialised_db, "interview", _COMBINED_V1_DEFAULT_INTERVIEW_PROMPT)
        init_db(initialised_db)
        assert self._prompt(initialised_db, "interview") == _DEFAULT_INTERVIEW_PROMPT

    def test_upgrades_the_pre_talking_points_combined_shape_prompt(self, initialised_db):
        from app.db import _COMBINED_V2_DEFAULT_GENERAL_PROMPT, _DEFAULT_GENERAL_PROMPT

        self._set_prompt(initialised_db, "general", _COMBINED_V2_DEFAULT_GENERAL_PROMPT)
        init_db(initialised_db)
        assert self._prompt(initialised_db, "general") == _DEFAULT_GENERAL_PROMPT

    def test_upgrades_the_pre_detailed_answer_combined_shape_prompt(self, initialised_db):
        from app.db import _COMBINED_V3_DEFAULT_INTERVIEW_PROMPT, _DEFAULT_INTERVIEW_PROMPT

        self._set_prompt(initialised_db, "interview", _COMBINED_V3_DEFAULT_INTERVIEW_PROMPT)
        init_db(initialised_db)
        assert self._prompt(initialised_db, "interview") == _DEFAULT_INTERVIEW_PROMPT

    def test_leaves_a_customized_prompt_alone(self, initialised_db):
        custom = "{{transcript}} {{previous_topics}} {{previous_questions}} {{previous_action_items}}"
        self._set_prompt(initialised_db, "general", custom)
        init_db(initialised_db)
        assert self._prompt(initialised_db, "general") == custom


def _make_user(conn, user_id: int, username: str) -> None:
    now = utcnow()
    conn.execute(
        "INSERT INTO users (id, username, password_hash, password_salt, created_at, updated_at) "
        "VALUES (?, ?, 'h', 's', ?, ?)",
        (user_id, username, now, now),
    )


def _add_integration(conn, user_id: int, provider: str, account_key: str) -> None:
    now = utcnow()
    conn.execute(
        "INSERT INTO integrations (user_id, provider, account_key, auth_type, "
        "created_at, updated_at) VALUES (?, ?, ?, 'token', ?, ?)",
        (user_id, provider, account_key, now, now),
    )


class TestIntegrationsTable:
    def test_the_same_account_cannot_be_connected_twice(self, conn):
        """Guards the "four Connect clicks make four rows" failure."""
        _make_user(conn, 1, "one")
        _add_integration(conn, 1, "google", "sub-123")
        with pytest.raises(sqlite3.IntegrityError):
            _add_integration(conn, 1, "google", "sub-123")

    def test_two_users_may_connect_the_same_account(self, conn):
        """Integrations are per-user; a shared family mailbox is legitimate."""
        _make_user(conn, 1, "one")
        _make_user(conn, 2, "two")
        _add_integration(conn, 1, "google", "sub-123")
        _add_integration(conn, 2, "google", "sub-123")
        assert conn.execute("SELECT COUNT(*) FROM integrations").fetchone()[0] == 2

    def test_one_user_may_connect_two_accounts_of_one_provider(self, conn):
        _make_user(conn, 1, "one")
        _add_integration(conn, 1, "google", "work-sub")
        _add_integration(conn, 1, "google", "personal-sub")
        assert conn.execute("SELECT COUNT(*) FROM integrations").fetchone()[0] == 2

    def test_deleting_a_user_removes_their_integrations(self, conn):
        _make_user(conn, 1, "one")
        _add_integration(conn, 1, "google", "sub-123")
        conn.execute("DELETE FROM users WHERE id = 1")
        assert conn.execute("SELECT COUNT(*) FROM integrations").fetchone()[0] == 0

    def test_capabilities_default_to_off(self, conn):
        """A freshly created row must not silently start searching anything."""
        _make_user(conn, 1, "one")
        _add_integration(conn, 1, "google", "sub-123")
        row = conn.execute(
            "SELECT calendar_enabled, email_enabled, status FROM integrations"
        ).fetchone()
        assert (row["calendar_enabled"], row["email_enabled"]) == (0, 0)
        assert row["status"] == "unverified"


class TestThreadGroups:
    """The FK on threads.group_id, which only behaves because foreign_keys=ON."""

    def _seed(self, conn):
        now = utcnow()
        _make_user(conn, 1, "one")
        conn.execute(
            "INSERT INTO thread_groups (id, owner_id, name, created_at, updated_at) "
            "VALUES (1, 1, 'Clients', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO threads (id, owner_id, group_id, title, created_at, updated_at) "
            "VALUES (1, 1, 1, 'T', ?, ?)",
            (now, now),
        )

    def test_deleting_a_group_releases_its_threads(self, conn):
        """SET NULL, not CASCADE: deleting a folder must not delete the work."""
        self._seed(conn)
        conn.execute("DELETE FROM thread_groups WHERE id = 1")
        row = conn.execute("SELECT group_id FROM threads WHERE id = 1").fetchone()
        assert row is not None and row["group_id"] is None

    def test_a_group_name_is_unique_per_owner(self, conn):
        self._seed(conn)
        now = utcnow()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO thread_groups (owner_id, name, created_at, updated_at) "
                "VALUES (1, 'clients', ?, ?)",
                (now, now),
            )

    def test_deleting_a_user_removes_their_groups(self, conn):
        self._seed(conn)
        conn.execute("DELETE FROM threads WHERE id = 1")
        conn.execute("DELETE FROM users WHERE id = 1")
        assert conn.execute("SELECT COUNT(*) FROM thread_groups").fetchone()[0] == 0


class TestDevData:
    """The Development provider's authored inbox and calendar."""

    def _seed(self, conn):
        now = utcnow()
        _make_user(conn, 1, "one")
        conn.execute(
            "INSERT INTO integrations (id, user_id, provider, account_key, auth_type, "
            "created_at, updated_at) VALUES (5, 1, 'dev', 'default', 'none', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO dev_emails (integration_id, subject, created_at, updated_at) "
            "VALUES (5, 'Hi', ?, ?)",
            (now, now),
        )

    def test_items_go_with_the_account(self, conn):
        """An inbox belonging to no account means nothing -- hence CASCADE, and
        hence the JSON export in dev_data for keeping fixtures across one."""
        self._seed(conn)
        conn.execute("DELETE FROM integrations WHERE id = 5")
        assert conn.execute("SELECT COUNT(*) FROM dev_emails").fetchone()[0] == 0

    def test_an_anchor_survives_its_meeting(self, conn):
        """SET NULL, so the item stays findable; dev.resolve_when then falls back
        to treating the offset as relative to now."""
        now = utcnow()
        self._seed(conn)
        conn.execute(
            "INSERT INTO threads (id, owner_id, title, created_at, updated_at) "
            "VALUES (1, 1, 'T', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO meetings (id, thread_id, owner_id, title, created_at, updated_at) "
            "VALUES (1, 1, 1, 'M', ?, ?)",
            (now, now),
        )
        conn.execute("UPDATE dev_emails SET anchor_meeting_id = 1")
        conn.execute("DELETE FROM meetings WHERE id = 1")

        row = conn.execute("SELECT * FROM dev_emails").fetchone()
        assert row is not None and row["anchor_meeting_id"] is None


# --------------------------------------------------------------------------- #
# Cascade: deleting a thread must not leave orphans in any child table.
# --------------------------------------------------------------------------- #


@pytest.fixture
def populated(conn):
    now = utcnow()
    conn.execute(
        "INSERT INTO users (id, username, password_hash, password_salt, created_at, updated_at) "
        "VALUES (1, 'u', 'h', 's', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO threads (id, owner_id, title, created_at, updated_at) "
        "VALUES (1, 1, 'T', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO meetings (id, thread_id, owner_id, title, created_at, updated_at) "
        "VALUES (1, 1, 1, 'M', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO diarizations (id, meeting_id, provider_url, model, raw_json, created_at) "
        "VALUES (1, 1, 'http://x', 'm', '{}', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO speaker_map (meeting_id, speaker_id, display_name) VALUES (1, 'SPEAKER_00', 'A')"
    )
    conn.execute(
        "INSERT INTO summaries (id, meeting_id, version, model, prompt_name, prompt_sha256, "
        "prompt_text, created_at) VALUES (1, 1, 1, 'm', 'p', 'sha', 'text', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO action_items (summary_id, meeting_id, idx, text, created_at) "
        "VALUES (1, 1, 0, 'do it', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO jobs (id, type, status, user_id, meeting_id, thread_id, created_at) "
        "VALUES ('j1', 'ingest', 'queued', 1, 1, 1, ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO job_events (job_id, ts, message) VALUES ('j1', ?, 'started')", (now,)
    )
    conn.execute(
        "INSERT INTO thread_emails (thread_id, message_id, raw_json, attached_at) "
        "VALUES (1, '<m@x>', '{}', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO thread_calendar_events (thread_id, uid, raw_json, attached_at) "
        "VALUES (1, 'uid-1', '{}', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO match_runs (meeting_id, thread_id, user_id, status, created_at) "
        "VALUES (1, 1, 1, 'ok', ?)",
        (now,),
    )
    return conn


CHILD_TABLES = [
    "meetings",
    "diarizations",
    "speaker_map",
    "summaries",
    "action_items",
    "job_events",
    "thread_emails",
    "thread_calendar_events",
    "match_runs",
]


def test_deleting_a_thread_cascades_to_every_child(populated):
    conn = populated
    for table in CHILD_TABLES:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0, table

    conn.execute("DELETE FROM threads WHERE id = 1")

    for table in CHILD_TABLES:
        remaining = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert remaining == 0, f"{table} still has {remaining} orphaned row(s)"


def test_deleting_a_summary_removes_its_action_items_only(populated):
    conn = populated
    conn.execute("DELETE FROM summaries WHERE id = 1")
    assert conn.execute("SELECT COUNT(*) FROM action_items").fetchone()[0] == 0
    # the meeting itself survives so v1's absence doesn't take the recording with it
    assert conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0] == 1


def test_unique_constraints(populated):
    conn = populated
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO speaker_map (meeting_id, speaker_id) VALUES (1, 'SPEAKER_00')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO thread_calendar_events (thread_id, uid, raw_json, attached_at) "
            "VALUES (1, 'uid-1', '{}', ?)",
            (utcnow(),),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO summaries (meeting_id, version, model, prompt_name, prompt_sha256, "
            "prompt_text, created_at) VALUES (1, 1, 'm', 'p', 'sha', 't', ?)",
            (utcnow(),),
        )


def test_username_is_case_insensitively_unique(conn):
    import sqlite3

    now = utcnow()
    conn.execute(
        "INSERT INTO users (username, password_hash, password_salt, created_at, updated_at) "
        "VALUES ('Admin', 'h', 's', ?, ?)",
        (now, now),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO users (username, password_hash, password_salt, created_at, updated_at) "
            "VALUES ('admin', 'h', 's', ?, ?)",
            (now, now),
        )


# --------------------------------------------------------------------------- #
# MCP server seeding
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# LATE_COLUMNS
# --------------------------------------------------------------------------- #

EMAIL_THREADING_COLUMNS = {
    "conversation_id",
    "in_reply_to",
    "references_json",
    "to_recipients",
    "cc_recipients",
    "direction",
    "body",
    "body_fetched_at",
    "ai_summary",
    "ai_summary_model",
    "integration_id",
}


def test_email_threading_columns_exist(conn):
    present = {row[1] for row in conn.execute("PRAGMA table_info(thread_emails)")}
    assert EMAIL_THREADING_COLUMNS <= present


def test_dev_email_authoring_columns_exist(conn):
    present = {row[1] for row in conn.execute("PRAGMA table_info(dev_emails)")}
    assert {"in_reply_to", "conversation_id", "to_recipients", "outbound"} <= present


def test_init_db_is_idempotent_for_late_columns(initialised_db):
    """A second boot must not raise -- "duplicate column name" is the no-op."""
    init_db(initialised_db)
    init_db(initialised_db)

    with get_conn(initialised_db) as conn:
        present = {row[1] for row in conn.execute("PRAGMA table_info(thread_emails)")}
    assert EMAIL_THREADING_COLUMNS <= present


def test_no_late_column_is_not_null_without_a_default():
    """A structural guard on the rule documented above LATE_COLUMNS.

    SQLite's ALTER TABLE ADD COLUMN cannot add NOT NULL without a default, and
    cannot add UNIQUE at all -- anything needing either has to go in SCHEMA
    instead. Asserted here rather than left to a failing boot, because the boot
    that fails is the one upgrading an existing database, not a fresh test run.
    """
    from app.db import LATE_COLUMNS

    offenders = [
        f"{table}.{column}"
        for table, column, ddl in LATE_COLUMNS
        if ("NOT NULL" in ddl.upper() and "DEFAULT" not in ddl.upper())
        or "UNIQUE" in ddl.upper()
    ]
    assert offenders == []


def test_utcnow_is_iso8601_utc():
    from datetime import datetime

    parsed = datetime.fromisoformat(utcnow())
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0
