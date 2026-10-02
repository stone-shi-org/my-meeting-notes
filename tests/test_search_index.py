"""MMN-15: the keyword index stays in step with every kind of document.

Service-level: rows are written through the same service functions the
routers call (threads/notes/matching), then ``index_all_now`` plays the part
of the background indexer -- which the suite switches off -- so each test
says exactly when indexing happens.
"""

from __future__ import annotations

import json

import pytest

from app.services import notes as notes_svc
from app.services import search as search_svc
from app.services import search_index
from app.services import threads as threads_svc
from tests.search_support import (
    add_email,
    add_event,
    add_summary,
    add_transcript,
    make_user,
)


@pytest.fixture
def owner(conn):
    return make_user(conn, "alice")


@pytest.fixture
def thread(conn, owner):
    return threads_svc.create_thread(
        conn, owner_id=owner, title="Atlas migration", description="Move off Oracle"
    )["id"]


@pytest.fixture
def meeting(conn, owner, thread):
    return threads_svc.create_meeting(
        conn, thread_id=thread, owner_id=owner, title="Kickoff", meeting_at="2026-09-01T10:00:00+00:00"
    )["id"]


def find(conn, owner, q, **kw):
    return search_svc.search(conn, owner_id=owner, q=q, mode="keyword", **kw)


def kinds(result):
    return [h["kind"] for h in result["hits"]]


# --------------------------------------------------------------------------- #
# Create / update / delete, per kind
# --------------------------------------------------------------------------- #


def test_thread_doc_is_indexed_synchronously_on_create(conn, owner, thread):
    # No index_all_now: the home filter must find a new thread immediately.
    assert kinds(find(conn, owner, "oracle")) == ["thread"]


def test_thread_rename_reindexes_and_drops_the_old_title(conn, owner, thread):
    conn.execute("UPDATE threads SET title = 'Zephyr rollout' WHERE id = ?", (thread,))
    search_index.index_thread_doc(conn, thread)
    assert find(conn, owner, "atlas")["hits"] == []
    assert kinds(find(conn, owner, "zephyr")) == ["thread"]


def test_meeting_title(conn, owner, meeting):
    search_index.index_all_now(conn)
    hits = find(conn, owner, "kickoff")["hits"]
    assert [(h["kind"], h["meeting_id"]) for h in hits] == [("meeting", meeting)]
    assert hits[0]["url"] == f"/meetings/{meeting}"


def test_transcript_segments_carry_meeting_and_start(conn, owner, meeting):
    add_transcript(
        conn, meeting,
        ("SPEAKER_00", "Welcome everyone."),
        ("SPEAKER_01", "The Oracle licence renews in March."),
    )
    search_index.index_all_now(conn)
    hit = find(conn, owner, "licence")["hits"][0]
    assert hit["kind"] == "segment"
    assert hit["meeting_id"] == meeting
    assert hit["start_sec"] == 10.0
    assert hit["id"] == 1
    assert hit["url"] == f"/meetings/{meeting}?t=10"
    assert "\x02licence\x03" in hit["snippet"]


def test_non_speech_markers_are_not_indexed(conn, owner, meeting):
    add_transcript(conn, meeting, ("SPEAKER_00", "[Music]"), ("SPEAKER_00", "Hello there"))
    search_index.index_all_now(conn)
    assert find(conn, owner, "music")["hits"] == []


def test_summary_and_action_items_and_resummarise_replaces_them(conn, owner, meeting):
    add_summary(
        conn, meeting, tldr="Cutover planned for Q4.", decisions=["Freeze schema in October"],
        topics=["Data migration"], questions=["Who signs off rollback?"],
        actions=["Draft the rollback runbook"],
    )
    search_index.index_all_now(conn)
    assert kinds(find(conn, owner, "cutover")) == ["summary"]
    assert kinds(find(conn, owner, "freeze schema")) == ["summary"]
    assert kinds(find(conn, owner, "rollback signs")) == ["summary"]
    assert kinds(find(conn, owner, "runbook")) == ["action_item"]

    add_summary(conn, meeting, tldr="Cutover moved to Q1.", actions=["Book the war room"])
    search_index.index_all_now(conn)
    assert find(conn, owner, "runbook")["hits"] == []
    assert find(conn, owner, "freeze")["hits"] == []
    assert kinds(find(conn, owner, "war room")) == ["action_item"]
    assert len(find(conn, owner, "cutover")["hits"]) == 1


