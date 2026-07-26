"""Meeting CRUD and thread resolution. Upload arrives in the job-runner phase."""

from __future__ import annotations

import pytest


@pytest.fixture
def thread(user_client):
    return user_client.post(
        "/api/threads", json={"title": "Atlas Migration", "description": "d"}
    ).json()


def test_create_in_an_existing_thread(user_client, thread):
    resp = user_client.post(
        "/api/meetings",
        json={
            "thread_id": thread["id"],
            "title": "Cutover go/no-go",
            "meeting_at": "2026-03-18T09:00:00+00:00",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["title"] == "Cutover go/no-go"
    assert body["thread_id"] == thread["id"]
    assert body["status"] == "new"
    assert body["has_audio"] is False
    assert body["has_transcript"] is False
    assert body["has_summary"] is False


def test_create_with_a_new_thread_creates_both(user_client):
    resp = user_client.post(
        "/api/meetings",
        json={
            "new_thread_title": "Fresh Project",
            "new_thread_description": "Just started",
            "title": "Kickoff",
        },
    )
    assert resp.status_code == 201
    meeting = resp.json()

    thread = user_client.get(f"/api/threads/{meeting['thread_id']}").json()
    assert thread["title"] == "Fresh Project"
    assert thread["description"] == "Just started"
    assert thread["meeting_count"] == 1


def test_create_requires_a_thread_one_way_or_another(user_client):
    resp = user_client.post("/api/meetings", json={"title": "Orphan"})
    assert resp.status_code == 400
    assert "thread_id" in resp.json()["error"]["message"]


def test_meeting_at_defaults_to_now(user_client, thread):
    body = user_client.post(
        "/api/meetings", json={"thread_id": thread["id"], "title": "No date"}
    ).json()
    assert body["meeting_at"] is not None


def test_update_meeting(user_client, thread):
    m = user_client.post(
        "/api/meetings", json={"thread_id": thread["id"], "title": "Before"}
    ).json()

    resp = user_client.patch(
        f"/api/meetings/{m['id']}",
        json={"title": "After", "notes": "some notes", "meeting_at": "2026-01-01T00:00:00+00:00"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "After"
    assert body["notes"] == "some notes"
    assert body["meeting_at"] == "2026-01-01T00:00:00+00:00"


def test_delete_meeting(user_client, thread):
    m = user_client.post(
        "/api/meetings", json={"thread_id": thread["id"], "title": "Doomed"}
    ).json()

    assert user_client.delete(f"/api/meetings/{m['id']}").status_code == 200
    assert user_client.get(f"/api/meetings/{m['id']}").status_code == 404
    assert user_client.get(f"/api/threads/{thread['id']}").json()["meeting_count"] == 0


def test_list_meetings_filtered_by_thread(user_client):
    a = user_client.post("/api/threads", json={"title": "A"}).json()
    b = user_client.post("/api/threads", json={"title": "B"}).json()
    user_client.post("/api/meetings", json={"thread_id": a["id"], "title": "in A"})
    user_client.post("/api/meetings", json={"thread_id": b["id"], "title": "in B"})

    body = user_client.get("/api/meetings", params={"thread_id": a["id"]}).json()
    assert [m["title"] for m in body["items"]] == ["in A"]


def test_list_meetings_is_paginated(user_client, thread):
    for i in range(12):
        user_client.post(
            "/api/meetings", json={"thread_id": thread["id"], "title": f"M{i}"}
        )

    body = user_client.get("/api/meetings", params={"page_size": 5}).json()
    assert len(body["items"]) == 5
    assert body["total"] == 12
    assert body["total_pages"] == 3


def test_thread_meetings_endpoint_is_paginated(user_client, thread):
    for i in range(7):
        user_client.post(
            "/api/meetings", json={"thread_id": thread["id"], "title": f"M{i}"}
        )

    body = user_client.get(
        f"/api/threads/{thread['id']}/meetings", params={"page_size": 3}
    ).json()
    assert len(body["items"]) == 3
    assert body["total"] == 7


def test_meetings_are_listed_newest_first(user_client, thread):
    user_client.post(
        "/api/meetings",
        json={"thread_id": thread["id"], "title": "old", "meeting_at": "2026-01-01T00:00:00+00:00"},
    )
    user_client.post(
        "/api/meetings",
        json={"thread_id": thread["id"], "title": "new", "meeting_at": "2026-06-01T00:00:00+00:00"},
    )

    body = user_client.get("/api/meetings", params={"thread_id": thread["id"]}).json()
    assert [m["title"] for m in body["items"]] == ["new", "old"]


# --------------------------------------------------------------------------- #
# Ownership
# --------------------------------------------------------------------------- #


def test_cannot_read_someone_elses_meeting(user_client, other_user_client, thread):
    m = user_client.post(
        "/api/meetings", json={"thread_id": thread["id"], "title": "Private"}
    ).json()

    assert other_user_client.get(f"/api/meetings/{m['id']}").status_code == 404
    assert other_user_client.patch(
        f"/api/meetings/{m['id']}", json={"title": "hijacked"}
    ).status_code == 404
    assert other_user_client.delete(f"/api/meetings/{m['id']}").status_code == 404


def test_cannot_add_a_meeting_to_someone_elses_thread(other_user_client, thread):
    resp = other_user_client.post(
        "/api/meetings", json={"thread_id": thread["id"], "title": "Intruder"}
    )
    assert resp.status_code == 404


def test_cannot_list_meetings_in_someone_elses_thread(other_user_client, thread):
    assert other_user_client.get(
        f"/api/threads/{thread['id']}/meetings"
    ).status_code == 404


def test_admin_can_see_all_meetings_with_the_flag(admin_client, user_client, thread):
    user_client.post("/api/meetings", json={"thread_id": thread["id"], "title": "Theirs"})

    assert admin_client.get("/api/meetings").json()["total"] == 0
    assert admin_client.get("/api/meetings", params={"all": True}).json()["total"] == 1
