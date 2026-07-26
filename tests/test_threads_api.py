"""Thread CRUD, pagination arithmetic, and the ownership boundary."""

from __future__ import annotations

import pytest


def make_thread(client, title="Atlas Migration", description="Move off Oracle"):
    resp = client.post(
        "/api/threads", json={"title": title, "description": description}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #


def test_create_and_fetch(user_client):
    created = make_thread(user_client)
    assert created["title"] == "Atlas Migration"
    assert created["archived"] is False
    assert created["meeting_count"] == 0

    fetched = user_client.get(f"/api/threads/{created['id']}").json()
    assert fetched == created


def test_update_fields(user_client):
    t = make_thread(user_client)
    resp = user_client.patch(
        f"/api/threads/{t['id']}", json={"title": "Renamed", "archived": True}
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "Renamed"
    assert resp.json()["archived"] is True


def test_delete_removes_the_thread(user_client):
    t = make_thread(user_client)
    assert user_client.delete(f"/api/threads/{t['id']}").status_code == 200
    assert user_client.get(f"/api/threads/{t['id']}").status_code == 404


def test_deleting_a_thread_cascades_to_its_meetings(user_client):
    t = make_thread(user_client)
    m = user_client.post(
        "/api/meetings", json={"thread_id": t["id"], "title": "Standup"}
    ).json()

    body = user_client.delete(f"/api/threads/{t['id']}").json()
    assert body["deleted_meetings"] == 1
    assert user_client.get(f"/api/meetings/{m['id']}").status_code == 404


def test_unknown_thread_is_404(user_client):
    assert user_client.get("/api/threads/9999").status_code == 404


def test_title_is_required(user_client):
    assert user_client.post("/api/threads", json={"title": ""}).status_code == 422


# --------------------------------------------------------------------------- #
# Pagination
# --------------------------------------------------------------------------- #


@pytest.fixture
def many_threads(user_client):
    for i in range(25):
        make_thread(user_client, title=f"Thread {i:02d}")
    return user_client


def test_pagination_arithmetic(many_threads):
    body = many_threads.get("/api/threads", params={"page": 1, "page_size": 10}).json()
    assert body["total"] == 25
    assert body["total_pages"] == 3
    assert body["page"] == 1
    assert body["page_size"] == 10
    assert len(body["items"]) == 10


def test_last_page_is_partial(many_threads):
    body = many_threads.get("/api/threads", params={"page": 3, "page_size": 10}).json()
    assert len(body["items"]) == 5


def test_page_beyond_the_end_is_empty_not_an_error(many_threads):
    body = many_threads.get("/api/threads", params={"page": 99, "page_size": 10}).json()
    assert body["items"] == []
    assert body["total"] == 25


def test_page_size_is_capped(many_threads, isolated_settings):
    body = many_threads.get("/api/threads", params={"page_size": 5000}).json()
    assert body["page_size"] == isolated_settings.page_size_max


def test_page_zero_is_rejected(many_threads):
    assert many_threads.get("/api/threads", params={"page": 0}).status_code == 422


def test_total_pages_is_at_least_one_when_empty(user_client):
    body = user_client.get("/api/threads").json()
    assert body["total"] == 0
    assert body["total_pages"] == 1


def test_pages_do_not_overlap_or_skip(many_threads):
    seen = []
    for page in (1, 2, 3):
        body = many_threads.get(
            "/api/threads", params={"page": page, "page_size": 10}
        ).json()
        seen.extend(i["id"] for i in body["items"])
    assert len(seen) == len(set(seen)) == 25


# --------------------------------------------------------------------------- #
# Filtering and sorting
# --------------------------------------------------------------------------- #


def test_search_matches_title_and_description(user_client):
    make_thread(user_client, title="Atlas Migration", description="Oracle cutover")
    make_thread(user_client, title="Q3 Planning", description="Budget envelope")

    by_title = user_client.get("/api/threads", params={"q": "Atlas"}).json()
    assert [t["title"] for t in by_title["items"]] == ["Atlas Migration"]

    by_desc = user_client.get("/api/threads", params={"q": "budget"}).json()
    assert [t["title"] for t in by_desc["items"]] == ["Q3 Planning"]


def test_archived_threads_are_hidden_by_default(user_client):
    keep = make_thread(user_client, title="Active")
    gone = make_thread(user_client, title="Archived")
    user_client.patch(f"/api/threads/{gone['id']}", json={"archived": True})

    default = user_client.get("/api/threads").json()
    assert [t["id"] for t in default["items"]] == [keep["id"]]

    archived = user_client.get("/api/threads", params={"archived": True}).json()
    assert [t["id"] for t in archived["items"]] == [gone["id"]]


def test_sort_by_title(user_client):
    make_thread(user_client, title="Zebra")
    make_thread(user_client, title="Alpha")

    body = user_client.get(
        "/api/threads", params={"sort": "title", "order": "asc"}
    ).json()
    assert [t["title"] for t in body["items"]] == ["Alpha", "Zebra"]


def test_an_unknown_sort_key_falls_back_rather_than_erroring(user_client):
    make_thread(user_client)
    resp = user_client.get("/api/threads", params={"sort": "; DROP TABLE threads--"})
    assert resp.status_code == 200


def test_search_treats_percent_literally_enough_to_not_error(user_client):
    make_thread(user_client, title="100% done")
    assert user_client.get("/api/threads", params={"q": "%"}).status_code == 200


# --------------------------------------------------------------------------- #
# Ownership
# --------------------------------------------------------------------------- #


def test_user_b_cannot_see_user_a_thread(user_client, other_user_client):
    t = make_thread(user_client)

    assert other_user_client.get(f"/api/threads/{t['id']}").status_code == 404
    assert other_user_client.get("/api/threads").json()["items"] == []


def test_user_b_cannot_modify_or_delete_user_a_thread(user_client, other_user_client):
    t = make_thread(user_client)
    assert other_user_client.patch(
        f"/api/threads/{t['id']}", json={"title": "hijacked"}
    ).status_code == 404
    assert other_user_client.delete(f"/api/threads/{t['id']}").status_code == 404
    assert user_client.get(f"/api/threads/{t['id']}").json()["title"] == "Atlas Migration"


def test_admin_needs_all_flag_to_see_other_threads(admin_client, user_client):
    t = make_thread(user_client)

    without = admin_client.get("/api/threads").json()
    assert t["id"] not in [x["id"] for x in without["items"]]

    with_all = admin_client.get("/api/threads", params={"all": True}).json()
    assert t["id"] in [x["id"] for x in with_all["items"]]


def test_admin_can_open_another_users_thread_directly(admin_client, user_client):
    t = make_thread(user_client)
    assert admin_client.get(f"/api/threads/{t['id']}").status_code == 200


def test_non_admin_all_flag_is_ignored_silently(user_client, other_user_client):
    """A view toggle, not a permission boundary -- 403 would force the SPA to
    branch on role before every request."""
    make_thread(other_user_client, title="Not yours")
    body = user_client.get("/api/threads", params={"all": True}).json()
    assert body["items"] == []


def test_threads_require_authentication(client):
    assert client.get("/api/threads").status_code == 401


# --------------------------------------------------------------------------- #
# Counts and ordering
# --------------------------------------------------------------------------- #


def test_thread_card_counts_reflect_children(user_client):
    t = make_thread(user_client)
    for i in range(3):
        user_client.post(
            "/api/meetings", json={"thread_id": t["id"], "title": f"Meeting {i}"}
        )

    body = user_client.get(f"/api/threads/{t['id']}").json()
    assert body["meeting_count"] == 3
    assert body["last_meeting_at"] is not None
    assert body["email_count"] == 0
    assert body["event_count"] == 0


def test_adding_a_meeting_bumps_the_thread_up_the_list(user_client):
    first = make_thread(user_client, title="Older")
    second = make_thread(user_client, title="Newer")

    order = [t["id"] for t in user_client.get("/api/threads").json()["items"]]
    assert order == [second["id"], first["id"]]

    user_client.post("/api/meetings", json={"thread_id": first["id"], "title": "M"})

    order = [t["id"] for t in user_client.get("/api/threads").json()["items"]]
    assert order == [first["id"], second["id"]]


# --------------------------------------------------------------------------- #
# Timeline
# --------------------------------------------------------------------------- #


def test_timeline_merges_and_sorts_all_three_kinds(user_client, isolated_settings):
    from app.db import get_conn, utcnow

    t = make_thread(user_client)
    user_client.post(
        "/api/meetings",
        json={"thread_id": t["id"], "title": "Standup", "meeting_at": "2026-03-18T09:00:00+00:00"},
    )

    with get_conn(isolated_settings.db_path) as conn:
        conn.execute(
            "INSERT INTO thread_calendar_events (thread_id, uid, summary, start_at, "
            "raw_json, attached_at) VALUES (?, 'uid-1', 'Daily Standup', ?, '{}', ?)",
            (t["id"], "2026-03-18T08:30:00+00:00", utcnow()),
        )
        conn.execute(
            "INSERT INTO thread_emails (thread_id, message_id, subject, date, "
            "raw_json, attached_at) VALUES (?, '<m1>', 'Re: cutover', ?, '{}', ?)",
            (t["id"], "2026-03-17T17:42:00+00:00", utcnow()),
        )

    items = user_client.get(f"/api/threads/{t['id']}/timeline").json()
    assert [i["kind"] for i in items] == ["meeting", "event", "email"]
    assert [i["at"] for i in items] == sorted((i["at"] for i in items), reverse=True)


def test_timeline_respects_ownership(user_client, other_user_client):
    t = make_thread(user_client)
    assert other_user_client.get(f"/api/threads/{t['id']}/timeline").status_code == 404


def test_timeline_of_an_empty_thread(user_client):
    t = make_thread(user_client)
    assert user_client.get(f"/api/threads/{t['id']}/timeline").json() == []


def test_attachment_listing_and_detach(user_client, isolated_settings):
    from app.db import get_conn, utcnow

    t = make_thread(user_client)
    with get_conn(isolated_settings.db_path) as conn:
        conn.execute(
            "INSERT INTO thread_emails (thread_id, message_id, subject, date, "
            "raw_json, attached_at) VALUES (?, '<m1>', 'Hi', ?, '{}', ?)",
            (t["id"], "2026-03-17T00:00:00+00:00", utcnow()),
        )

    emails = user_client.get(f"/api/threads/{t['id']}/emails").json()
    assert len(emails) == 1

    assert user_client.delete(
        f"/api/threads/{t['id']}/emails/{emails[0]['id']}"
    ).status_code == 200
    assert user_client.get(f"/api/threads/{t['id']}/emails").json() == []


def test_detaching_something_not_attached_is_404(user_client):
    t = make_thread(user_client)
    assert user_client.delete(f"/api/threads/{t['id']}/emails/999").status_code == 404
