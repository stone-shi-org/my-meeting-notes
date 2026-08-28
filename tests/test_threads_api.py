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


def test_archiving_is_reversible_and_keeps_everything(user_client):
    """Archiving is a filing decision, not a destructive one.

    The Archive button on a thread sends exactly this, and the round trip is
    what makes it safe to press: the meetings are still there afterwards and
    unarchiving puts the thread back on the default list.
    """
    t = make_thread(user_client, title="Shipped")
    user_client.post("/api/meetings", json={"thread_id": t["id"], "title": "Retro"})

    away = user_client.patch(f"/api/threads/{t['id']}", json={"archived": True}).json()
    assert away["archived"] is True
    assert away["meeting_count"] == 1
    assert user_client.get(f"/api/threads/{t['id']}").status_code == 200
    assert user_client.get("/api/threads").json()["items"] == []

    back = user_client.patch(f"/api/threads/{t['id']}", json={"archived": False}).json()
    assert back["archived"] is False
    assert back["meeting_count"] == 1
    assert [x["id"] for x in user_client.get("/api/threads").json()["items"]] == [t["id"]]


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
    # Emails arrive grouped: a lone email is a chain of one, so the timeline has
    # a single email renderer rather than two that could drift apart.
    assert [i["kind"] for i in items] == ["meeting", "event", "email_chain"]
    assert [i["at"] for i in items] == sorted((i["at"] for i in items), reverse=True)

    chain = items[-1]["payload"]
    assert chain["message_count"] == 1
    assert chain["subject"] == "Re: cutover"
    # Keyed on the root message's row id, not the newest, so a later reply does
    # not change the React key and remount the card.
    assert items[-1]["id"] == chain["messages"][0]["id"]


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


