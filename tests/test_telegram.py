"""Telegram: sending, the getUpdates/getMe transport, the settings Test
button, per-user pairing, and the notify_* entry points the sweep, next-step
generation and transcription call.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.db import get_conn
from app.services import telegram as telegram_svc

BOT_TOKEN = "123456:ABC-DEF"
SEND_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
GETUPDATES_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
GETME_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"


def telegram_response(ok: bool, description: str | None = None) -> dict:
    body: dict = {"ok": ok}
    if description is not None:
        body["description"] = description
    return body


def configure(admin_client, **values):
    resp = admin_client.put("/api/settings", json={"values": values})
    assert resp.status_code == 200, resp.text


def _link(db_path, user_id: int, chat_id: str, **prefs) -> None:
    """Directly wires up a user's Telegram link -- the same writes
    consume_link_code/link_chat would make, without going through a real
    pairing exchange, for tests that only care about what happens *after*
    linking."""
    defaults = {
        "notify_new_attachments": False,
        "notify_next_steps": False,
        "notify_transcript_ready": False,
        "notify_transcript_failed": False,
    }
    defaults.update(prefs)
    with get_conn(db_path) as conn:
        telegram_svc.link_chat(conn, user_id, chat_id)
        telegram_svc.set_notify_preferences(conn, user_id, **defaults)


def _user_id(client) -> int:
    return client.get("/api/auth/me").json()["id"]


# --------------------------------------------------------------------------- #
# send_message
# --------------------------------------------------------------------------- #


class TestSendMessage:
    @respx.mock
    def test_success(self):
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        telegram_svc.send_message(BOT_TOKEN, "-1001", "hello")
        assert route.called

    @respx.mock
    def test_telegram_rejection_is_raised_with_the_description(self):
        respx.post(SEND_URL).mock(
            return_value=httpx.Response(400, json=telegram_response(False, "chat not found"))
        )
        with pytest.raises(telegram_svc.TelegramError, match="chat not found"):
            telegram_svc.send_message(BOT_TOKEN, "-1001", "hello")

    @respx.mock
    def test_unreachable_service_is_a_telegram_error(self):
        respx.post(SEND_URL).mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(telegram_svc.TelegramError):
            telegram_svc.send_message(BOT_TOKEN, "-1001", "hello")


# --------------------------------------------------------------------------- #
# get_updates / get_me
# --------------------------------------------------------------------------- #


class TestGetUpdates:
    @respx.mock
    def test_returns_the_result_list(self):
        respx.get(GETUPDATES_URL).mock(
            return_value=httpx.Response(200, json={"ok": True, "result": [{"update_id": 1}]})
        )
        assert telegram_svc.get_updates(BOT_TOKEN, offset=0, timeout=25) == [{"update_id": 1}]

    @respx.mock
    def test_sends_the_offset_and_timeout(self):
        route = respx.get(GETUPDATES_URL).mock(
            return_value=httpx.Response(200, json={"ok": True, "result": []})
        )
        telegram_svc.get_updates(BOT_TOKEN, offset=42, timeout=25)
        params = dict(route.calls[0].request.url.params)
        assert params["offset"] == "42"
        assert params["timeout"] == "25"

    @respx.mock
    def test_a_rejection_is_a_telegram_error(self):
        respx.get(GETUPDATES_URL).mock(
            return_value=httpx.Response(401, json=telegram_response(False, "Unauthorized"))
        )
        with pytest.raises(telegram_svc.TelegramError, match="Unauthorized"):
            telegram_svc.get_updates(BOT_TOKEN, offset=0, timeout=25)


class TestGetMe:
    @respx.mock
    def test_returns_the_bot_info(self):
        respx.get(GETME_URL).mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"username": "mmn_bot"}})
        )
        assert telegram_svc.get_me(BOT_TOKEN) == {"username": "mmn_bot"}

    @respx.mock
    def test_a_bad_token_is_a_telegram_error(self):
        respx.get(GETME_URL).mock(
            return_value=httpx.Response(401, json=telegram_response(False, "Unauthorized"))
        )
        with pytest.raises(telegram_svc.TelegramError):
            telegram_svc.get_me(BOT_TOKEN)


# --------------------------------------------------------------------------- #
# test_connection
# --------------------------------------------------------------------------- #


class TestConnection:
    def test_no_bot_token_is_reported_not_raised(self):
        result = telegram_svc.test_connection(None, "-1001")
        assert result["ok"] is False
        assert "bot token" in result["error"]

    @respx.mock
    def test_with_a_recipient_sends_a_real_message(self):
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        result = telegram_svc.test_connection(BOT_TOKEN, "-1001")
        assert result["ok"] is True
        assert route.called
        assert "linked chat" in result["response"]

    @respx.mock
    def test_without_a_recipient_falls_back_to_a_getme_probe(self):
        """No admin has linked their own Telegram yet -- there's nowhere real
        to send to, so this must not invent a chat id."""
        send_route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        getme_route = respx.get(GETME_URL).mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"username": "mmn_bot"}})
        )
        result = telegram_svc.test_connection(BOT_TOKEN, None)
        assert result["ok"] is True
        assert not send_route.called
        assert getme_route.called
        assert "mmn_bot" in result["response"]

    @respx.mock
    def test_a_bad_token_probe_is_reported_not_raised(self):
        respx.get(GETME_URL).mock(
            return_value=httpx.Response(401, json=telegram_response(False, "Unauthorized"))
        )
        result = telegram_svc.test_connection(BOT_TOKEN, None)
        assert result["ok"] is False


# --------------------------------------------------------------------------- #
# Pairing: create_link_code / consume_link_code / link_chat / unlink_chat
# --------------------------------------------------------------------------- #


class TestPairing:
    def test_consume_link_code_resolves_the_owning_user(self, conn):
        conn.execute(
            "INSERT INTO users (id, username, password_hash, password_salt, created_at, updated_at) "
            "VALUES (1, 'u', 'h', 's', '2026-01-01', '2026-01-01')"
        )
        code, _expires_at = telegram_svc.create_link_code(conn, 1)
        assert telegram_svc.consume_link_code(conn, code) == 1

    def test_consume_link_code_is_single_use(self, conn):
        conn.execute(
            "INSERT INTO users (id, username, password_hash, password_salt, created_at, updated_at) "
            "VALUES (1, 'u', 'h', 's', '2026-01-01', '2026-01-01')"
        )
        code, _ = telegram_svc.create_link_code(conn, 1)
        assert telegram_svc.consume_link_code(conn, code) == 1
        # Second attempt at the same code: already deleted, resolves to nothing.
        assert telegram_svc.consume_link_code(conn, code) is None

    def test_an_unknown_code_resolves_to_nothing(self, conn):
        assert telegram_svc.consume_link_code(conn, "NOSUCHCODE") is None

    def test_an_expired_code_resolves_to_nothing(self, conn):
        conn.execute(
            "INSERT INTO users (id, username, password_hash, password_salt, created_at, updated_at) "
            "VALUES (1, 'u', 'h', 's', '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO telegram_link_codes (code, user_id, created_at, expires_at) "
            "VALUES ('STALE001', 1, '2020-01-01T00:00:00+00:00', '2020-01-01T00:10:00+00:00')"
        )
        assert telegram_svc.consume_link_code(conn, "STALE001") is None

    def test_generating_a_new_code_replaces_the_pending_one(self, conn):
        conn.execute(
            "INSERT INTO users (id, username, password_hash, password_salt, created_at, updated_at) "
            "VALUES (1, 'u', 'h', 's', '2026-01-01', '2026-01-01')"
        )
        first, _ = telegram_svc.create_link_code(conn, 1)
        second, _ = telegram_svc.create_link_code(conn, 1)
        assert telegram_svc.consume_link_code(conn, first) is None
        assert telegram_svc.consume_link_code(conn, second) == 1

    def test_a_code_only_ever_resolves_to_the_user_it_was_generated_for(self, conn):
        conn.execute(
            "INSERT INTO users (id, username, password_hash, password_salt, created_at, updated_at) "
            "VALUES (1, 'alice', 'h', 's', '2026-01-01', '2026-01-01'), "
            "(2, 'bob', 'h', 's', '2026-01-01', '2026-01-01')"
        )
        code, _ = telegram_svc.create_link_code(conn, 1)
        # Whoever sends it -- there's no "as bob" concept here, since consuming
        # a code has no notion of who's asking, only the code's own value --
        # it can only ever resolve to the user_id it was generated for.
        assert telegram_svc.consume_link_code(conn, code) == 1


class TestLinkAndUnlink:
    def _seed_user(self, conn, user_id: int = 1):
        conn.execute(
            "INSERT INTO users (id, username, password_hash, password_salt, created_at, updated_at) "
            "VALUES (?, 'u', 'h', 's', '2026-01-01', '2026-01-01')",
            (user_id,),
        )

    def test_link_chat_sets_chat_id_and_timestamp(self, conn):
        self._seed_user(conn)
        telegram_svc.link_chat(conn, 1, "555")
        status = telegram_svc.get_link_status(conn, 1)
        assert status["linked"] is True
        assert status["linked_at"] is not None

    def test_unlink_chat_clears_the_link(self, conn):
        self._seed_user(conn)
        telegram_svc.link_chat(conn, 1, "555")
        telegram_svc.unlink_chat(conn, 1)
        status = telegram_svc.get_link_status(conn, 1)
        assert status["linked"] is False
        assert status["linked_at"] is None

    def test_find_user_by_chat_id(self, conn):
        self._seed_user(conn)
        telegram_svc.link_chat(conn, 1, "555")
        row = telegram_svc.find_user_by_chat_id(conn, "555")
        assert row["id"] == 1
        assert telegram_svc.find_user_by_chat_id(conn, "no-such-chat") is None

    def test_get_link_status_reports_a_pending_code(self, conn):
        self._seed_user(conn)
        code, expires_at = telegram_svc.create_link_code(conn, 1)
        status = telegram_svc.get_link_status(conn, 1)
        assert status["linked"] is False
        assert status["pending_code"] == code
        assert status["pending_code_expires_at"] == expires_at

    def test_set_notify_preferences_round_trips(self, conn):
        self._seed_user(conn)
        telegram_svc.set_notify_preferences(
            conn, 1,
            notify_new_attachments=True, notify_next_steps=False,
            notify_transcript_ready=True, notify_transcript_failed=False,
        )
        status = telegram_svc.get_link_status(conn, 1)
        assert status["notify_new_attachments"] is True
        assert status["notify_next_steps"] is False
        assert status["notify_transcript_ready"] is True
        assert status["notify_transcript_failed"] is False


# --------------------------------------------------------------------------- #
# notify_new_attachments / notify_next_step / notify_transcript_*
# --------------------------------------------------------------------------- #


class TestNotifyNewAttachments:
    @respx.mock
    def test_master_switch_off_sends_nothing(self, admin_client, user_client, db_path):
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        configure(admin_client, telegram_enabled=False, telegram_bot_token=BOT_TOKEN)
        thread = user_client.post("/api/threads", json={"title": "Atlas Migration"}).json()
        _link(db_path, _user_id(user_client), "-1001", notify_new_attachments=True)

        telegram_svc.notify_new_attachments(
            lambda: get_conn(db_path), thread_id=thread["id"], thread_title="Atlas Migration",
            events=[{"summary": "Cutover review"}], emails=[],
        )
        assert not route.called

    @respx.mock
    def test_not_linked_sends_nothing(self, admin_client, user_client, db_path):
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        configure(admin_client, telegram_enabled=True, telegram_bot_token=BOT_TOKEN)
        thread = user_client.post("/api/threads", json={"title": "Atlas Migration"}).json()

        telegram_svc.notify_new_attachments(
            lambda: get_conn(db_path), thread_id=thread["id"], thread_title="Atlas Migration",
            events=[{"summary": "Cutover review"}], emails=[],
        )
        assert not route.called

    @respx.mock
    def test_the_owners_own_notify_toggle_gates_independently(self, admin_client, user_client, db_path):
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        configure(admin_client, telegram_enabled=True, telegram_bot_token=BOT_TOKEN)
        thread = user_client.post("/api/threads", json={"title": "Atlas Migration"}).json()
        _link(db_path, _user_id(user_client), "-1001", notify_new_attachments=False)

        telegram_svc.notify_new_attachments(
            lambda: get_conn(db_path), thread_id=thread["id"], thread_title="Atlas Migration",
            events=[{"summary": "Cutover review"}], emails=[],
        )
        assert not route.called

    @respx.mock
    def test_enabled_and_linked_sends_to_the_owners_own_chat(self, admin_client, user_client, db_path):
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        configure(admin_client, telegram_enabled=True, telegram_bot_token=BOT_TOKEN, public_base_url="https://mmn.test")
        thread = user_client.post("/api/threads", json={"title": "Atlas Migration"}).json()
        _link(db_path, _user_id(user_client), "-1001", notify_new_attachments=True)

        telegram_svc.notify_new_attachments(
            lambda: get_conn(db_path), thread_id=thread["id"], thread_title="Atlas Migration",
            events=[{"summary": "Cutover review"}], emails=[{"subject": "Re: cutover window"}],
        )
        assert route.called
        body = json.loads(route.calls[0].request.content)
        assert body["chat_id"] == "-1001"
        assert "Atlas Migration" in body["text"]
        assert "Cutover review" in body["text"]
        assert "Re: cutover window" in body["text"]
        assert f"https://mmn.test/threads/{thread['id']}" in body["text"]

    @respx.mock
    def test_a_second_users_thread_never_notifies_the_first_user(self, admin_client, user_client, other_user_client, db_path):
        """Notifications must not cross accounts -- bob's thread must never
        reach alice's linked chat even though both have Telegram linked."""
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        configure(admin_client, telegram_enabled=True, telegram_bot_token=BOT_TOKEN)
        _link(db_path, _user_id(user_client), "-1001", notify_new_attachments=True)
        _link(db_path, _user_id(other_user_client), "-2002", notify_new_attachments=True)
        thread = other_user_client.post("/api/threads", json={"title": "Bob's thread"}).json()

        telegram_svc.notify_new_attachments(
            lambda: get_conn(db_path), thread_id=thread["id"], thread_title="Bob's thread",
            events=[{"summary": "x"}], emails=[],
        )
        assert route.called
        assert json.loads(route.calls[0].request.content)["chat_id"] == "-2002"

    @respx.mock
    def test_a_send_failure_does_not_raise(self, admin_client, user_client, db_path):
        respx.post(SEND_URL).mock(return_value=httpx.Response(400, json=telegram_response(False, "blocked")))
        configure(admin_client, telegram_enabled=True, telegram_bot_token=BOT_TOKEN)
        thread = user_client.post("/api/threads", json={"title": "Atlas Migration"}).json()
        _link(db_path, _user_id(user_client), "-1001", notify_new_attachments=True)

        # Must not raise -- a sweep must not be broken by a Telegram outage.
        telegram_svc.notify_new_attachments(
            lambda: get_conn(db_path), thread_id=thread["id"], thread_title="Atlas Migration",
            events=[{"summary": "x"}], emails=[],
        )

    @respx.mock
    def test_no_bot_token_configured_is_a_silent_no_op(self, admin_client, user_client, db_path):
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        configure(admin_client, telegram_enabled=True)
        thread = user_client.post("/api/threads", json={"title": "Atlas Migration"}).json()
        _link(db_path, _user_id(user_client), "-1001", notify_new_attachments=True)

        telegram_svc.notify_new_attachments(
            lambda: get_conn(db_path), thread_id=thread["id"], thread_title="Atlas Migration",
            events=[{"summary": "x"}], emails=[],
        )
        assert not route.called


