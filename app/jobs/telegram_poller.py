"""The inbound side of Telegram: one long-polling loop that links new chats
and answers messages from already-linked ones.

Same lifecycle shape as ``scheduler.py``'s ``AutoMatchScheduler`` -- started
and stopped by the FastAPI lifespan, always running even with Telegram
disabled, since a cycle checks the setting itself and an admin flipping it in
the UI should take effect within one cycle rather than at the next restart.

Unlike the auto-match sweep, there is no fixed tick to sleep on: Telegram's
own ``getUpdates`` long-poll *is* the wait, holding the connection open until
a message arrives or ``LONG_POLL_TIMEOUT_SEC`` elapses with none. A short
backoff only kicks in when the feature is disabled (nothing to wait on) or a
cycle raised (so a misconfigured bot token can't spin in a tight error loop).
"""

from __future__ import annotations

import asyncio

from app.config import effective
from app.db import get_conn
from app.logging_config import get_logger
from app.services import home_chat as home_chat_svc
from app.services import telegram as telegram_svc

log = get_logger("telegram_poller")

LONG_POLL_TIMEOUT_SEC = 25
DISABLED_RECHECK_SEC = 10
ERROR_BACKOFF_SEC = 5

NOT_LINKED_REPLY = (
    "Not linked yet. Open the app, go to Settings → Telegram, generate a code, "
    "then send /start <code> here."
)
LINK_MISSING_CODE_REPLY = (
    "Send /start followed by the code shown in Settings → Telegram, "
    "e.g. \"/start AB12CD34\"."
)
LINK_INVALID_CODE_REPLY = (
    "That code is invalid or expired. Generate a new one in Settings → Telegram."
)
LINK_SUCCESS_REPLY = "✅ Connected! You can now ask me things right here."


class TelegramPoller:
    def __init__(self, db_path=None):
        self.db_path = db_path
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._offset = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="mmn-telegram-poll")

    async def stop(self) -> None:
        self._stopping.set()
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # pragma: no cover - shutdown must not raise
            log.exception("telegram poll loop raised on shutdown")

    async def poll_once(self) -> dict:
        """One getUpdates round trip + dispatch. Public so a test -- and the
        operator -- can trigger it. Never raises: a cycle that blew up would
        take the whole loop task with it and silently stop the bot.
        """
        with get_conn(self.db_path) as conn:
            enabled = effective(conn, "telegram_enabled")
            bot_token = effective(conn, "telegram_bot_token")
        if not enabled or not bot_token:
            return {"enabled": False, "processed": 0}

        try:
            updates = await asyncio.to_thread(
                telegram_svc.get_updates,
                bot_token, offset=self._offset, timeout=LONG_POLL_TIMEOUT_SEC,
            )
        except telegram_svc.TelegramError:
            log.exception("telegram getUpdates failed")
            # No backoff wait happened inside this call (unlike a healthy long
            # poll), so the loop must add one itself -- otherwise a bad token
            # spins in a tight retry loop hammering Telegram's API.
            return {"enabled": True, "processed": 0, "error": True}

        processed = 0
        for update in updates:
            # Advance past every update seen, whether or not handling it
            # succeeds -- a message that can't be handled must not be
            # redelivered forever on every future cycle.
            self._offset = update["update_id"] + 1
            try:
                await self._handle_update(bot_token, update)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("failed to handle telegram update %s", update.get("update_id"))
            processed += 1
        return {"enabled": True, "processed": processed}

    async def _handle_update(self, bot_token: str, update: dict) -> None:
        message = update.get("message") or update.get("edited_message")
        if not message or "text" not in message:
            return
        chat_id = str(message["chat"]["id"])
        text = message["text"].strip()
        command = text.split(maxsplit=1)[0].lower() if text else ""

        if command in ("/start", "/link"):
            await self._try_link(bot_token, chat_id, text)
            return

        with get_conn(self.db_path) as conn:
            user_row = telegram_svc.find_user_by_chat_id(conn, chat_id)
        if user_row is None:
            await asyncio.to_thread(telegram_svc.send_message, bot_token, chat_id, NOT_LINKED_REPLY)
            return

        reply = await home_chat_svc.run_telegram_turn(self.db_path, user_row["id"], text)
        await asyncio.to_thread(telegram_svc.send_message, bot_token, chat_id, reply)

    async def _try_link(self, bot_token: str, chat_id: str, text: str) -> None:
        parts = text.split(maxsplit=1)
        code = parts[1].strip() if len(parts) > 1 else ""
        if not code:
            await asyncio.to_thread(
                telegram_svc.send_message, bot_token, chat_id, LINK_MISSING_CODE_REPLY
            )
            return

        with get_conn(self.db_path) as conn:
            user_id = telegram_svc.consume_link_code(conn, code.upper())
            if user_id is not None:
                telegram_svc.link_chat(conn, user_id, chat_id)

        reply = LINK_SUCCESS_REPLY if user_id is not None else LINK_INVALID_CODE_REPLY
        await asyncio.to_thread(telegram_svc.send_message, bot_token, chat_id, reply)

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                result = await self.poll_once()
            except asyncio.CancelledError:
                return
            except Exception:  # pragma: no cover - defensive
                log.exception("telegram poll cycle failed")
                result = {"enabled": True}

            # getUpdates' own long-poll timeout already paced a normal,
            # enabled cycle -- only back off when there was nothing to wait
            # on (disabled) or the request itself failed.
            if not result.get("enabled"):
                try:
                    await asyncio.sleep(DISABLED_RECHECK_SEC)
                except asyncio.CancelledError:
                    return
            elif result.get("error"):
                try:
                    await asyncio.sleep(ERROR_BACKOFF_SEC)
                except asyncio.CancelledError:
                    return


_poller: TelegramPoller | None = None


def get_poller() -> TelegramPoller:
    global _poller
    if _poller is None:
        _poller = TelegramPoller()
    return _poller


def set_poller(poller: TelegramPoller | None) -> None:
    global _poller
    _poller = poller
