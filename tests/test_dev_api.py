"""Authoring fake data: the routes, the flag, the ownership boundary, and generation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import reset_settings_cache
from app.db import get_conn


@pytest.fixture(autouse=True)
def dev_on(monkeypatch):
    monkeypatch.setenv("MMN_DEV_PROVIDER_ENABLED", "1")
    reset_settings_cache()
    yield
    reset_settings_cache()


def connect(client, label="Fixtures"):
    resp = client.post(
        "/api/integrations", json={"provider": "dev", "account_label": label}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def add_email(client, integration_id, **fields):
    body = {"subject": "Re: Atlas cutover", "offset_minutes": -1440, **fields}
    return client.post(f"/api/dev/integrations/{integration_id}/emails", json=body)


def add_event(client, integration_id, **fields):
    body = {"summary": "Atlas standup", "offset_minutes": 60, **fields}
    return client.post(f"/api/dev/integrations/{integration_id}/events", json=body)


# --------------------------------------------------------------------------- #
# Connecting the account
# --------------------------------------------------------------------------- #


def test_the_provider_is_offered(user_client):
    ids = [p["id"] for p in user_client.get("/api/integrations/providers").json()]
    assert "dev" in ids


def test_connect_needs_no_credentials(user_client):
    account = connect(user_client)
    assert account["provider"] == "dev"
    assert account["calendar_enabled"] and account["email_enabled"]
    assert account["has_secret"] is False


def test_two_accounts_under_two_labels(user_client):
    connect(user_client, "Fixtures")
    assert connect(user_client, "Second set")["account_key"] == "second-set"


def test_the_same_label_twice_is_a_conflict(user_client):
    """The unique index doing its documented job: repeated Connect clicks must
    not pile up rows."""
    connect(user_client, "Fixtures")
    resp = user_client.post(
        "/api/integrations", json={"provider": "dev", "account_label": "Fixtures"}
    )
    assert resp.status_code == 409


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #


def test_create_and_list_an_email(user_client):
    account = connect(user_client)
    created = add_email(user_client, account["id"], sender="Jane <jane@example.com>")
    assert created.status_code == 201
    assert created.json()["subject"] == "Re: Atlas cutover"

    listed = user_client.get(f"/api/dev/integrations/{account['id']}/emails").json()
    assert [e["id"] for e in listed] == [created.json()["id"]]


def test_create_and_list_an_event(user_client):
    account = connect(user_client)
    created = add_event(user_client, account["id"], attendees=["Jane Doe", "Bob"])
    assert created.status_code == 201
    assert created.json()["attendees"] == ["Jane Doe", "Bob"]


def test_update_and_delete(user_client):
    account = connect(user_client)
    item = add_email(user_client, account["id"]).json()

    patched = user_client.patch(
        f"/api/dev/emails/{item['id']}", json={"subject": "Renamed"}
    )
    assert patched.json()["subject"] == "Renamed"

    assert user_client.delete(f"/api/dev/emails/{item['id']}").status_code == 200
    assert user_client.get(f"/api/dev/integrations/{account['id']}/emails").json() == []


def test_a_subject_is_required(user_client):
    account = connect(user_client)
    resp = user_client.post(
        f"/api/dev/integrations/{account['id']}/emails", json={"subject": "  "}
    )
    assert resp.status_code == 400


def test_an_absolute_item_needs_a_date(user_client):
    account = connect(user_client)
    resp = add_email(user_client, account["id"], date_mode="absolute", at=None)
    assert resp.status_code == 400


def test_an_anchored_item_needs_a_meeting(user_client):
    account = connect(user_client)
    resp = add_email(user_client, account["id"], date_mode="anchored")
    assert resp.status_code == 400


def test_an_unknown_date_mode_is_rejected(user_client):
    account = connect(user_client)
    resp = add_email(user_client, account["id"], date_mode="whenever")
    assert resp.status_code == 400


def test_repeat_is_bounded(user_client):
    """Every instance is persisted three times per match run."""
    account = connect(user_client)
    assert add_event(user_client, account["id"], repeat_weekly=500).status_code == 400


def test_unknown_fields_are_ignored_not_stored(user_client):
    """The write narrows to a named column list, so a client cannot reassign an
    item to another account or pick its own id."""
    account = connect(user_client)
    created = user_client.post(
        f"/api/dev/integrations/{account['id']}/emails",
        json={"subject": "Hi", "integration_id": 999, "id": 42, "nonsense": True},
    ).json()
    assert created["integration_id"] == account["id"]
    assert created["id"] != 42


# --------------------------------------------------------------------------- #
# The flag
# --------------------------------------------------------------------------- #


def test_every_route_404s_when_disabled(user_client, monkeypatch):
    account = connect(user_client)
    item = add_email(user_client, account["id"]).json()

    monkeypatch.setenv("MMN_DEV_PROVIDER_ENABLED", "0")
    reset_settings_cache()

    assert user_client.get(f"/api/dev/integrations/{account['id']}/emails").status_code == 404
    assert add_email(user_client, account["id"]).status_code == 404
    assert user_client.patch(f"/api/dev/emails/{item['id']}", json={}).status_code == 404
    assert user_client.delete(f"/api/dev/emails/{item['id']}").status_code == 404
    assert user_client.get(f"/api/dev/integrations/{account['id']}/export").status_code == 404


def test_cannot_connect_one_when_disabled(user_client, monkeypatch):
    monkeypatch.setenv("MMN_DEV_PROVIDER_ENABLED", "0")
    reset_settings_cache()

    resp = user_client.post("/api/integrations", json={"provider": "dev"})
    assert resp.status_code == 400
    assert "MMN_DEV_PROVIDER_ENABLED" in resp.json()["error"]["message"]


def test_the_provider_is_hidden_when_disabled(user_client, monkeypatch):
    monkeypatch.setenv("MMN_DEV_PROVIDER_ENABLED", "0")
    reset_settings_cache()

    ids = [p["id"] for p in user_client.get("/api/integrations/providers").json()]
    assert "dev" not in ids


# --------------------------------------------------------------------------- #
# Ownership
# --------------------------------------------------------------------------- #


def test_someone_elses_account_is_404(user_client, other_user_client):
    account = connect(user_client)
    assert other_user_client.get(
        f"/api/dev/integrations/{account['id']}/emails"
    ).status_code == 404
    assert add_email(other_user_client, account["id"]).status_code == 404


def test_someone_elses_item_is_404(user_client, other_user_client):
    """Guessing an item id must not reach another user's fixtures."""
    account = connect(user_client)
    item = add_email(user_client, account["id"]).json()

    assert other_user_client.patch(
        f"/api/dev/emails/{item['id']}", json={"subject": "mine"}
    ).status_code == 404
    assert other_user_client.delete(f"/api/dev/emails/{item['id']}").status_code == 404


