"""Meeting/insight types -- app/services/insight_types.py and
app/routers/insight_types.py. See test_db.py for the seeded-once-on-an-
empty-table behaviour of the two built-ins (general/interview).
"""

from __future__ import annotations

import pytest

from app.errors import NotFoundError, ValidationError
from app.services import insight_types as svc

PROMPT = "{{transcript}} {{previous_topics}} {{previous_questions}} {{previous_action_items}}"


# --------------------------------------------------------------------------- #
# Service layer
# --------------------------------------------------------------------------- #


class TestSeeds:
    def test_the_two_built_ins_exist(self, conn):
        slugs = {row["slug"] for row in svc.list_types(conn)}
        assert {"general", "interview"} <= slugs


class TestGetType:
    def test_unknown_slug_is_not_found(self, conn):
        with pytest.raises(NotFoundError):
            svc.get_type(conn, "does-not-exist")


class TestCreateType:
    def test_slugifies_the_name(self, conn):
        row = svc.create_type(conn, name="Sales Call!", prompt=PROMPT)
        assert row["slug"] == "sales-call"

    def test_a_colliding_name_gets_a_numeric_suffix(self, conn):
        first = svc.create_type(conn, name="Standup", prompt=PROMPT)
        second = svc.create_type(conn, name="Standup", prompt=PROMPT)
        assert first["slug"] == "standup"
        assert second["slug"] == "standup-2"

    def test_rejects_a_blank_name(self, conn):
        with pytest.raises(ValidationError):
            svc.create_type(conn, name="   ", prompt=PROMPT)

    def test_rejects_a_prompt_missing_a_required_placeholder(self, conn):
        with pytest.raises(ValidationError):
            svc.create_type(conn, name="Bad", prompt="{{transcript}} {{previous_topics}}")

    def test_new_types_sort_after_existing_ones(self, conn):
        row = svc.create_type(conn, name="Extra", prompt=PROMPT)
        assert row["sort_order"] > svc.get_type(conn, "interview")["sort_order"]


class TestUpdateType:
    def test_updates_only_the_given_fields(self, conn):
        created = svc.create_type(conn, name="Kickoff", prompt=PROMPT)
        updated = svc.update_type(conn, created["slug"], name="Kickoff Call")
        assert updated["name"] == "Kickoff Call"
        assert updated["prompt"] == PROMPT

    def test_the_slug_never_changes_on_rename(self, conn):
        created = svc.create_type(conn, name="Kickoff", prompt=PROMPT)
        updated = svc.update_type(conn, created["slug"], name="Something Else Entirely")
        assert updated["slug"] == created["slug"]

    def test_updating_the_prompt_re_validates_it(self, conn):
        created = svc.create_type(conn, name="Kickoff", prompt=PROMPT)
        with pytest.raises(ValidationError):
            svc.update_type(conn, created["slug"], prompt="{{transcript}}")

    def test_unknown_slug_is_not_found(self, conn):
        with pytest.raises(NotFoundError):
            svc.update_type(conn, "nope", name="x")


class TestDeleteType:
    def test_deletes_the_row(self, conn):
        created = svc.create_type(conn, name="Throwaway", prompt=PROMPT)
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
    payload = {"name": "Retro", "prompt": PROMPT}
    assert user_client.post("/api/settings/insight-types", json=payload).status_code == 403

    resp = admin_client.post("/api/settings/insight-types", json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["slug"] == "retro"


def test_create_surfaces_a_validation_error_as_400(admin_client):
    resp = admin_client.post(
        "/api/settings/insight-types",
        json={"name": "Bad", "prompt": "{{transcript}}"},
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
        json={"name": "Deletable", "prompt": PROMPT},
    )
    assert user_client.delete("/api/settings/insight-types/deletable").status_code == 403

    resp = admin_client.delete("/api/settings/insight-types/deletable")
    assert resp.status_code == 200, resp.text
    remaining = {row["slug"] for row in admin_client.get("/api/settings/insight-types").json()}
    assert "deletable" not in remaining


def test_deleting_an_unknown_slug_is_404(admin_client):
    resp = admin_client.delete("/api/settings/insight-types/nope")
    assert resp.status_code == 404
