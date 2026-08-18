"""Meeting/insight types -- app/services/insight_types.py and
app/routers/insight_types.py. See test_db.py for the seeded-once-on-an-
empty-table behaviour of the two built-ins (general/interview).
"""

from __future__ import annotations

import pytest

from app.errors import NotFoundError, ValidationError
from app.services import insight_types as svc

TOPICS_PROMPT = "{{transcript}} {{previous_topics}}"
QUESTIONS_PROMPT = "{{transcript}} {{previous_items}}"


# --------------------------------------------------------------------------- #
# Service layer
# --------------------------------------------------------------------------- #


class TestSeeds:
    def test_the_two_built_ins_exist(self, conn):
        slugs = {row["slug"] for row in svc.list_types(conn)}
        assert {"general", "interview"} <= slugs

    def test_general_is_topics_shaped(self, conn):
        assert svc.get_type(conn, "general")["kind"] == "topics"

    def test_interview_is_questions_shaped(self, conn):
        assert svc.get_type(conn, "interview")["kind"] == "questions"


class TestGetType:
    def test_unknown_slug_is_not_found(self, conn):
        with pytest.raises(NotFoundError):
            svc.get_type(conn, "does-not-exist")


class TestCreateType:
    def test_slugifies_the_name(self, conn):
        row = svc.create_type(conn, name="Sales Call!", kind="topics", prompt=TOPICS_PROMPT)
        assert row["slug"] == "sales-call"

    def test_a_colliding_name_gets_a_numeric_suffix(self, conn):
        first = svc.create_type(conn, name="Standup", kind="topics", prompt=TOPICS_PROMPT)
        second = svc.create_type(conn, name="Standup", kind="topics", prompt=TOPICS_PROMPT)
        assert first["slug"] == "standup"
        assert second["slug"] == "standup-2"

    def test_rejects_an_unknown_kind(self, conn):
        with pytest.raises(ValidationError):
            svc.create_type(conn, name="Bad", kind="bogus", prompt=TOPICS_PROMPT)

    def test_rejects_a_blank_name(self, conn):
        with pytest.raises(ValidationError):
            svc.create_type(conn, name="   ", kind="topics", prompt=TOPICS_PROMPT)

    def test_rejects_a_topics_prompt_missing_previous_topics(self, conn):
        with pytest.raises(ValidationError):
            svc.create_type(conn, name="Bad", kind="topics", prompt="{{transcript}}")

    def test_rejects_a_questions_prompt_missing_previous_items(self, conn):
        with pytest.raises(ValidationError):
            svc.create_type(conn, name="Bad", kind="questions", prompt="{{transcript}}")

    def test_new_types_sort_after_existing_ones(self, conn):
        row = svc.create_type(conn, name="Extra", kind="topics", prompt=TOPICS_PROMPT)
        assert row["sort_order"] > svc.get_type(conn, "interview")["sort_order"]


class TestUpdateType:
    def test_updates_only_the_given_fields(self, conn):
        created = svc.create_type(conn, name="Kickoff", kind="topics", prompt=TOPICS_PROMPT)
        updated = svc.update_type(conn, created["slug"], name="Kickoff Call")
        assert updated["name"] == "Kickoff Call"
        assert updated["kind"] == "topics"
        assert updated["prompt"] == TOPICS_PROMPT

    def test_the_slug_never_changes_on_rename(self, conn):
        created = svc.create_type(conn, name="Kickoff", kind="topics", prompt=TOPICS_PROMPT)
        updated = svc.update_type(conn, created["slug"], name="Something Else Entirely")
        assert updated["slug"] == created["slug"]

    def test_changing_kind_re_validates_the_existing_prompt(self, conn):
        created = svc.create_type(conn, name="Kickoff", kind="topics", prompt=TOPICS_PROMPT)
        with pytest.raises(ValidationError):
            # Still the topics-shaped prompt -- missing {{previous_items}}.
            svc.update_type(conn, created["slug"], kind="questions")

    def test_unknown_slug_is_not_found(self, conn):
        with pytest.raises(NotFoundError):
            svc.update_type(conn, "nope", name="x")


class TestDeleteType:
    def test_deletes_the_row(self, conn):
        created = svc.create_type(conn, name="Throwaway", kind="topics", prompt=TOPICS_PROMPT)
        svc.delete_type(conn, created["slug"])
        with pytest.raises(NotFoundError):
            svc.get_type(conn, created["slug"])

    def test_unknown_slug_is_not_found(self, conn):
        with pytest.raises(NotFoundError):
            svc.delete_type(conn, "nope")


# --------------------------------------------------------------------------- #
# Router
# --------------------------------------------------------------------------- #


def test_any_user_can_list_the_public_shape(user_client):
    body = user_client.get("/api/insight-types").json()
    slugs = {row["slug"] for row in body}
    assert {"general", "interview"} <= slugs
    # Public shape only -- no prompt text handed to every signed-in user.
    assert "prompt" not in body[0]


def test_requires_login_to_list(client):
    assert client.get("/api/insight-types").status_code == 401


def test_only_admins_can_list_the_admin_shape(user_client, admin_client):
    assert user_client.get("/api/settings/insight-types").status_code == 403
    body = admin_client.get("/api/settings/insight-types").json()
    assert any(row["slug"] == "general" and "prompt" in row for row in body)


def test_only_admins_can_create(user_client, admin_client):
    payload = {"name": "Retro", "kind": "topics", "prompt": TOPICS_PROMPT}
    assert user_client.post("/api/settings/insight-types", json=payload).status_code == 403

    resp = admin_client.post("/api/settings/insight-types", json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["slug"] == "retro"


def test_create_surfaces_a_validation_error_as_400(admin_client):
    resp = admin_client.post(
        "/api/settings/insight-types",
        json={"name": "Bad", "kind": "topics", "prompt": "{{transcript}}"},
    )
    assert resp.status_code == 400
    assert "previous_topics" in resp.json()["error"]["message"]


def test_only_admins_can_update(user_client, admin_client):
    assert (
        user_client.put("/api/settings/insight-types/general", json={"name": "x"}).status_code
        == 403
    )
    resp = admin_client.put("/api/settings/insight-types/general", json={"name": "General"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "General"
    assert resp.json()["slug"] == "general"


def test_updating_an_unknown_slug_is_404(admin_client):
    resp = admin_client.put("/api/settings/insight-types/nope", json={"name": "x"})
    assert resp.status_code == 404


def test_only_admins_can_delete(user_client, admin_client):
    admin_client.post(
        "/api/settings/insight-types",
        json={"name": "Deletable", "kind": "topics", "prompt": TOPICS_PROMPT},
    )
    assert user_client.delete("/api/settings/insight-types/deletable").status_code == 403

    resp = admin_client.delete("/api/settings/insight-types/deletable")
    assert resp.status_code == 200, resp.text
    remaining = {row["slug"] for row in admin_client.get("/api/settings/insight-types").json()}
    assert "deletable" not in remaining


def test_deleting_an_unknown_slug_is_404(admin_client):
    resp = admin_client.delete("/api/settings/insight-types/nope")
    assert resp.status_code == 404
