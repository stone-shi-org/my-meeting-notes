"""Thread groups: CRUD, membership, the Ungrouped default, and the ownership boundary."""

from __future__ import annotations

import pytest


def make_group(client, name="Clients"):
    resp = client.post("/api/thread-groups", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()


def make_thread(client, title="Atlas Migration"):
    resp = client.post("/api/threads", json={"title": title})
    assert resp.status_code == 201, resp.text
    return resp.json()


def move(client, thread_id, group_id):
    return client.put(f"/api/threads/{thread_id}/group", json={"group_id": group_id})


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #


def test_create_and_list(user_client):
    created = make_group(user_client)
    assert created["name"] == "Clients"
    assert created["thread_count"] == 0

    listed = user_client.get("/api/thread-groups").json()
    assert [g["id"] for g in listed] == [created["id"]]


def test_empty_groups_are_listed(user_client):
    """The drop target you just made must come back, or you cannot fill it."""
    make_group(user_client, "Empty")
    assert len(user_client.get("/api/thread-groups").json()) == 1


def test_groups_come_back_in_name_order(user_client):
    for name in ("Zebra", "apple", "Mango"):
        make_group(user_client, name)
    listed = [g["name"] for g in user_client.get("/api/thread-groups").json()]
    assert listed == ["apple", "Mango", "Zebra"]


def test_rename(user_client):
    g = make_group(user_client)
    resp = user_client.patch(f"/api/thread-groups/{g['id']}", json={"name": "Customers"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Customers"


def test_duplicate_name_is_a_conflict(user_client):
    make_group(user_client, "Clients")
    resp = user_client.post("/api/thread-groups", json={"name": "Clients"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"


def test_duplicate_name_ignores_case(user_client):
    """"clients" is the same folder as "Clients", not a second one."""
    make_group(user_client, "Clients")
    assert user_client.post("/api/thread-groups", json={"name": "clients"}).status_code == 409


def test_renaming_onto_an_existing_name_is_a_conflict(user_client):
    make_group(user_client, "Clients")
    other = make_group(user_client, "Internal")
    resp = user_client.patch(f"/api/thread-groups/{other['id']}", json={"name": "Clients"})
    assert resp.status_code == 409


def test_two_users_may_use_the_same_group_name(user_client, other_user_client):
    make_group(user_client, "Clients")
    assert other_user_client.post(
        "/api/thread-groups", json={"name": "Clients"}
    ).status_code == 201


def test_name_is_required(user_client):
    assert user_client.post("/api/thread-groups", json={"name": ""}).status_code == 422


# --------------------------------------------------------------------------- #
# Membership
# --------------------------------------------------------------------------- #


def test_a_new_thread_is_ungrouped(user_client):
    assert make_thread(user_client)["group_id"] is None


def test_move_into_a_group(user_client):
    g = make_group(user_client)
    t = make_thread(user_client)

    resp = move(user_client, t["id"], g["id"])
    assert resp.status_code == 200
    assert resp.json()["group_id"] == g["id"]

    assert user_client.get(f"/api/threads/{t['id']}").json()["group_id"] == g["id"]
    assert user_client.get("/api/thread-groups").json()[0]["thread_count"] == 1


def test_move_back_out_to_ungrouped(user_client):
    g = make_group(user_client)
    t = make_thread(user_client)
    move(user_client, t["id"], g["id"])

    assert move(user_client, t["id"], None).json()["group_id"] is None
    assert user_client.get("/api/thread-groups").json()[0]["thread_count"] == 0


def test_moving_does_not_bump_the_thread(user_client):
    """Filing a thread is not activity on it -- the default sort is last activity."""
    g = make_group(user_client)
    t = make_thread(user_client)
    move(user_client, t["id"], g["id"])
    assert user_client.get(f"/api/threads/{t['id']}").json()["updated_at"] == t["updated_at"]


def test_deleting_a_group_keeps_its_threads(user_client):
    g = make_group(user_client)
    t = make_thread(user_client)
    move(user_client, t["id"], g["id"])

    body = user_client.delete(f"/api/thread-groups/{g['id']}").json()
    assert body["ungrouped_threads"] == 1

    survivor = user_client.get(f"/api/threads/{t['id']}")
    assert survivor.status_code == 200
    assert survivor.json()["group_id"] is None


# --------------------------------------------------------------------------- #
# Listing threads one group at a time
# --------------------------------------------------------------------------- #


def test_filter_by_group(user_client):
    g = make_group(user_client)
    inside = make_thread(user_client, "Inside")
    make_thread(user_client, "Outside")
    move(user_client, inside["id"], g["id"])

    page = user_client.get("/api/threads", params={"group": str(g["id"])}).json()
    assert [t["title"] for t in page["items"]] == ["Inside"]
    assert page["total"] == 1


def test_filter_by_ungrouped(user_client):
    g = make_group(user_client)
    inside = make_thread(user_client, "Inside")
    make_thread(user_client, "Outside")
    move(user_client, inside["id"], g["id"])

    page = user_client.get("/api/threads", params={"group": "none"}).json()
    assert [t["title"] for t in page["items"]] == ["Outside"]


def test_no_group_param_lists_everything(user_client):
    g = make_group(user_client)
    inside = make_thread(user_client, "Inside")
    make_thread(user_client, "Outside")
    move(user_client, inside["id"], g["id"])

    assert user_client.get("/api/threads").json()["total"] == 2


def test_each_group_pages_on_its_own(user_client):
    g = make_group(user_client)
    for i in range(5):
        t = make_thread(user_client, f"Thread {i}")
        move(user_client, t["id"], g["id"])
    make_thread(user_client, "Loose")

    page = user_client.get(
        "/api/threads", params={"group": str(g["id"]), "page_size": 2}
    ).json()
    assert len(page["items"]) == 2
    assert page["total"] == 5
    assert page["total_pages"] == 3


def test_a_junk_group_filter_is_rejected(user_client):
    resp = user_client.get("/api/threads", params={"group": "'; DROP TABLE threads--"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "validation_error"


def test_search_still_applies_inside_a_group(user_client):
    g = make_group(user_client)
    for title in ("Atlas Migration", "Billing rewrite"):
        t = make_thread(user_client, title)
        move(user_client, t["id"], g["id"])

    page = user_client.get(
        "/api/threads", params={"group": str(g["id"]), "q": "Atlas"}
    ).json()
    assert [t["title"] for t in page["items"]] == ["Atlas Migration"]


# --------------------------------------------------------------------------- #
# Ownership
# --------------------------------------------------------------------------- #


def test_someone_elses_group_is_invisible(user_client, other_user_client):
    g = make_group(user_client)
    assert other_user_client.get("/api/thread-groups").json() == []
    assert other_user_client.patch(
        f"/api/thread-groups/{g['id']}", json={"name": "Mine now"}
    ).status_code == 404
    assert other_user_client.delete(f"/api/thread-groups/{g['id']}").status_code == 404


def test_cannot_file_a_thread_into_someone_elses_group(user_client, other_user_client):
    """404, not 403: a 403 would confirm the group exists."""
    g = make_group(user_client)
    t = other_user_client.post("/api/threads", json={"title": "Bob's work"}).json()
    assert move(other_user_client, t["id"], g["id"]).status_code == 404


def test_cannot_move_someone_elses_thread(user_client, other_user_client):
    t = make_thread(user_client)
    g = other_user_client.post("/api/thread-groups", json={"name": "Bob's"}).json()
    assert move(other_user_client, t["id"], g["id"]).status_code == 404


def test_unknown_group_is_404(user_client):
    assert user_client.patch("/api/thread-groups/9999", json={"name": "x"}).status_code == 404
    t = make_thread(user_client)
    assert move(user_client, t["id"], 9999).status_code == 404
