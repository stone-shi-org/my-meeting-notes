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


# --------------------------------------------------------------------------- #
# Moving to another thread -- cascades to everything scoped to the meeting
# --------------------------------------------------------------------------- #


def _attach_email(db_path, thread_id: int, meeting_id: int, message_id: str = "<m1>") -> int:
    from app.db import get_conn, utcnow

    with get_conn(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO thread_emails (thread_id, meeting_id, message_id, subject, date, "
            "raw_json, attached_at) VALUES (?, ?, ?, 'Hi', ?, '{}', ?)",
            (thread_id, meeting_id, message_id, "2026-03-17T00:00:00+00:00", utcnow()),
        )
        return cur.lastrowid


def _attach_event(db_path, thread_id: int, meeting_id: int, uid: str = "evt-1") -> int:
    from app.db import get_conn, utcnow

    with get_conn(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO thread_calendar_events (thread_id, meeting_id, uid, summary, "
            "start_at, raw_json, attached_at) VALUES (?, ?, ?, 'Standup', ?, '{}', ?)",
            (thread_id, meeting_id, uid, "2026-03-18T09:00:00+00:00", utcnow()),
        )
        return cur.lastrowid


def test_moving_a_meeting_updates_counts_on_both_threads(user_client, thread):
    other = user_client.post("/api/threads", json={"title": "Other"}).json()
    m = user_client.post(
        "/api/meetings", json={"thread_id": thread["id"], "title": "Relocating"}
    ).json()

    resp = user_client.post(f"/api/meetings/{m['id']}/move", json={"target_thread_id": other["id"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["thread_id"] == other["id"]

    assert user_client.get(f"/api/threads/{thread['id']}").json()["meeting_count"] == 0
    assert user_client.get(f"/api/threads/{other['id']}").json()["meeting_count"] == 1
    assert [x["title"] for x in user_client.get(f"/api/threads/{other['id']}/meetings").json()["items"]] == [
        "Relocating"
    ]


def test_moving_a_meeting_cascades_its_attachments(user_client, thread, isolated_settings):
    other = user_client.post("/api/threads", json={"title": "Other"}).json()
    m = user_client.post(
        "/api/meetings", json={"thread_id": thread["id"], "title": "Relocating"}
    ).json()
    email_id = _attach_email(isolated_settings.db_path, thread["id"], m["id"])
    event_id = _attach_event(isolated_settings.db_path, thread["id"], m["id"])
    note = user_client.post(
        f"/api/meetings/{m['id']}/notes", json={"body": "notes", "title": "T"}
    ).json()

    assert user_client.post(
        f"/api/meetings/{m['id']}/move", json={"target_thread_id": other["id"]}
    ).status_code == 200

    # Gone from the old thread's attachments...
    assert user_client.get(f"/api/threads/{thread['id']}/emails").json() == []
    assert user_client.get(f"/api/threads/{thread['id']}/calendar-events").json() == []
    assert user_client.get(f"/api/threads/{thread['id']}/notes").json() == []

    # ...and present on the new one, still tied to the same meeting.
    emails = user_client.get(f"/api/threads/{other['id']}/emails").json()
    assert [e["id"] for e in emails] == [email_id]
    assert emails[0]["meeting_id"] == m["id"]

    events = user_client.get(f"/api/threads/{other['id']}/calendar-events").json()
    assert [e["id"] for e in events] == [event_id]
    assert events[0]["meeting_id"] == m["id"]

    notes = user_client.get(f"/api/threads/{other['id']}/notes").json()
    assert [n["id"] for n in notes] == [note["id"]]
    assert notes[0]["meeting_id"] == m["id"]


def test_moving_a_meeting_is_a_conflict_if_an_attached_event_collides(
    user_client, thread, isolated_settings
):
    other = user_client.post("/api/threads", json={"title": "Other"}).json()
    m = user_client.post(
        "/api/meetings", json={"thread_id": thread["id"], "title": "Relocating"}
    ).json()
    _attach_event(isolated_settings.db_path, thread["id"], m["id"], uid="evt-1")
    # Something unrelated already sits on the destination thread under the same uid.
    _attach_event(isolated_settings.db_path, other["id"], meeting_id=None, uid="evt-1")

    resp = user_client.post(f"/api/meetings/{m['id']}/move", json={"target_thread_id": other["id"]})
    assert resp.status_code == 409, resp.text

    # Rolled back entirely: the meeting never moved.
    assert user_client.get(f"/api/meetings/{m['id']}").json()["thread_id"] == thread["id"]
    assert len(user_client.get(f"/api/threads/{thread['id']}/calendar-events").json()) == 1


def test_moving_someone_elses_meeting_is_404(user_client, other_user_client, thread):
    m = user_client.post(
        "/api/meetings", json={"thread_id": thread["id"], "title": "Private"}
    ).json()
    resp = other_user_client.post(
        f"/api/meetings/{m['id']}/move", json={"target_thread_id": thread["id"]}
    )
    assert resp.status_code == 404


def test_moving_into_someone_elses_thread_is_404(user_client, other_user_client, thread):
    m = user_client.post(
        "/api/meetings", json={"thread_id": thread["id"], "title": "Mine"}
    ).json()
    other_thread = other_user_client.post("/api/threads", json={"title": "Not yours"}).json()

    resp = user_client.post(
        f"/api/meetings/{m['id']}/move", json={"target_thread_id": other_thread["id"]}
    )
    assert resp.status_code == 404
    assert user_client.get(f"/api/meetings/{m['id']}").json()["thread_id"] == thread["id"]