def test_note_create_update_delete(conn, owner, thread):
    note = notes_svc.create_note(
        conn, thread_id=thread, meeting_id=None, title="Vendor shortlist",
        body="Postgres consultancy quotes", source="manual", user_id=owner,
    )
    search_index.index_all_now(conn)
    assert kinds(find(conn, owner, "consultancy")) == ["note"]

    notes_svc.update_note(conn, thread_id=thread, note_id=note["id"], body="Snowflake quotes")
    search_index.index_all_now(conn)
    assert find(conn, owner, "consultancy")["hits"] == []
    assert kinds(find(conn, owner, "snowflake")) == ["note"]

    notes_svc.delete_note(conn, thread_id=thread, note_id=note["id"])
    # Deletes are synchronous -- no indexer run needed.
    assert find(conn, owner, "snowflake")["hits"] == []


def test_email_subject_snippet_summary_and_stored_body(conn, owner, thread):
    email_id = add_email(
        conn, thread, owner, message_id="<m1@x>", subject="Licence renewal",
        snippet="see attached quote", body="The renewal is <b>expensive</b>.",
        ai_summary="Priya wants a decision by Friday.",
    )
    search_index.index_all_now(conn)
    for q in ("licence", "attached quote", "friday", "expensive"):
        assert kinds(find(conn, owner, q)) == ["email"], q
    # The literal tag never reaches the index as a word.
    assert find(conn, owner, "b")["hits"] == []

    conn.execute("DELETE FROM thread_emails WHERE id = ?", (email_id,))
    search_index.delete_doc(conn, "email", email_id)
    assert find(conn, owner, "licence")["hits"] == []


def test_calendar_event(conn, owner, thread):
    add_event(conn, thread, owner, uid="ev1", summary="Steering committee",
              description="Quarterly review", location="Room Bellatrix")
    search_index.index_all_now(conn)
    for q in ("steering", "quarterly", "bellatrix"):
        assert kinds(find(conn, owner, q)) == ["event"], q


def test_hydrated_body_is_indexed_after_it_is_stored(conn, owner, thread):
    from app.services import email_bodies

    email_id = add_email(conn, thread, owner, message_id="<m2@x>", subject="Hello")
    search_index.index_all_now(conn)
    assert find(conn, owner, "kangaroo")["hits"] == []

    conn.commit()
    email_bodies._store(conn_path(conn), email_id, body="A kangaroo appeared.", fill_snippet=False)
    search_index.index_all_now(conn)
    assert kinds(find(conn, owner, "kangaroo")) == ["email"]


def conn_path(conn):
    return conn.execute("PRAGMA database_list").fetchone()["file"]


# --------------------------------------------------------------------------- #
# Placement and cascades
# --------------------------------------------------------------------------- #


def test_deleting_a_thread_removes_everything_on_it(conn, owner, thread, meeting):
    add_transcript(conn, meeting, ("SPEAKER_00", "Zebra crossing"))
    notes_svc.create_note(conn, thread_id=thread, meeting_id=None, title="t", body="Zebra note",
                          source="manual", user_id=owner)
    search_index.index_all_now(conn)
    assert len(find(conn, owner, "zebra")["hits"]) == 2

    search_index.delete_thread_scope(conn, thread)
    conn.execute("DELETE FROM threads WHERE id = ?", (thread,))
    assert find(conn, owner, "zebra")["hits"] == []
    assert conn.execute("SELECT COUNT(*) FROM search_docs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM search_fts").fetchone()[0] == 0


def test_deleting_a_meeting_drops_its_docs_and_keeps_its_thread(conn, owner, thread, meeting):
    add_transcript(conn, meeting, ("SPEAKER_00", "Zebra crossing"))
    search_index.index_all_now(conn)
    search_index.delete_meeting_scope(conn, meeting)
    conn.execute("DELETE FROM meetings WHERE id = ?", (meeting,))
    assert find(conn, owner, "zebra")["hits"] == []
    assert kinds(find(conn, owner, "atlas")) == ["thread"]