def test_a_non_dev_integration_is_rejected(user_client):
    mcp = user_client.post(
        "/api/integrations",
        json={
            "provider": "mcp_email",
            "config": {"base_url": "http://email-mcp.test:4003"},
            "secret": {"auth_token": "t"},
        },
    ).json()
    assert add_email(user_client, mcp["id"]).status_code == 400


# --------------------------------------------------------------------------- #
# Export / import
# --------------------------------------------------------------------------- #


def test_export_import_round_trip(user_client):
    source = connect(user_client, "Source")
    add_email(user_client, source["id"], subject="Kept")
    add_event(user_client, source["id"], summary="Also kept")

    exported = user_client.get(f"/api/dev/integrations/{source['id']}/export").json()
    assert len(exported["emails"]) == 1 and len(exported["events"]) == 1

    target = connect(user_client, "Target")
    resp = user_client.post(f"/api/dev/integrations/{target['id']}/import", json=exported)
    assert resp.json()["imported"] == {"emails": 1, "events": 1}

    landed = user_client.get(f"/api/dev/integrations/{target['id']}/emails").json()
    assert landed[0]["subject"] == "Kept"
    assert landed[0]["integration_id"] == target["id"]


def test_import_is_additive(user_client):
    """A silent wipe of what you had authored is not a recoverable mistake."""
    account = connect(user_client)
    add_email(user_client, account["id"], subject="Already here")
    user_client.post(
        f"/api/dev/integrations/{account['id']}/import",
        json={"emails": [{"subject": "New"}], "events": []},
    )
    subjects = {
        e["subject"]
        for e in user_client.get(f"/api/dev/integrations/{account['id']}/emails").json()
    }
    assert subjects == {"Already here", "New"}