class TestNotifyNextStep:
    @respx.mock
    def test_disabled_sends_nothing(self, admin_client, user_client, db_path):
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        configure(admin_client, telegram_enabled=True, telegram_bot_token=BOT_TOKEN)
        thread = user_client.post("/api/threads", json={"title": "Atlas Migration"}).json()
        _link(db_path, _user_id(user_client), "-1001", notify_next_steps=False)

        telegram_svc.notify_next_step(
            db_path, thread_id=thread["id"], thread_title="Atlas Migration", next_step="Send the recap."
        )
        assert not route.called

    @respx.mock
    def test_enabled_sends_the_next_step_text(self, admin_client, user_client, db_path):
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        configure(admin_client, telegram_enabled=True, telegram_bot_token=BOT_TOKEN)
        thread = user_client.post("/api/threads", json={"title": "Atlas Migration"}).json()
        _link(db_path, _user_id(user_client), "-1001", notify_next_steps=True)

        telegram_svc.notify_next_step(
            db_path, thread_id=thread["id"], thread_title="Atlas Migration", next_step="Send the recap."
        )
        assert route.called
        text = json.loads(route.calls[0].request.content)["text"]
        assert "Atlas Migration" in text
        assert "Send the recap." in text

    @respx.mock
    def test_a_very_long_next_step_is_truncated(self, admin_client, user_client, db_path):
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        configure(admin_client, telegram_enabled=True, telegram_bot_token=BOT_TOKEN)
        thread = user_client.post("/api/threads", json={"title": "Atlas Migration"}).json()
        _link(db_path, _user_id(user_client), "-1001", notify_next_steps=True)

        telegram_svc.notify_next_step(
            db_path, thread_id=thread["id"], thread_title="Atlas Migration", next_step="x" * 5000
        )
        text = json.loads(route.calls[0].request.content)["text"]
        assert len(text) < 5000


