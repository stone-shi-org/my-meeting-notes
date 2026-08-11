"""The inbound Telegram poller: linking via /start <code>, and the AI chat
turn a message from an already-linked chat triggers.
"""

from __future__ import annotations

import json

import httpx
import respx

from app.config import reset_settings_cache
from app.db import get_conn
from app.jobs.telegram_poller import TelegramPoller
from app.services import telegram as telegram_svc
from tests.test_chat import stream_response

BOT_TOKEN = "123456:ABC-DEF"
SEND_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
GETUPDATES_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
LLM_URL = "https://llm.test/v1/chat/completions"


def _update(update_id: int, chat_id: str, text: str) -> dict:
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


def _updates_response(updates: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": updates})


def _telegram_ok() -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


def _sent_text(route, index: int = 0) -> str:
    return json.loads(route.calls[index].request.content)["text"]


def _user_id(client) -> int:
    return client.get("/api/auth/me").json()["id"]


def enable_telegram(admin_client):
    resp = admin_client.put(
        "/api/settings", json={"values": {"telegram_enabled": True, "telegram_bot_token": BOT_TOKEN}}
    )
    assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------------- #
# Enable/disable gate
# --------------------------------------------------------------------------- #


@respx.mock
async def test_disabled_never_calls_getupdates(admin_client, db_path):
    admin_client.put("/api/settings", json={"values": {"telegram_enabled": False}})
    route = respx.get(GETUPDATES_URL).mock(return_value=_updates_response([]))

    poller = TelegramPoller(db_path)
    result = await poller.poll_once()

    assert result == {"enabled": False, "processed": 0}
    assert not route.called


@respx.mock
async def test_no_bot_token_never_calls_getupdates(admin_client, db_path):
    admin_client.put("/api/settings", json={"values": {"telegram_enabled": True, "telegram_bot_token": ""}})
    route = respx.get(GETUPDATES_URL).mock(return_value=_updates_response([]))

    poller = TelegramPoller(db_path)
    result = await poller.poll_once()

    assert result == {"enabled": False, "processed": 0}
    assert not route.called


@respx.mock
async def test_a_failed_getupdates_call_is_reported_not_raised(admin_client, db_path):
    enable_telegram(admin_client)
    respx.get(GETUPDATES_URL).mock(
        return_value=httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
    )

    poller = TelegramPoller(db_path)
    result = await poller.poll_once()

    assert result == {"enabled": True, "processed": 0, "error": True}


# --------------------------------------------------------------------------- #
# Linking
# --------------------------------------------------------------------------- #


@respx.mock
async def test_start_with_a_valid_code_links_and_confirms(admin_client, user_client, db_path):
    enable_telegram(admin_client)
    user_id = _user_id(user_client)
    with get_conn(db_path) as conn:
        code, _expires_at = telegram_svc.create_link_code(conn, user_id)

    respx.get(GETUPDATES_URL).mock(
        return_value=_updates_response([_update(1, "555", f"/start {code}")])
    )
    send_route = respx.post(SEND_URL).mock(return_value=_telegram_ok())

    poller = TelegramPoller(db_path)
    result = await poller.poll_once()

    assert result == {"enabled": True, "processed": 1}
    with get_conn(db_path) as conn:
        status = telegram_svc.get_link_status(conn, user_id)
    assert status["linked"] is True

    sent = json.loads(send_route.calls[0].request.content)
    assert sent["chat_id"] == "555"
    assert "connected" in sent["text"].lower()


@respx.mock
async def test_link_with_a_lowercase_code_still_works(admin_client, user_client, db_path):
    """Codes are generated uppercase; a user retyping one shouldn't have to
    match case exactly."""
    enable_telegram(admin_client)
    user_id = _user_id(user_client)
    with get_conn(db_path) as conn:
        code, _ = telegram_svc.create_link_code(conn, user_id)

    respx.get(GETUPDATES_URL).mock(
        return_value=_updates_response([_update(1, "555", f"/start {code.lower()}")])
    )
    respx.post(SEND_URL).mock(return_value=_telegram_ok())

    poller = TelegramPoller(db_path)
    await poller.poll_once()

    with get_conn(db_path) as conn:
        assert telegram_svc.get_link_status(conn, user_id)["linked"] is True


@respx.mock
async def test_link_command_also_works(admin_client, user_client, db_path):
    enable_telegram(admin_client)
    user_id = _user_id(user_client)
    with get_conn(db_path) as conn:
        code, _ = telegram_svc.create_link_code(conn, user_id)

    respx.get(GETUPDATES_URL).mock(
        return_value=_updates_response([_update(1, "555", f"/link {code}")])
    )
    respx.post(SEND_URL).mock(return_value=_telegram_ok())

    poller = TelegramPoller(db_path)
    await poller.poll_once()

    with get_conn(db_path) as conn:
        assert telegram_svc.get_link_status(conn, user_id)["linked"] is True


@respx.mock
async def test_start_with_an_invalid_code_does_not_link(admin_client, db_path):
    enable_telegram(admin_client)
    respx.get(GETUPDATES_URL).mock(
        return_value=_updates_response([_update(1, "555", "/start WRONGCODE")])
    )
    send_route = respx.post(SEND_URL).mock(return_value=_telegram_ok())

    poller = TelegramPoller(db_path)
    await poller.poll_once()

    with get_conn(db_path) as conn:
        assert telegram_svc.find_user_by_chat_id(conn, "555") is None
    assert "invalid or expired" in _sent_text(send_route).lower()


@respx.mock
async def test_start_with_no_code_asks_for_one(admin_client, db_path):
    enable_telegram(admin_client)
    respx.get(GETUPDATES_URL).mock(return_value=_updates_response([_update(1, "555", "/start")]))
    send_route = respx.post(SEND_URL).mock(return_value=_telegram_ok())

    poller = TelegramPoller(db_path)
    await poller.poll_once()

    assert "code" in _sent_text(send_route).lower()


# --------------------------------------------------------------------------- #
# Chat turn from a linked chat
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_message_from_an_unlinked_chat_gets_the_not_linked_reply(admin_client, db_path):
    enable_telegram(admin_client)
    respx.get(GETUPDATES_URL).mock(return_value=_updates_response([_update(1, "999", "hello")]))
    send_route = respx.post(SEND_URL).mock(return_value=_telegram_ok())

    poller = TelegramPoller(db_path)
    await poller.poll_once()

    assert "not linked" in _sent_text(send_route).lower()


@respx.mock
async def test_a_message_from_a_linked_chat_gets_an_ai_reply(admin_client, user_client, db_path, monkeypatch):
    monkeypatch.setenv("MMN_LLM_BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("MMN_LLM_MODEL", "test/model")
    monkeypatch.setenv("MMN_LLM_API_KEY", "sk-test")
    reset_settings_cache()

    enable_telegram(admin_client)
    user_id = _user_id(user_client)
    with get_conn(db_path) as conn:
        telegram_svc.link_chat(conn, user_id, "555")

    respx.get(GETUPDATES_URL).mock(
        return_value=_updates_response([_update(1, "555", "What needs my attention?")])
    )
    llm_route = respx.post(LLM_URL).mock(return_value=stream_response(["You have nothing pressing."]))
    send_route = respx.post(SEND_URL).mock(return_value=_telegram_ok())

    poller = TelegramPoller(db_path)
    result = await poller.poll_once()

    assert result["processed"] == 1
    assert llm_route.called
    sent = json.loads(send_route.calls[0].request.content)
    assert sent["chat_id"] == "555"
    assert "nothing pressing" in sent["text"]

    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT role, content FROM telegram_chat_messages WHERE owner_id = ? ORDER BY id",
            (user_id,),
        ).fetchall()
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[0]["content"] == "What needs my attention?"


# --------------------------------------------------------------------------- #
# Offset bookkeeping and per-item isolation
# --------------------------------------------------------------------------- #


@respx.mock
async def test_offset_advances_past_every_update_seen(admin_client, db_path):
    enable_telegram(admin_client)
    route = respx.get(GETUPDATES_URL).mock(
        return_value=_updates_response([_update(5, "111", "hello"), _update(6, "222", "hello")])
    )
    respx.post(SEND_URL).mock(return_value=_telegram_ok())

    poller = TelegramPoller(db_path)
    await poller.poll_once()
    assert poller._offset == 7

    await poller.poll_once()
    second_call_params = dict(route.calls[1].request.url.params)
    assert second_call_params["offset"] == "7"


@respx.mock
async def test_one_malformed_update_does_not_stop_the_rest_of_the_batch(admin_client, db_path):
    enable_telegram(admin_client)
    respx.get(GETUPDATES_URL).mock(
        return_value=_updates_response(
            [
                # No "chat" key -- raises inside _handle_update, must not
                # abort the batch or leave the offset stuck.
                {"update_id": 1, "message": {"text": "hello"}},
                _update(2, "222", "hello"),
            ]
        )
    )
    send_route = respx.post(SEND_URL).mock(return_value=_telegram_ok())

    poller = TelegramPoller(db_path)
    result = await poller.poll_once()

    assert result["processed"] == 2
    assert poller._offset == 3
    assert send_route.called
