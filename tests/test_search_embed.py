"""MMN-15: the semantic half -- chunking, backfill, model changes, fail-open.

All offline: respx stands in for the embeddings endpoint with a deterministic
"concept" model (tests/search_support.py), so a query can match a document it
shares no word with.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from app.db import get_conn
from app.services import notes as notes_svc
from app.services import search as search_svc
from app.services import search_embed, search_index
from app.services import threads as threads_svc
from app.services.search_index import Doc
from tests.search_support import (
    EMBED_URL,
    add_transcript,
    embed_side_effect,
    enable_embeddings,
    make_user,
)


@pytest.fixture
def seeded(initialised_db, monkeypatch):
    """One user, one thread with a budget note, a hiring note, a transcript."""
    enable_embeddings(monkeypatch)
    with get_conn(initialised_db) as conn:
        owner = make_user(conn, "alice")
        t = threads_svc.create_thread(conn, owner_id=owner, title="Planning")["id"]
        notes_svc.create_note(conn, thread_id=t, meeting_id=None, title="Finance",
                              body="The budget for next quarter is tight.", source="manual",
                              user_id=owner)
        notes_svc.create_note(conn, thread_id=t, meeting_id=None, title="People",
                              body="We need to hire two engineers.", source="manual", user_id=owner)
        m = threads_svc.create_meeting(conn, thread_id=t, owner_id=owner, title="Sync")["id"]
        add_transcript(conn, m, *[("SPEAKER_00", f"Line {i} about the flight to Tokyo") for i in range(12)])
        search_index.index_all_now(conn)
    return {"db": initialised_db, "owner": owner, "thread": t, "meeting": m}


def run_pass(db):
    total = 0
    while True:
        result = asyncio.run(search_embed.embed_pass(db))
        total += result["embedded"]
        if result["scopes"] == 0:
            return total


def query(db, owner, q, mode="hybrid", **kw):
    with get_conn(db) as conn:
        return search_svc.search(conn, owner_id=owner, q=q, mode=mode, **kw)


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #


def _seg(i, text="x" * 50, speaker="Ann"):
    return Doc("segment", f"7:{i}", 1, 1, 1, float(i * 10), speaker, text, "", None)


def test_transcript_windows_overlap_and_carry_first_start():
    chunks = search_embed.chunks_for_docs([_seg(i) for i in range(20)])
    assert all(c.kind == "segment" for c in chunks)
    assert chunks[0].start_sec == 0.0 and chunks[0].doc_ref == "7:0"
    first_len = int(chunks[0].ref_id.split(":")[-1])
    assert first_len == search_embed.WINDOW_MAX_SEGMENTS
    # Next window starts WINDOW_OVERLAP segments before the previous one ended.
    assert chunks[1].doc_ref == f"7:{first_len - search_embed.WINDOW_OVERLAP}"
    assert chunks[-1].text.endswith("x" * 50)


def test_transcript_window_respects_the_char_cap():
    chunks = search_embed.chunks_for_docs([_seg(i, "y" * 400) for i in range(6)])
    assert all(len(c.text) <= 2 * search_embed.WINDOW_MAX_CHARS for c in chunks)
    assert len(chunks) > 1


def test_paragraph_split_with_size_cap():
    body = "\n\n".join(f"Paragraph {i} " + "z" * 500 for i in range(5))
    doc = Doc("note", "3", 1, 1, None, None, "Title", body, "Title", None)
    chunks = search_embed.chunks_for_docs([doc])
    assert len(chunks) >= 3
    assert all(len(c.text) <= search_embed.PARAGRAPH_CHUNK_CHARS for c in chunks)
    assert [c.ref_id for c in chunks] == [f"3#p{i}" for i in range(len(chunks))]
    assert all(c.doc_ref == "3" for c in chunks)


def test_chunking_is_deterministic():
    docs = [_seg(i) for i in range(10)]
    assert search_embed.chunks_for_docs(docs) == search_embed.chunks_for_docs(docs)


# --------------------------------------------------------------------------- #
# Backfill
# --------------------------------------------------------------------------- #


@respx.mock
def test_backfill_embeds_then_skips_unchanged_text(seeded):
    calls: list[list[str]] = []
    respx.post(EMBED_URL).mock(side_effect=embed_side_effect(calls))

    first = run_pass(seeded["db"])
    assert first > 0
    sent = sum(len(c) for c in calls)
    assert sent == first

    with get_conn(seeded["db"]) as conn:
        rows = conn.execute("SELECT model, dim, owner_id FROM search_embeddings").fetchall()
        assert {r["model"] for r in rows} == {"fake-embed-1"}
        assert {r["owner_id"] for r in rows} == {seeded["owner"]}
        status = search_svc.status(conn, owner_id=seeded["owner"], is_admin=False)
        assert status["embedding"]["embedded"] == status["embedding"]["chunks"] == len(rows)
        assert status["embedding"]["pending_scopes"] == 0

    # Nothing changed: a second pass sends nothing at all.
    calls.clear()
    assert run_pass(seeded["db"]) == 0
    assert calls == []

    # A reindex with no text change is also free.
    with get_conn(seeded["db"]) as conn:
        search_index.rebuild(conn)
        search_index.index_all_now(conn)
    assert run_pass(seeded["db"]) == 0
    assert calls == []


@respx.mock
def test_editing_one_note_re_embeds_only_that_chunk(seeded):
    calls: list[list[str]] = []
    respx.post(EMBED_URL).mock(side_effect=embed_side_effect(calls))
    run_pass(seeded["db"])
    calls.clear()

    with get_conn(seeded["db"]) as conn:
        note_id = conn.execute("SELECT id FROM thread_notes WHERE title = 'People'").fetchone()[0]
        notes_svc.update_note(conn, thread_id=seeded["thread"], note_id=note_id,
                              body="We need to recruit a designer.")
        search_index.index_all_now(conn)

    assert run_pass(seeded["db"]) == 1
    assert calls == [["People\n\nWe need to recruit a designer."]]


@respx.mock
def test_backfill_resumes_after_an_endpoint_failure(seeded):
    route = respx.post(EMBED_URL)
    route.mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(Exception):
        asyncio.run(search_embed.embed_pass(seeded["db"]))
    assert search_embed.state()["last_error"]
    with get_conn(seeded["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM search_embeddings").fetchone()[0] == 0

    route.mock(side_effect=embed_side_effect())
    assert run_pass(seeded["db"]) > 0
    assert search_embed.state()["last_error"] is None


@respx.mock
def test_model_change_hides_old_vectors_then_prunes_them(seeded, monkeypatch):
    respx.post(EMBED_URL).mock(side_effect=embed_side_effect())
    run_pass(seeded["db"])

    enable_embeddings(monkeypatch, model="fake-embed-2")
    with get_conn(seeded["db"]) as conn:
        # Old vectors exist but are never read under the new model.
        with pytest.raises(search_embed.SemanticUnavailable):
            search_embed.embed_query(conn, "money")
        assert search_svc.status(conn, owner_id=seeded["owner"], is_admin=False)[
            "embedding"]["embedded"] == 0
        # And not pruned while the new model is still catching up.
        assert search_embed.prune_old_models(conn) == 0

    run_pass(seeded["db"])
    with get_conn(seeded["db"]) as conn:
        search_index.reconcile(conn)
        models = {r[0] for r in conn.execute("SELECT DISTINCT model FROM search_embeddings")}
    assert models == {"fake-embed-2"}


# --------------------------------------------------------------------------- #
# Query
# --------------------------------------------------------------------------- #


@respx.mock
def test_semantic_finds_what_keyword_cannot(seeded):
    respx.post(EMBED_URL).mock(side_effect=embed_side_effect())
    run_pass(seeded["db"])

    assert query(seeded["db"], seeded["owner"], "spending", mode="keyword")["hits"] == []
    result = query(seeded["db"], seeded["owner"], "spending")
    assert result["mode_used"] == "hybrid"
    assert result["semantic"] == {"available": True, "reason": None}
    top = result["hits"][0]
    assert (top["kind"], top["title"]) == ("note", "Finance")
    assert top["matched_by"] == ["semantic"]
    assert "budget" in top["snippet"]


@respx.mock
def test_semantic_transcript_hit_points_at_the_window_start(seeded):
    respx.post(EMBED_URL).mock(side_effect=embed_side_effect())
    run_pass(seeded["db"])
    hits = query(seeded["db"], seeded["owner"], "airport", mode="semantic", kinds="segment")["hits"]
    assert hits and all(h["kind"] == "segment" for h in hits)
    assert hits[0]["start_sec"] == 0.0
    assert hits[0]["url"] == f"/meetings/{seeded['meeting']}?t=0"


@respx.mock
def test_hybrid_dedupes_a_doc_matched_both_ways(seeded):
    respx.post(EMBED_URL).mock(side_effect=embed_side_effect())
    run_pass(seeded["db"])
    hits = query(seeded["db"], seeded["owner"], "budget")["hits"]
    finance = [h for h in hits if h["title"] == "Finance"]
    assert len(finance) == 1
    assert finance[0]["matched_by"] == ["keyword", "semantic"]
    assert hits[0]["title"] == "Finance"


@respx.mock
def test_semantic_fails_open_to_keyword_when_the_endpoint_500s(seeded):
    respx.post(EMBED_URL).mock(side_effect=embed_side_effect())
    run_pass(seeded["db"])
    respx.post(EMBED_URL).mock(return_value=httpx.Response(500, text="down"))

    result = query(seeded["db"], seeded["owner"], "budget")
    assert result["mode_used"] == "keyword"
    assert result["semantic"]["available"] is False
    assert "unavailable" in result["semantic"]["reason"]
    assert [h["title"] for h in result["hits"]] == ["Finance"]
    assert result["hits"][0]["matched_by"] == ["keyword"]

    semantic_only = query(seeded["db"], seeded["owner"], "budget", mode="semantic")
    assert semantic_only["hits"] == [] and semantic_only["semantic"]["available"] is False


def test_semantic_is_off_when_embedding_is_disabled(seeded, monkeypatch):
    from app.config import reset_settings_cache

    monkeypatch.setenv("MMN_EMBEDDING_ENABLED", "false")
    reset_settings_cache()
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(EMBED_URL)
        result = query(seeded["db"], seeded["owner"], "budget")
        assert route.call_count == 0
    assert result["semantic"] == {"available": False, "reason": "Semantic search is turned off"}
    assert [h["title"] for h in result["hits"]] == ["Finance"]
    # Disabled also means the backfill does nothing.
    assert asyncio.run(search_embed.embed_pass(seeded["db"]))["enabled"] is False


@respx.mock
def test_semantic_owner_isolation(seeded):
    respx.post(EMBED_URL).mock(side_effect=embed_side_effect())
    with get_conn(seeded["db"]) as conn:
        bob = make_user(conn, "bob")
        t = threads_svc.create_thread(conn, owner_id=bob, title="Bob")["id"]
        notes_svc.create_note(conn, thread_id=t, meeting_id=None, title="Bob money",
                              body="invoice price cost", source="manual", user_id=bob)
        search_index.index_all_now(conn)
    run_pass(seeded["db"])
    alice_titles = [h["title"] for h in query(seeded["db"], seeded["owner"], "dollars")["hits"]]
    assert "Bob money" not in alice_titles
    assert "Finance" in alice_titles
    bob_titles = [h["title"] for h in query(seeded["db"], bob, "dollars")["hits"]]
    assert bob_titles == ["Bob money"]


@respx.mock
def test_deleted_note_vectors_stop_matching_immediately(seeded):
    respx.post(EMBED_URL).mock(side_effect=embed_side_effect())
    run_pass(seeded["db"])
    with get_conn(seeded["db"]) as conn:
        note_id = conn.execute("SELECT id FROM thread_notes WHERE title = 'Finance'").fetchone()[0]
        notes_svc.delete_note(conn, thread_id=seeded["thread"], note_id=note_id)
    titles = [h["title"] for h in query(seeded["db"], seeded["owner"], "spending")["hits"]]
    assert "Finance" not in titles