@pytest.fixture
def meeting(user_client):
    return user_client.post(
        "/api/meetings",
        json={
            "new_thread_title": "Atlas Migration",
            "title": "Cutover go/no-go",
        },
    ).json()


class TestNotifyTranscriptReady:
    @respx.mock
    def test_disabled_sends_nothing(self, admin_client, user_client, db_path, meeting):
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        configure(admin_client, telegram_enabled=True, telegram_bot_token=BOT_TOKEN)
        _link(db_path, _user_id(user_client), "-1001", notify_transcript_ready=False)

        telegram_svc.notify_transcript_ready(db_path, meeting_id=meeting["id"])
        assert not route.called

    @respx.mock
    def test_enabled_sends_a_message_naming_the_meeting(self, admin_client, user_client, db_path, meeting):
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        configure(admin_client, telegram_enabled=True, telegram_bot_token=BOT_TOKEN, public_base_url="https://mmn.test")
        _link(db_path, _user_id(user_client), "-1001", notify_transcript_ready=True)

        telegram_svc.notify_transcript_ready(db_path, meeting_id=meeting["id"])

        assert route.called
        text = json.loads(route.calls[0].request.content)["text"]
        assert "Cutover go/no-go" in text
        assert f"https://mmn.test/meetings/{meeting['id']}" in text

    @respx.mock
    def test_a_send_failure_does_not_raise(self, admin_client, user_client, db_path, meeting):
        respx.post(SEND_URL).mock(return_value=httpx.Response(400, json=telegram_response(False, "blocked")))
        configure(admin_client, telegram_enabled=True, telegram_bot_token=BOT_TOKEN)
        _link(db_path, _user_id(user_client), "-1001", notify_transcript_ready=True)

        telegram_svc.notify_transcript_ready(db_path, meeting_id=meeting["id"])