def test_moving_a_meeting_rewrites_thread_id(conn, owner, thread, meeting):
    other = threads_svc.create_thread(conn, owner_id=owner, title="Second")["id"]
    add_transcript(conn, meeting, ("SPEAKER_00", "Zebra crossing"))
    search_index.index_all_now(conn)
    threads_svc.move_meeting(conn, meeting_id=meeting, thread_id=thread, target_thread_id=other)
    search_index.index_all_now(conn)
    hits = find(conn, owner, "zebra")["hits"]
    assert [h["thread_id"] for h in hits] == [other]
    assert hits[0]["thread_title"] == "Second"


def test_moving_a_note_rewrites_thread_id(conn, owner, thread):
    other = threads_svc.create_thread(conn, owner_id=owner, title="Second")["id"]
    note = notes_svc.create_note(conn, thread_id=thread, meeting_id=None, title="t",
                                 body="Quokka", source="manual", user_id=owner)
    search_index.index_all_now(conn)
    notes_svc.move_note(conn, thread_id=thread, note_id=note["id"], target_thread_id=other)
    search_index.index_all_now(conn)
    assert [h["thread_id"] for h in find(conn, owner, "quokka")["hits"]] == [other]


# --------------------------------------------------------------------------- #
# Speakers: rename re-indexes, raw_json never changes
# --------------------------------------------------------------------------- #


def test_speaker_rename_reindexes_segments_and_leaves_raw_json_byte_identical(
    user_client, isolated_settings
):
    from app.db import get_conn

    t = user_client.post("/api/threads", json={"title": "Sync"}).json()
    m = user_client.post("/api/meetings", json={"thread_id": t["id"], "title": "Weekly"}).json()
    with get_conn(isolated_settings.db_path) as c:
        add_transcript(c, m["id"], ("SPEAKER_00", "Budget is tight"), ("SPEAKER_01", "Agreed"))
        before = c.execute("SELECT raw_json FROM diarizations").fetchone()[0]
        search_index.index_all_now(c)

    resp = user_client.put(
        f"/api/meetings/{m['id']}/speakers",
        json=[{"speaker_id": "SPEAKER_00", "display_name": "Priya Raman"}],
    )
    assert resp.status_code == 200, resp.text

    with get_conn(isolated_settings.db_path) as c:
        assert c.execute(
            "SELECT 1 FROM search_dirty WHERE scope_key = ?", (f"m:{m['id']}",)
        ).fetchone(), "the speaker route must mark the meeting dirty"
        search_index.index_all_now(c)
        after = c.execute("SELECT raw_json FROM diarizations").fetchone()[0]
        owner = t["owner_id"]
        hit = search_svc.search(c, owner_id=owner, q="priya", mode="keyword")["hits"]
        assert [(h["kind"], h["title"]) for h in hit] == [("segment", "Priya Raman @ 0:00")]
        assert search_svc.search(c, owner_id=owner, q="SPEAKER_00", mode="keyword")["hits"] == []
    assert before == after


def test_merged_speaker_renders_as_the_merge_target(conn, owner, meeting):
    add_transcript(conn, meeting, ("SPEAKER_00", "First"), ("SPEAKER_01", "Wombat facts"))
    conn.execute(
        "UPDATE speaker_map SET display_name = 'Dana' WHERE meeting_id = ? AND speaker_id = 'SPEAKER_00'",
        (meeting,),
    )
    conn.execute(
        "UPDATE speaker_map SET merged_into = 'SPEAKER_00', updated_at = 'later' "
        "WHERE meeting_id = ? AND speaker_id = 'SPEAKER_01'",
        (meeting,),
    )
    search_index.index_all_now(conn)
    assert find(conn, owner, "wombat")["hits"][0]["title"].startswith("Dana @")


# --------------------------------------------------------------------------- #
# Reconcile + rebuild
# --------------------------------------------------------------------------- #


def test_reconcile_catches_a_write_nobody_marked(conn, owner, thread, meeting):
    search_index.index_all_now(conn)
    # A write path that "forgot" to mark its scope.
    conn.execute("UPDATE meetings SET title = 'Retrospective' WHERE id = ?", (meeting,))
    assert find(conn, owner, "retrospective")["hits"] == []
    search_index.index_all_now(conn)  # reconcile first, then drain
    assert kinds(find(conn, owner, "retrospective")) == ["meeting"]