def _attach_event(db_path, thread_id: int, uid: str = "evt-1") -> int:
    from app.db import get_conn, utcnow

    with get_conn(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO thread_calendar_events (thread_id, uid, summary, start_at, "
            "raw_json, attached_at) VALUES (?, ?, 'Standup', ?, '{}', ?)",
            (thread_id, uid, "2026-03-18T09:00:00+00:00", utcnow()),
        )
        return cur.lastrowid


def test_detaching_a_calendar_event(user_client, isolated_settings):
    """The other half of the Remove button on the timeline."""
    t = make_thread(user_client)
    event_id = _attach_event(isolated_settings.db_path, t["id"])

    assert len(user_client.get(f"/api/threads/{t['id']}/calendar-events").json()) == 1
    assert user_client.delete(
        f"/api/threads/{t['id']}/calendar-events/{event_id}"
    ).status_code == 200
    assert user_client.get(f"/api/threads/{t['id']}/calendar-events").json() == []


def test_detaching_removes_it_from_the_timeline(user_client, isolated_settings):
    """What the user actually sees disappear."""
    t = make_thread(user_client)
    event_id = _attach_event(isolated_settings.db_path, t["id"])

    timeline = user_client.get(f"/api/threads/{t['id']}/timeline").json()
    assert [i["kind"] for i in timeline] == ["event"]

    user_client.delete(f"/api/threads/{t['id']}/calendar-events/{event_id}")
    assert user_client.get(f"/api/threads/{t['id']}/timeline").json() == []


def test_another_user_cannot_detach_your_attachments(
    user_client, other_user_client, isolated_settings
):
    """404 rather than 403 -- a 403 would confirm the thread exists."""
    t = make_thread(user_client)
    event_id = _attach_event(isolated_settings.db_path, t["id"])

    assert other_user_client.delete(
        f"/api/threads/{t['id']}/calendar-events/{event_id}"
    ).status_code == 404
    # And it is still there afterwards.
    assert len(user_client.get(f"/api/threads/{t['id']}/calendar-events").json()) == 1


# --------------------------------------------------------------------------- #
# Moving an attachment to another thread
# --------------------------------------------------------------------------- #


def _attach_email(
    db_path, thread_id: int, message_id: str = "<m1>", meeting_id: int | None = None,
    auto_attached: int = 1,
) -> int:
    from app.db import get_conn, utcnow

    with get_conn(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO thread_emails (thread_id, meeting_id, message_id, subject, date, "
            "raw_json, auto_attached, attached_at) VALUES (?, ?, ?, 'Hi', ?, '{}', ?, ?)",
            (thread_id, meeting_id, message_id, "2026-03-17T00:00:00+00:00", auto_attached, utcnow()),
        )
        return cur.lastrowid


def test_moving_an_email_clears_meeting_id_and_the_unread_mark(user_client, isolated_settings):
    source = make_thread(user_client, title="Source")
    target = make_thread(user_client, title="Target")
    meeting = user_client.post(
        "/api/meetings", json={"thread_id": source["id"], "title": "Kickoff"}
    ).json()
    email_id = _attach_email(isolated_settings.db_path, source["id"], meeting_id=meeting["id"])

    resp = user_client.post(
        f"/api/threads/{source['id']}/emails/{email_id}/move",
        json={"target_thread_id": target["id"]},
    )
    assert resp.status_code == 200, resp.text

    assert user_client.get(f"/api/threads/{source['id']}/emails").json() == []
    moved = user_client.get(f"/api/threads/{target['id']}/emails").json()
    assert len(moved) == 1
    assert moved[0]["id"] == email_id
    assert moved[0]["meeting_id"] is None
    assert moved[0]["unread"] is False, "a move is a person acting on it, not the sweep"


def test_moving_an_event_that_already_exists_on_the_target_thread_is_a_conflict(
    user_client, isolated_settings
):
    source = make_thread(user_client, title="Source")
    target = make_thread(user_client, title="Target")
    event_id = _attach_event(isolated_settings.db_path, source["id"], uid="evt-1")
    _attach_event(isolated_settings.db_path, target["id"], uid="evt-1")

    resp = user_client.post(
        f"/api/threads/{source['id']}/calendar-events/{event_id}/move",
        json={"target_thread_id": target["id"]},
    )
    assert resp.status_code == 409, resp.text
    # Untouched on both ends.
    assert len(user_client.get(f"/api/threads/{source['id']}/calendar-events").json()) == 1
    assert len(user_client.get(f"/api/threads/{target['id']}/calendar-events").json()) == 1


def test_moving_something_not_attached_is_404(user_client):
    source = make_thread(user_client, title="Source")
    target = make_thread(user_client, title="Target")
    resp = user_client.post(
        f"/api/threads/{source['id']}/emails/999/move",
        json={"target_thread_id": target["id"]},
    )
    assert resp.status_code == 404


def test_moving_into_someone_elses_thread_is_404(user_client, other_user_client, isolated_settings):
    source = make_thread(user_client, title="Source")
    other_thread = make_thread(other_user_client, title="Not yours")
    event_id = _attach_event(isolated_settings.db_path, source["id"])

    resp = user_client.post(
        f"/api/threads/{source['id']}/calendar-events/{event_id}/move",
        json={"target_thread_id": other_thread["id"]},
    )
    assert resp.status_code == 404
    assert len(user_client.get(f"/api/threads/{source['id']}/calendar-events").json()) == 1


def test_moving_someone_elses_attachment_is_404(user_client, other_user_client, isolated_settings):
    t = make_thread(user_client)
    event_id = _attach_event(isolated_settings.db_path, t["id"])

    resp = other_user_client.post(
        f"/api/threads/{t['id']}/calendar-events/{event_id}/move",
        json={"target_thread_id": t["id"]},
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Email conversations, bodies and hydration
# --------------------------------------------------------------------------- #


def _attach_email(db_path, thread_id, **extra):
    from app.db import get_conn, utcnow

    values = {
        "thread_id": thread_id,
        "message_id": extra.pop("message_id", "<m1>"),
        "subject": extra.pop("subject", "Re: cutover"),
        "sender": extra.pop("sender", "priya@acme.com"),
        "date": extra.pop("date", "2026-03-17T17:42:00+00:00"),
        "raw_json": "{}",
        "attached_at": utcnow(),
        **extra,
    }
    with get_conn(db_path) as conn:
        cur = conn.execute(
            f"INSERT INTO thread_emails ({', '.join(values)}) "
            f"VALUES ({', '.join('?' * len(values))})",
            list(values.values()),
        )
        return cur.lastrowid


def test_the_email_list_never_returns_a_body(user_client, isolated_settings):
    """SQLite reads whole rows, so a 32KB body would be pulled off disk by every
    list load that never displays one."""
    t = make_thread(user_client)
    _attach_email(isolated_settings.db_path, t["id"], body="the full text",
                  body_fetched_at="2026-08-27T10:00:00+00:00")

    [email] = user_client.get(f"/api/threads/{t['id']}/emails").json()

    assert "body" not in email
    # ...but whether there is one, so the UI can offer to load it.
    assert email["has_body"] is True
    assert email["body_fetched_at"] is not None


def test_the_email_list_reports_the_new_fields(user_client, isolated_settings):
    t = make_thread(user_client)
    _attach_email(
        isolated_settings.db_path, t["id"],
        direction="inbound", ai_summary="Priya confirms Friday",
        ai_summary_model="test-model", conversation_id="google:5:tABC",
        to_recipients="me@acme.com",
    )

    [email] = user_client.get(f"/api/threads/{t['id']}/emails").json()

    assert email["direction"] == "inbound"
    assert email["ai_summary"] == "Priya confirms Friday"
    assert email["ai_summary_model"] == "test-model"
    assert email["conversation_id"] == "google:5:tABC"
    assert email["to_recipients"] == "me@acme.com"


def test_an_unhydratable_row_is_distinguishable_from_an_unfetched_one(
    user_client, isolated_settings
):
    """The two states the UI must not conflate: "not asked yet" offers a Load
    button, "asked and this account cannot" must not, because a retry that
    cannot succeed is a lie."""
    t = make_thread(user_client)
    _attach_email(isolated_settings.db_path, t["id"], message_id="<untried>")
    _attach_email(isolated_settings.db_path, t["id"], message_id="<tried>",
                  body_fetched_at="2026-08-27T10:00:00+00:00")

    emails = {e["message_id"]: e for e in
              user_client.get(f"/api/threads/{t['id']}/emails").json()}

    assert emails["<untried>"]["has_body"] is False
    assert emails["<untried>"]["body_fetched_at"] is None

    assert emails["<tried>"]["has_body"] is False
    assert emails["<tried>"]["body_fetched_at"] is not None


def test_a_direction_that_was_never_determined_stays_null(user_client, isolated_settings):
    t = make_thread(user_client)
    _attach_email(isolated_settings.db_path, t["id"])

    [email] = user_client.get(f"/api/threads/{t['id']}/emails").json()
    assert email["direction"] is None


def test_the_email_chains_route_groups_a_reply_thread(user_client, isolated_settings):
    t = make_thread(user_client)
    _attach_email(isolated_settings.db_path, t["id"], message_id="<a>",
                  subject="Atlas cutover", rfc_message_id="<a@x>",
                  date="2026-03-16T09:00:00+00:00", direction="outbound")
    _attach_email(isolated_settings.db_path, t["id"], message_id="<b>",
                  subject="Re: Atlas cutover", rfc_message_id="<b@x>",
                  in_reply_to="<a@x>", date="2026-03-17T09:00:00+00:00",
                  direction="inbound")
    _attach_email(isolated_settings.db_path, t["id"], message_id="<z>",
                  subject="Unrelated vendor invoice", sender="billing@vendor.example",
                  date="2026-03-01T09:00:00+00:00")

    chains = user_client.get(f"/api/threads/{t['id']}/email-chains").json()

    assert [c["message_count"] for c in chains] == [2, 1], "newest chain first"
    conversation = chains[0]
    assert conversation["subject"] == "Atlas cutover"
    # You wrote, she replied -- so the ball is in your court.
    assert conversation["last_message_from"] == "them"
    assert conversation["awaiting"] == "you"


def test_the_chains_route_respects_ownership(user_client, other_user_client):
    t = make_thread(user_client)
    assert other_user_client.get(
        f"/api/threads/{t['id']}/email-chains"
    ).status_code == 404


def test_the_body_route_returns_the_stored_text(user_client, isolated_settings):
    t = make_thread(user_client)
    email_id = _attach_email(isolated_settings.db_path, t["id"], body="the full text",
                             body_fetched_at="2026-08-27T10:00:00+00:00")

    body = user_client.get(f"/api/threads/{t['id']}/emails/{email_id}/body").json()

    assert body["body"] == "the full text"
    assert body["has_body"] is True


def test_the_body_route_is_404_for_another_thread(user_client, isolated_settings):
    """Not 403: a 403 would confirm the row exists."""
    t = make_thread(user_client)
    other = make_thread(user_client, title="Other")
    email_id = _attach_email(isolated_settings.db_path, t["id"], body="mine")

    assert user_client.get(
        f"/api/threads/{other['id']}/emails/{email_id}/body"
    ).status_code == 404


def test_the_body_route_respects_ownership(user_client, other_user_client, isolated_settings):
    t = make_thread(user_client)
    email_id = _attach_email(isolated_settings.db_path, t["id"], body="mine")

    assert other_user_client.get(
        f"/api/threads/{t['id']}/emails/{email_id}/body"
    ).status_code == 404


def test_hydrating_a_thread_with_no_provider_reports_unavailable(
    user_client, isolated_settings
):
    """No integration row at all, so nothing can supply a body -- and the route
    must say so rather than erroring."""
    t = make_thread(user_client)
    _attach_email(isolated_settings.db_path, t["id"], mcp_id="m1", integration_id=5)

    result = user_client.post(f"/api/threads/{t['id']}/emails/hydrate").json()

    assert result["requested"] == 1
    assert result["unavailable"] == 1
    # No LLM leg at all on this route any more.
    assert "summarised" not in result


def test_hydrating_an_empty_thread_is_a_no_op(user_client):
    t = make_thread(user_client)
    result = user_client.post(f"/api/threads/{t['id']}/emails/hydrate").json()
    assert result["requested"] == 0


def test_hydrate_respects_ownership(user_client, other_user_client):
    t = make_thread(user_client)
    assert other_user_client.post(
        f"/api/threads/{t['id']}/emails/hydrate"
    ).status_code == 404


def test_hydrating_an_email_not_on_this_thread_is_404(user_client, isolated_settings):
    t = make_thread(user_client)
    other = make_thread(user_client, title="Other")
    email_id = _attach_email(isolated_settings.db_path, t["id"])

    assert user_client.post(
        f"/api/threads/{other['id']}/emails/{email_id}/hydrate"
    ).status_code == 404


def test_hydrate_reports_what_is_still_pending(user_client, isolated_settings):
    """So the SPA can keep going rather than leaving a long thread half-filled
    at 12 bodies per page visit."""
    t = make_thread(user_client)
    for i in range(15):
        _attach_email(isolated_settings.db_path, t["id"], message_id=f"<m{i}>",
                      mcp_id=f"m{i}", integration_id=5)

    result = user_client.post(f"/api/threads/{t['id']}/emails/hydrate").json()

    from app.services import email_bodies as eb
    assert result["requested"] == eb.HYDRATE_MAX_PER_CALL
    # All 12 were stamped as unavailable (no provider), so they drop out of the
    # pending count -- what is left is the 3 nobody has asked about yet.
    assert result["remaining"] == 3


def test_summarise_is_a_separate_opt_in_route(user_client, isolated_settings):
    t = make_thread(user_client)
    _attach_email(isolated_settings.db_path, t["id"], body="word " * 400,
                  body_fetched_at="2026-08-27T10:00:00+00:00")

    result = user_client.post(f"/api/threads/{t['id']}/emails/summarise").json()

    # One row is eligible; the LLM is not wired in this test, so it fails and
    # stays eligible -- which is the retryable behaviour we want.
    assert result["requested"] == 1
    assert result["summarised"] + result["failed"] == 1


def test_summarise_skips_a_body_that_is_too_short(user_client, isolated_settings):
    t = make_thread(user_client)
    _attach_email(isolated_settings.db_path, t["id"], body="Short.",
                  body_fetched_at="2026-08-27T10:00:00+00:00")

    assert user_client.post(
        f"/api/threads/{t['id']}/emails/summarise"
    ).json()["requested"] == 0


def test_summarise_can_be_scoped_to_named_ids(user_client, isolated_settings):
    """How one conversation's button avoids paying for the whole thread."""
    t = make_thread(user_client)
    keep = _attach_email(isolated_settings.db_path, t["id"], message_id="<a>",
                         body="word " * 400, body_fetched_at="2026-08-27T10:00:00+00:00")
    _attach_email(isolated_settings.db_path, t["id"], message_id="<b>",
                  body="word " * 400, body_fetched_at="2026-08-27T10:00:00+00:00")

    result = user_client.post(
        f"/api/threads/{t['id']}/emails/summarise", json={"email_ids": [keep]}
    ).json()

    # One requested, not two: that is the scoping. `remaining` still counts both,
    # because the LLM is not wired here so even the named one stays eligible --
    # which is the retryable behaviour, not a scoping failure.
    assert result["requested"] == 1
    assert result["remaining"] == 2


def test_summarise_respects_ownership(user_client, other_user_client):
    t = make_thread(user_client)
    assert other_user_client.post(
        f"/api/threads/{t['id']}/emails/summarise"
    ).status_code == 404


def test_summarise_ignores_an_id_from_another_thread(user_client, isolated_settings):
    t = make_thread(user_client)
    other = make_thread(user_client, title="Other")
    foreign = _attach_email(isolated_settings.db_path, other["id"], body="word " * 400,
                            body_fetched_at="2026-08-27T10:00:00+00:00")

    result = user_client.post(
        f"/api/threads/{t['id']}/emails/summarise", json={"email_ids": [foreign]}
    ).json()

    assert result["requested"] == 0, "the thread_id filter is in the query"