class TestNotifyTranscriptFailed:
    @respx.mock
    def test_disabled_sends_nothing(self, admin_client, user_client, db_path, meeting):
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        configure(admin_client, telegram_enabled=True, telegram_bot_token=BOT_TOKEN)
        _link(db_path, _user_id(user_client), "-1001", notify_transcript_failed=False)

        telegram_svc.notify_transcript_failed(db_path, meeting_id=meeting["id"], error="boom")
        assert not route.called

    @respx.mock
    def test_enabled_sends_the_error(self, admin_client, user_client, db_path, meeting):
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        configure(admin_client, telegram_enabled=True, telegram_bot_token=BOT_TOKEN)
        _link(db_path, _user_id(user_client), "-1001", notify_transcript_failed=True)

        telegram_svc.notify_transcript_failed(
            db_path, meeting_id=meeting["id"], error="Diarization service unreachable"
        )

        assert route.called
        text = json.loads(route.calls[0].request.content)["text"]
        assert "Cutover go/no-go" in text
        assert "Diarization service unreachable" in text

    @respx.mock
    def test_a_very_long_error_is_truncated(self, admin_client, user_client, db_path, meeting):
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        configure(admin_client, telegram_enabled=True, telegram_bot_token=BOT_TOKEN)
        _link(db_path, _user_id(user_client), "-1001", notify_transcript_failed=True)

        telegram_svc.notify_transcript_failed(db_path, meeting_id=meeting["id"], error="x" * 5000)

        text = json.loads(route.calls[0].request.content)["text"]
        assert len(text) < 5000