def test_reconcile_repairs_a_corrupted_index_and_drops_orphans(conn, owner, thread, meeting):
    search_index.index_all_now(conn)
    doc_id = conn.execute("SELECT id FROM search_docs WHERE kind = 'meeting'").fetchone()[0]
    conn.execute("DELETE FROM search_fts WHERE rowid = ?", (doc_id,))
    conn.execute(
        "INSERT INTO search_docs (kind, ref_id, scope_key, owner_id, fingerprint, indexed_at) "
        "VALUES ('note', '999', 't:999', ?, 'x', 'now')",
        (owner,),
    )
    assert find(conn, owner, "kickoff")["hits"] == []

    search_index.index_all_now(conn)
    assert kinds(find(conn, owner, "kickoff")) == ["meeting"]
    assert conn.execute("SELECT COUNT(*) FROM search_docs WHERE ref_id = '999'").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM search_docs d WHERE NOT EXISTS "
        "(SELECT 1 FROM search_fts f WHERE f.rowid = d.id)"
    ).fetchone()[0] == 0


def test_reconcile_is_idempotent(conn, owner, thread, meeting):
    search_index.index_all_now(conn)
    snapshot = conn.execute(
        "SELECT kind, ref_id, fingerprint, indexed_at FROM search_docs ORDER BY id"
    ).fetchall()
    assert search_index.reconcile(conn)["marked"] == 0
    search_index.index_all_now(conn)
    assert conn.execute(
        "SELECT kind, ref_id, fingerprint, indexed_at FROM search_docs ORDER BY id"
    ).fetchall() == snapshot


def test_rebuild_from_scratch(conn, owner, thread, meeting):
    add_transcript(conn, meeting, ("SPEAKER_00", "Platypus"))
    search_index.index_all_now(conn)
    before = conn.execute("SELECT COUNT(*) FROM search_docs").fetchone()[0]
    queued = search_index.rebuild(conn)
    assert queued == 2  # one thread scope, one meeting scope
    assert conn.execute("SELECT COUNT(*) FROM search_docs").fetchone()[0] == 0
    search_index.index_all_now(conn)
    assert conn.execute("SELECT COUNT(*) FROM search_docs").fetchone()[0] == before
    assert kinds(find(conn, owner, "platypus")) == ["segment"]


# --------------------------------------------------------------------------- #
# Must-nots
# --------------------------------------------------------------------------- #


def test_indexing_is_not_activity(conn, owner, thread, meeting):
    eid = add_email(conn, thread, owner, message_id="<m3@x>", subject="s", body="b")
    conn.execute("UPDATE thread_emails SET seen_at = NULL, auto_attached = 1 WHERE id = ?", (eid,))
    before = conn.execute(
        "SELECT t.updated_at, m.updated_at, e.seen_at, e.body_fetched_at FROM threads t "
        "JOIN meetings m ON m.thread_id = t.id JOIN thread_emails e ON e.thread_id = t.id"
    ).fetchone()
    search_index.rebuild(conn)
    search_index.index_all_now(conn)
    after = conn.execute(
        "SELECT t.updated_at, m.updated_at, e.seen_at, e.body_fetched_at FROM threads t "
        "JOIN meetings m ON m.thread_id = t.id JOIN thread_emails e ON e.thread_id = t.id"
    ).fetchone()
    assert tuple(before) == tuple(after)


def test_indexing_never_hydrates(conn, owner, thread, monkeypatch):
    from app.services import email_bodies

    def boom(*a, **kw):  # pragma: no cover - the assertion is that it never runs
        raise AssertionError("indexing must not fetch email bodies")

    monkeypatch.setattr(email_bodies, "hydrate_thread_emails", boom)
    add_email(conn, thread, owner, message_id="<m4@x>", subject="Unfetched")
    search_index.index_all_now(conn)
    assert kinds(find(conn, owner, "unfetched")) == ["email"]
    assert conn.execute("SELECT body_fetched_at FROM thread_emails").fetchone()[0] is None


def test_attached_context_does_not_read_the_search_index(conn, owner, thread, meeting):
    from app.services import matching

    notes_svc.create_note(conn, thread_id=thread, meeting_id=meeting, title="n", body="Search me",
                          source="manual", user_id=owner)
    search_index.index_all_now(conn)
    ctx = matching.attached_context(conn, meeting)
    assert "Search me" not in json.dumps(ctx, default=str)