def test_disconnecting_takes_the_items_with_it(user_client, db_path):
    account = connect(user_client)
    add_email(user_client, account["id"])
    user_client.delete(f"/api/integrations/{account['id']}")

    with get_conn(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dev_emails").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #


GOOD_REPLY = {
    "items": [
        {
            "kind": "email",
            "subject": "Re: Atlas cutover window",
            "sender": "Jane Doe <jane@example.com>",
            "snippet": "Rollback plan attached.",
            "date_mode": "anchored",
            "anchor_meeting_id": None,  # filled in by the test
            "offset_minutes": 2880,
            "expected_relevant": True,
            "note": "direct follow-up",
        },
        {
            "kind": "event",
            "summary": "Expenses deadline",
            "attendees": ["Someone Else"],
            "date_mode": "relative",
            "offset_minutes": 1440,
            "expected_relevant": False,
            "note": "office noise",
        },
    ]
}


def seed_thread(client):
    thread = client.post("/api/threads", json={"title": "Atlas Migration"}).json()
    meeting = client.post(
        "/api/meetings", json={"thread_id": thread["id"], "title": "Kickoff"}
    ).json()
    return thread, meeting


def stub_llm(monkeypatch, payload):
    from app.services import llm as llm_svc

    monkeypatch.setattr(
        llm_svc, "chat_json", lambda *a, **k: (payload, {}, "raw"), raising=True
    )


def test_generate_returns_drafts_without_writing(user_client, monkeypatch):
    """A model that returns half a batch of nonsense should cost a click, not a
    cleanup -- so nothing is persisted until the user accepts it."""
    account = connect(user_client)
    thread, meeting = seed_thread(user_client)

    reply = {"items": [dict(i) for i in GOOD_REPLY["items"]]}
    reply["items"][0]["anchor_meeting_id"] = meeting["id"]
    stub_llm(monkeypatch, reply)

    resp = user_client.post(
        f"/api/dev/integrations/{account['id']}/generate",
        json={"thread_id": thread["id"], "count": 5},
    )
    assert resp.status_code == 200
    drafts = resp.json()["drafts"]
    assert [d["kind"] for d in drafts] == ["emails", "events"]
    assert drafts[0]["anchor_meeting_id"] == meeting["id"]
    assert drafts[1]["expected_relevant"] is False

    assert user_client.get(f"/api/dev/integrations/{account['id']}/emails").json() == []


def test_a_draft_anchored_to_an_invented_meeting_keeps_the_item(user_client, monkeypatch):
    account = connect(user_client)
    thread, _ = seed_thread(user_client)
    stub_llm(
        monkeypatch,
        {"items": [{"kind": "email", "subject": "Hi", "date_mode": "anchored",
                    "anchor_meeting_id": 9999, "offset_minutes": 60}]},
    )

    drafts = user_client.post(
        f"/api/dev/integrations/{account['id']}/generate",
        json={"thread_id": thread["id"]},
    ).json()["drafts"]
    assert drafts[0]["date_mode"] == "relative"
    assert drafts[0]["anchor_meeting_id"] is None


def test_unusable_drafts_are_dropped_not_fatal(user_client, monkeypatch):
    account = connect(user_client)
    thread, _ = seed_thread(user_client)
    stub_llm(
        monkeypatch,
        {"items": ["not an object", {"kind": "email"}, {"kind": "email", "subject": "Keeper"}]},
    )

    drafts = user_client.post(
        f"/api/dev/integrations/{account['id']}/generate",
        json={"thread_id": thread["id"]},
    ).json()["drafts"]
    assert [d["subject"] for d in drafts] == ["Keeper"]


def test_an_empty_reply_is_not_an_error(user_client, monkeypatch):
    account = connect(user_client)
    thread, _ = seed_thread(user_client)
    stub_llm(monkeypatch, {"items": []})

    resp = user_client.post(
        f"/api/dev/integrations/{account['id']}/generate",
        json={"thread_id": thread["id"]},
    )
    assert resp.status_code == 200
    assert resp.json()["drafts"] == []


def test_cannot_generate_against_someone_elses_thread(user_client, other_user_client, monkeypatch):
    account = connect(other_user_client, "Bob's")
    thread, _ = seed_thread(user_client)
    stub_llm(monkeypatch, {"items": []})

    resp = other_user_client.post(
        f"/api/dev/integrations/{account['id']}/generate",
        json={"thread_id": thread["id"]},
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# End to end: the wiring the whole feature exists for
# --------------------------------------------------------------------------- #


async def test_authored_items_reach_the_match_pipeline(user_client, db_path):
    """Through the real loader → registry → provider chain, into
    gather_candidates. If this passes, everything downstream -- ranking,
    attach, the sweep -- is being fed the same shape a real provider produces."""
    from app.services import matching as matching_svc

    account = connect(user_client)
    thread, _ = seed_thread(user_client)
    add_email(user_client, account["id"], subject="Re: Atlas rollback window")
    add_event(user_client, account["id"], summary="Atlas cutover rehearsal")

    now = datetime.now(timezone.utc)
    gathered = await matching_svc.gather_candidates(
        lambda: get_conn(db_path),
        thread_id=thread["id"],
        keywords=["Atlas"],
        start=now - timedelta(days=30),
        end=now + timedelta(days=30),
        max_candidates=20,
        user_id=user_client_id(user_client),
    )

    assert [e["subject"] for e in gathered["emails"]] == ["Re: Atlas rollback window"]
    assert [e["summary"] for e in gathered["events"]] == ["Atlas cutover rehearsal"]
    assert gathered["emails"][0]["provider"] == "dev"


def user_client_id(client) -> int:
    return client.get("/api/auth/me").json()["id"]