# --------------------------------------------------------------------------- #
# POST /api/telegram/test
# --------------------------------------------------------------------------- #


@respx.mock
def test_telegram_test_endpoint_falls_back_to_getme_when_admin_is_not_linked(admin_client):
    route = respx.get(GETME_URL).mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"username": "mmn_bot"}})
    )
    configure(admin_client, telegram_bot_token=BOT_TOKEN)

    resp = admin_client.post("/api/telegram/test")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert route.called


@respx.mock
def test_telegram_test_endpoint_sends_to_the_admins_own_linked_chat(admin_client, db_path):
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
    configure(admin_client, telegram_bot_token=BOT_TOKEN)
    _link(db_path, _user_id(admin_client), "-9001")

    resp = admin_client.post("/api/telegram/test")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert json.loads(route.calls[0].request.content)["chat_id"] == "-9001"


@respx.mock
def test_telegram_test_endpoint_can_try_an_unsaved_token(admin_client):
    other_token = "999:XYZ"
    other_url = f"https://api.telegram.org/bot{other_token}/getMe"
    route = respx.get(other_url).mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"username": "other_bot"}})
    )

    resp = admin_client.post("/api/telegram/test", json={"bot_token": other_token})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert route.called


@respx.mock
def test_telegram_test_endpoint_ignores_a_masked_token_echo(admin_client):
    """The form round-trips the masked placeholder; that must not become the
    literal bot token used to send."""
    route = respx.get(GETME_URL).mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"username": "mmn_bot"}})
    )
    configure(admin_client, telegram_bot_token=BOT_TOKEN)

    admin_client.post("/api/telegram/test", json={"bot_token": "••••ABC-DEF"})
    assert route.called, "the saved token must still be used"


