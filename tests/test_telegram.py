"""The Telegram notifier: sending, the settings Test button, and the
notify_* entry points the sweep, next-step generation and transcription call.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.db import get_conn
from app.services import telegram as telegram_svc

BOT_TOKEN = "123456:ABC-DEF"
SEND_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"


def telegram_response(ok: bool, description: str | None = None) -> dict:
    body: dict = {"ok": ok}
    if description is not None:
        body["description"] = description
    return body


# --------------------------------------------------------------------------- #
# parse_chat_ids
# --------------------------------------------------------------------------- #


def test_parse_chat_ids_splits_strips_and_drops_empties():
    assert telegram_svc.parse_chat_ids(" -1001,  @chan ,,") == ["-1001", "@chan"]


def test_parse_chat_ids_handles_none_and_empty():
    assert telegram_svc.parse_chat_ids(None) == []
    assert telegram_svc.parse_chat_ids("") == []


# --------------------------------------------------------------------------- #
# send_message / send_to_all
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


class TestSendToAll:
    @respx.mock
    def test_all_succeed(self):
        respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        result = telegram_svc.send_to_all(BOT_TOKEN, ["-1001", "-1002"], "hi")
        assert result == {"ok": True, "sent": 2, "errors": []}

    @respx.mock
    def test_one_recipient_failing_does_not_stop_the_others(self):
        calls = []

        def side_effect(request: httpx.Request) -> httpx.Response:
            import json as _json

            body = _json.loads(request.content)
            calls.append(body["chat_id"])
            if body["chat_id"] == "-1002":
                return httpx.Response(400, json=telegram_response(False, "blocked"))
            return httpx.Response(200, json=telegram_response(True))

        respx.post(SEND_URL).mock(side_effect=side_effect)
        result = telegram_svc.send_to_all(BOT_TOKEN, ["-1001", "-1002"], "hi")

        assert result["ok"] is False
        assert result["sent"] == 1
        assert result["errors"] == [{"chat_id": "-1002", "error": "Telegram rejected the message to -1002: blocked"}]
        assert calls == ["-1001", "-1002"], "the first failure must not stop the second send"


class TestConnection:
    def test_no_bot_token_is_reported_not_raised(self):
        result = telegram_svc.test_connection(None, ["-1001"])
        assert result["ok"] is False
        assert "bot token" in result["error"]

    def test_no_chat_ids_is_reported_not_raised(self):
        result = telegram_svc.test_connection(BOT_TOKEN, [])
        assert result["ok"] is False
        assert "chat" in result["error"]

    @respx.mock
    def test_success_reports_latency_and_recipient_count(self):
        respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        result = telegram_svc.test_connection(BOT_TOKEN, ["-1001", "-1002"])
        assert result["ok"] is True
        assert result["error"] is None
        assert result["latency_ms"] >= 0
        assert "2 recipient" in result["response"]

    @respx.mock
    def test_this_is_a_real_send_not_a_cheap_probe(self):
        """Unlike the LLM/diarization tests, a getMe-only probe wouldn't confirm
        the thing this feature promises: that a message actually arrives."""
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        telegram_svc.test_connection(BOT_TOKEN, ["-1001"])
        assert route.called


# --------------------------------------------------------------------------- #
# notify_new_attachments / notify_next_step
# --------------------------------------------------------------------------- #


def configure(admin_client, **values):
    resp = admin_client.put("/api/settings", json={"values": values})
    assert resp.status_code == 200, resp.text


class TestNotifyNewAttachments:
    @respx.mock
    def test_disabled_sends_nothing(self, admin_client, db_path):
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        configure(
            admin_client,
            telegram_enabled=False,
            telegram_bot_token=BOT_TOKEN,
            telegram_chat_ids="-1001",
            telegram_notify_new_attachments=True,
        )
        telegram_svc.notify_new_attachments(
            lambda: get_conn(db_path), thread_id=1, thread_title="Atlas Migration",
            events=[{"summary": "Cutover review"}], emails=[],
        )
        assert not route.called

    @respx.mock
    def test_the_notify_toggle_gates_independently_of_the_master_switch(self, admin_client, db_path):
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        configure(
            admin_client,
            telegram_enabled=True,
            telegram_bot_token=BOT_TOKEN,
            telegram_chat_ids="-1001",
            telegram_notify_new_attachments=False,
        )
        telegram_svc.notify_new_attachments(
            lambda: get_conn(db_path), thread_id=1, thread_title="Atlas Migration",
            events=[{"summary": "Cutover review"}], emails=[],
        )
        assert not route.called

    @respx.mock
    def test_enabled_sends_a_message_naming_the_thread_and_items(self, admin_client, db_path):
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        configure(
            admin_client,
            telegram_enabled=True,
            telegram_bot_token=BOT_TOKEN,
            telegram_chat_ids="-1001",
            telegram_notify_new_attachments=True,
            public_base_url="https://mmn.test",
        )
        telegram_svc.notify_new_attachments(
            lambda: get_conn(db_path), thread_id=42, thread_title="Atlas Migration",
            events=[{"summary": "Cutover review"}], emails=[{"subject": "Re: cutover window"}],
        )
        assert route.called
        import json

        text = json.loads(route.calls[0].request.content)["text"]
        assert "Atlas Migration" in text
        assert "Cutover review" in text
        assert "Re: cutover window" in text
        assert "https://mmn.test/threads/42" in text

    @respx.mock
    def test_a_send_failure_does_not_raise(self, admin_client, db_path):
        respx.post(SEND_URL).mock(return_value=httpx.Response(400, json=telegram_response(False, "blocked")))
        configure(
            admin_client,
            telegram_enabled=True,
            telegram_bot_token=BOT_TOKEN,
            telegram_chat_ids="-1001",
            telegram_notify_new_attachments=True,
        )
        # Must not raise -- a sweep must not be broken by a Telegram outage.
        telegram_svc.notify_new_attachments(
            lambda: get_conn(db_path), thread_id=1, thread_title="Atlas Migration",
            events=[{"summary": "x"}], emails=[],
        )

    @respx.mock
    def test_no_bot_token_configured_is_a_silent_no_op(self, admin_client, db_path):
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        configure(
            admin_client,
            telegram_enabled=True,
            telegram_chat_ids="-1001",
            telegram_notify_new_attachments=True,
        )
        telegram_svc.notify_new_attachments(
            lambda: get_conn(db_path), thread_id=1, thread_title="Atlas Migration",
            events=[{"summary": "x"}], emails=[],
        )
        assert not route.called


class TestNotifyNextStep:
    @respx.mock
    def test_disabled_sends_nothing(self, admin_client, db_path):
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        configure(
            admin_client,
            telegram_enabled=True,
            telegram_bot_token=BOT_TOKEN,
            telegram_chat_ids="-1001",
            telegram_notify_next_steps=False,
        )
        telegram_svc.notify_next_step(
            db_path, thread_id=7, thread_title="Atlas Migration", next_step="Send the recap."
        )
        assert not route.called

    @respx.mock
    def test_enabled_sends_the_next_step_text(self, admin_client, db_path):
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        configure(
            admin_client,
            telegram_enabled=True,
            telegram_bot_token=BOT_TOKEN,
            telegram_chat_ids="-1001",
            telegram_notify_next_steps=True,
        )
        telegram_svc.notify_next_step(
            db_path, thread_id=7, thread_title="Atlas Migration", next_step="Send the recap."
        )
        assert route.called
        import json

        text = json.loads(route.calls[0].request.content)["text"]
        assert "Atlas Migration" in text
        assert "Send the recap." in text

    @respx.mock
    def test_a_very_long_next_step_is_truncated(self, admin_client, db_path):
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        configure(
            admin_client,
            telegram_enabled=True,
            telegram_bot_token=BOT_TOKEN,
            telegram_chat_ids="-1001",
            telegram_notify_next_steps=True,
        )
        telegram_svc.notify_next_step(
            db_path, thread_id=7, thread_title="Atlas Migration", next_step="x" * 5000
        )
        import json

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
    def test_disabled_sends_nothing(self, admin_client, db_path, meeting):
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        configure(
            admin_client,
            telegram_enabled=True,
            telegram_bot_token=BOT_TOKEN,
            telegram_chat_ids="-1001",
            telegram_notify_transcript_ready=False,
        )
        telegram_svc.notify_transcript_ready(db_path, meeting_id=meeting["id"])
        assert not route.called

    @respx.mock
    def test_enabled_sends_a_message_naming_the_meeting(self, admin_client, db_path, meeting):
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        configure(
            admin_client,
            telegram_enabled=True,
            telegram_bot_token=BOT_TOKEN,
            telegram_chat_ids="-1001",
            telegram_notify_transcript_ready=True,
            public_base_url="https://mmn.test",
        )
        telegram_svc.notify_transcript_ready(db_path, meeting_id=meeting["id"])

        assert route.called
        import json

        text = json.loads(route.calls[0].request.content)["text"]
        assert "Cutover go/no-go" in text
        assert f"https://mmn.test/meetings/{meeting['id']}" in text

    @respx.mock
    def test_a_send_failure_does_not_raise(self, admin_client, db_path, meeting):
        respx.post(SEND_URL).mock(return_value=httpx.Response(400, json=telegram_response(False, "blocked")))
        configure(
            admin_client,
            telegram_enabled=True,
            telegram_bot_token=BOT_TOKEN,
            telegram_chat_ids="-1001",
            telegram_notify_transcript_ready=True,
        )
        telegram_svc.notify_transcript_ready(db_path, meeting_id=meeting["id"])


class TestNotifyTranscriptFailed:
    @respx.mock
    def test_disabled_sends_nothing(self, admin_client, db_path, meeting):
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        configure(
            admin_client,
            telegram_enabled=True,
            telegram_bot_token=BOT_TOKEN,
            telegram_chat_ids="-1001",
            telegram_notify_transcript_failed=False,
        )
        telegram_svc.notify_transcript_failed(db_path, meeting_id=meeting["id"], error="boom")
        assert not route.called

    @respx.mock
    def test_enabled_sends_the_error(self, admin_client, db_path, meeting):
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        configure(
            admin_client,
            telegram_enabled=True,
            telegram_bot_token=BOT_TOKEN,
            telegram_chat_ids="-1001",
            telegram_notify_transcript_failed=True,
        )
        telegram_svc.notify_transcript_failed(
            db_path, meeting_id=meeting["id"], error="Diarization service unreachable"
        )

        assert route.called
        import json

        text = json.loads(route.calls[0].request.content)["text"]
        assert "Cutover go/no-go" in text
        assert "Diarization service unreachable" in text

    @respx.mock
    def test_a_very_long_error_is_truncated(self, admin_client, db_path, meeting):
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
        configure(
            admin_client,
            telegram_enabled=True,
            telegram_bot_token=BOT_TOKEN,
            telegram_chat_ids="-1001",
            telegram_notify_transcript_failed=True,
        )
        telegram_svc.notify_transcript_failed(db_path, meeting_id=meeting["id"], error="x" * 5000)

        import json

        text = json.loads(route.calls[0].request.content)["text"]
        assert len(text) < 5000


# --------------------------------------------------------------------------- #
# POST /api/telegram/test
# --------------------------------------------------------------------------- #


@respx.mock
def test_telegram_test_endpoint_uses_the_saved_config(admin_client):
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
    configure(admin_client, telegram_bot_token=BOT_TOKEN, telegram_chat_ids="-1001")

    resp = admin_client.post("/api/telegram/test")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert route.called


@respx.mock
def test_telegram_test_endpoint_can_try_unsaved_edits(admin_client):
    other_token = "999:XYZ"
    other_url = f"https://api.telegram.org/bot{other_token}/sendMessage"
    route = respx.post(other_url).mock(return_value=httpx.Response(200, json=telegram_response(True)))

    resp = admin_client.post(
        "/api/telegram/test", json={"bot_token": other_token, "chat_ids": "-2002"}
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert route.called


@respx.mock
def test_telegram_test_endpoint_ignores_a_masked_token_echo(admin_client):
    """The form round-trips the masked placeholder; that must not become the
    literal bot token used to send."""
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json=telegram_response(True)))
    configure(admin_client, telegram_bot_token=BOT_TOKEN, telegram_chat_ids="-1001")

    admin_client.post("/api/telegram/test", json={"bot_token": "••••ABC-DEF"})
    assert route.called, "the saved token must still be used"


def test_telegram_test_endpoint_is_admin_only(user_client):
    assert user_client.post("/api/telegram/test").status_code == 403
