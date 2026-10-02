"""MMN-15: the /api/search contract, status, rebuild, and the indexer task."""

from __future__ import annotations

import asyncio

import pytest

from app.db import get_conn
from app.services import search_index


def make_thread(client, title, description=None):
    resp = client.post("/api/threads", json={"title": title, "description": description})
    assert resp.status_code == 201, resp.text
    return resp.json()


def index_now(db_path):
    with get_conn(db_path) as conn:
        search_index.index_all_now(conn)


HIT_KEYS = {
    "kind", "id", "ref_id", "thread_id", "thread_title", "meeting_id", "meeting_title",
    "start_sec", "title", "snippet", "date", "score", "matched_by", "url",
}


def test_search_contract(user_client, isolated_settings):
    t = make_thread(user_client, "Atlas migration")
    m = user_client.post(
        "/api/meetings", json={"thread_id": t["id"], "title": "Atlas kickoff"}
    ).json()
    user_client.post(f"/api/threads/{t['id']}/notes",
                     json={"title": "Atlas risks", "body": "Rollback plan"})
    index_now(isolated_settings.db_path)

    resp = user_client.get("/api/search", params={"q": "atlas"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"query", "mode", "mode_used", "semantic", "limit", "offset", "has_more", "hits"}
    assert body["mode"] == "hybrid" and body["mode_used"] == "keyword"
    assert body["semantic"] == {"available": False, "reason": "Semantic search is turned off"}
    assert {h["kind"] for h in body["hits"]} == {"thread", "meeting", "note"}
    for hit in body["hits"]:
        assert set(hit) == HIT_KEYS
        assert hit["thread_id"] == t["id"]
        assert hit["thread_title"] == "Atlas migration"
        assert hit["matched_by"] == ["keyword"]
    meeting_hit = next(h for h in body["hits"] if h["kind"] == "meeting")
    assert meeting_hit["url"] == f"/meetings/{m['id']}"
    assert meeting_hit["meeting_title"] == "Atlas kickoff"
    note_hit = next(h for h in body["hits"] if h["kind"] == "note")
    assert note_hit["url"] == f"/threads/{t['id']}"


def test_validation(user_client):
    assert user_client.get("/api/search").status_code == 422
    assert user_client.get("/api/search", params={"q": ""}).status_code == 422
    assert user_client.get("/api/search", params={"q": "x" * 501}).status_code == 422
    assert user_client.get("/api/search", params={"q": "x", "limit": 101}).status_code == 422
    assert user_client.get("/api/search", params={"q": "x", "mode": "fuzzy"}).status_code == 422
    bad_kind = user_client.get("/api/search", params={"q": "x", "kinds": "banana"})
    assert bad_kind.status_code == 400
    assert bad_kind.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize("q", ['"', "-", "*", "NEAR(", "🙂", 'a"b*c-'])
def test_hostile_queries_return_200(user_client, q):
    make_thread(user_client, "Anything")
    assert user_client.get("/api/search", params={"q": q}).status_code == 200


def test_owner_isolation_and_404_for_someone_elses_thread(user_client, other_user_client, isolated_settings):
    mine = make_thread(user_client, "Narwhal plans")
    theirs = make_thread(other_user_client, "Narwhal secrets")
    index_now(isolated_settings.db_path)

    titles = [h["title"] for h in user_client.get("/api/search", params={"q": "narwhal"}).json()["hits"]]
    assert titles == ["Narwhal plans"]

    scoped = user_client.get("/api/search", params={"q": "narwhal", "thread_id": mine["id"]})
    assert scoped.status_code == 200 and len(scoped.json()["hits"]) == 1
    other = user_client.get("/api/search", params={"q": "narwhal", "thread_id": theirs["id"]})
    assert other.status_code == 404
    missing = user_client.get("/api/search", params={"q": "narwhal", "thread_id": 99999})
    assert missing.status_code == 404


def test_detach_and_delete_drop_hits_without_an_indexer_run(user_client, isolated_settings):
    t = make_thread(user_client, "Container")
    note = user_client.post(f"/api/threads/{t['id']}/notes",
                            json={"title": "Okapi", "body": "okapi"}).json()
    index_now(isolated_settings.db_path)
    assert user_client.get("/api/search", params={"q": "okapi"}).json()["hits"]

    assert user_client.delete(f"/api/threads/{t['id']}/notes/{note['id']}").status_code == 200
    assert user_client.get("/api/search", params={"q": "okapi"}).json()["hits"] == []

    m = user_client.post("/api/meetings", json={"thread_id": t["id"], "title": "Tapir sync"}).json()
    index_now(isolated_settings.db_path)
    assert user_client.delete(f"/api/meetings/{m['id']}").status_code == 200
    assert user_client.get("/api/search", params={"q": "tapir"}).json()["hits"] == []

    assert user_client.delete(f"/api/threads/{t['id']}").status_code == 200
    assert user_client.get("/api/search", params={"q": "container"}).json()["hits"] == []


def test_thread_list_filter_end_to_end(user_client):
    make_thread(user_client, "Weekly meeting notes")
    make_thread(user_client, "Budget", description="Q3 envelope")
    get = lambda q: [t["title"] for t in user_client.get("/api/threads", params={"q": q}).json()["items"]]
    assert get("meet") == []
    assert get("meet*") == ["Weekly meeting notes"]
    assert get("envelope q3") == ["Budget"]
    # Rename is visible to the filter on the very next request.
    tid = user_client.get("/api/threads", params={"q": "budget"}).json()["items"][0]["id"]
    user_client.patch(f"/api/threads/{tid}", json={"title": "Forecast"})
    assert get("forecast") == ["Forecast"]
    assert get("budget") == []


def test_status_shape(user_client, isolated_settings):
    t = make_thread(user_client, "Status thread")
    user_client.post("/api/meetings", json={"thread_id": t["id"], "title": "Status meeting"})

    before = user_client.get("/api/search/status").json()
    assert before["pending_scopes"] == 2
    index_now(isolated_settings.db_path)
    body = user_client.get("/api/search/status").json()
    assert body["pending_scopes"] == 0
    assert {k["kind"]: k["indexed"] for k in body["kinds"]}["meeting"] == 1
    assert [k["kind"] for k in body["kinds"]] == [
        "thread", "meeting", "segment", "summary", "action_item", "note", "email", "event",
    ]
    assert body["embedding"]["enabled"] is False
    assert body["embedding"]["pending_scopes"] == 0
    assert body["embedding"]["scale_limit"] > 0
    assert body["is_admin"] is False
    assert "global" not in body


def test_rebuild_is_admin_only(user_client, admin_client, isolated_settings):
    make_thread(user_client, "Rebuild me")
    index_now(isolated_settings.db_path)
    assert user_client.post("/api/search/rebuild").status_code in (403, 404)

    resp = admin_client.post("/api/search/rebuild")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True and resp.json()["queued_scopes"] >= 1
    status = admin_client.get("/api/search/status").json()
    assert status["global"]["pending_scopes"] >= 1

    index_now(isolated_settings.db_path)
    assert user_client.get("/api/search", params={"q": "rebuild"}).json()["hits"]


def _seed_unindexed_meeting(db_path, title="Gazelle review"):
    from app.services import threads as threads_svc
    from tests.search_support import make_user

    with get_conn(db_path) as conn:
        owner = make_user(conn, "alice")
        t = threads_svc.create_thread(conn, owner_id=owner, title="T")["id"]
        threads_svc.create_meeting(conn, thread_id=t, owner_id=owner, title=title)
    return owner, t


def _meeting_docs(db_path):
    with get_conn(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM search_docs WHERE kind = 'meeting'").fetchone()[0]


def test_indexer_cycle_reconciles_and_drains(initialised_db):
    """One cycle, driven by hand (the loop itself is not started, so nothing
    races the assertion)."""
    from app.jobs.search_indexer import SearchIndexer

    _seed_unindexed_meeting(initialised_db)
    result = asyncio.run(SearchIndexer(initialised_db).run_once())
    assert result["indexed"] >= 1
    with get_conn(initialised_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM search_dirty").fetchone()[0] == 0
    assert _meeting_docs(initialised_db) == 1


def test_running_indexer_is_woken_by_a_write(initialised_db):
    """The started loop picks up a write within about a second -- the nudge,
    not the idle tick (which is longer than this test waits)."""
    from app.jobs import search_indexer
    from app.services import notes as notes_svc

    async def go():
        indexer = search_indexer.SearchIndexer(initialised_db)
        indexer.start()
        try:
            owner, t = await asyncio.to_thread(_seed_unindexed_meeting, initialised_db)
            # Let the startup reconcile + drain settle and the loop go idle.
            for _ in range(40):
                if _meeting_docs(initialised_db) == 1:
                    break
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.2)

            def write():
                with get_conn(initialised_db) as conn:
                    notes_svc.create_note(conn, thread_id=t, meeting_id=None, title="Eland",
                                          body="eland", source="manual", user_id=owner)

            await asyncio.to_thread(write)
            for _ in range(60):  # up to 3s, well under IDLE_SEC + SETTLE_SEC
                with get_conn(initialised_db) as conn:
                    if conn.execute(
                        "SELECT 1 FROM search_docs WHERE kind = 'note'"
                    ).fetchone():
                        return True
                await asyncio.sleep(0.05)
            return False
        finally:
            await indexer.stop()

    assert search_indexer.IDLE_SEC > 3
    assert asyncio.run(go()) is True