def test_telegram_test_endpoint_is_admin_only(user_client):
    assert user_client.post("/api/telegram/test").status_code == 403


# --------------------------------------------------------------------------- #
# Self-service /api/auth/me/telegram*
# --------------------------------------------------------------------------- #


def test_get_my_telegram_when_never_linked(user_client):
    body = user_client.get("/api/auth/me/telegram").json()
    assert body["linked"] is False
    assert body["pending_code"] is None


def test_generating_a_link_code_shows_up_on_status(user_client):
    created = user_client.post("/api/auth/me/telegram/link-code").json()
    assert created["code"]
    status = user_client.get("/api/auth/me/telegram").json()
    assert status["pending_code"] == created["code"]


def test_unlinking_clears_the_status(user_client, db_path):
    _link(db_path, _user_id(user_client), "-1001")
    assert user_client.get("/api/auth/me/telegram").json()["linked"] is True

    resp = user_client.delete("/api/auth/me/telegram")
    assert resp.status_code == 200
    assert user_client.get("/api/auth/me/telegram").json()["linked"] is False


def test_updating_preferences(user_client, db_path):
    _link(db_path, _user_id(user_client), "-1001")
    resp = user_client.put(
        "/api/auth/me/telegram/preferences",
        json={
            "notify_new_attachments": True,
            "notify_next_steps": True,
            "notify_transcript_ready": False,
            "notify_transcript_failed": False,
        },
    )
    assert resp.status_code == 200
    status = user_client.get("/api/auth/me/telegram").json()
    assert status["notify_new_attachments"] is True
    assert status["notify_next_steps"] is True
    assert status["notify_transcript_ready"] is False


def test_telegram_self_service_endpoints_are_isolated_per_user(user_client, other_user_client):
    """One user's link code and status must never be visible or editable by
    another user hitting the same self-service endpoint."""
    created = user_client.post("/api/auth/me/telegram/link-code").json()
    bob_status = other_user_client.get("/api/auth/me/telegram").json()
    assert bob_status["pending_code"] != created["code"]
    assert bob_status["linked"] is False
