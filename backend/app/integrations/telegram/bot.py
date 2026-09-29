"""Telegram bot process (long polling, no public webhook needed).

Run:  python -m app.integrations.telegram.bot
Without TELEGRAM_BOT_TOKEN the process logs CONFIGURATION REQUIRED and idles (so Compose stays healthy).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.integrations.telegram.api_client import AtlasApiClient
from app.integrations.telegram.handlers import Ctx, handle_callback, handle_message

logger = logging.getLogger("atlas.telegram")


class TelegramSender:
    def __init__(self, token: str, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(base_url=f"https://api.telegram.org/bot{token}", timeout=70,
                                         transport=transport)

    async def call(self, method: str, **params: Any) -> Any:
        r = await self._client.post(f"/{method}", json=params)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
        return data["result"]

    async def send(self, chat_id: int, text: str, buttons: list[list[dict[str, str]]] | None = None) -> None:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text[:4096]}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        await self.call("sendMessage", **params)

    async def answer_callback(self, callback_id: str, text: str) -> None:
        await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190])

    async def close(self) -> None:
        await self._client.aclose()


async def run() -> None:
    s = get_settings()
    configure_logging(s.log_level, s.log_json)
    if not s.telegram_configured:
        logger.warning("Telegram CONFIGURATION REQUIRED: set TELEGRAM_BOT_TOKEN (and TELEGRAM_ALLOWED_CHAT_IDS). Idling.")
        while True:
            await asyncio.sleep(3600)
    if not s.telegram_allowed_chat_ids:
        logger.warning("TELEGRAM_ALLOWED_CHAT_IDS is empty: any chat can use this bot. Set it to your chat id.")
    sender = TelegramSender(s.telegram_bot_token.get_secret_value())
    ctx = Ctx(api=AtlasApiClient(), sender=sender, allowed_chat_ids=s.telegram_allowed_chat_ids,
              poll_seconds=s.telegram_result_poll_seconds)
    me = await sender.call("getMe")
    logger.info(f"telegram bot connected as @{me.get('username')}")
    offset = 0
    background: set[asyncio.Task] = set()
    while True:
        try:
            updates = await sender.call("getUpdates", offset=offset, timeout=50,
                                        allowed_updates=["message", "callback_query"])
        except Exception:  # noqa: BLE001 - network hiccups: back off and retry
            logger.exception("telegram_poll_failed")
            await asyncio.sleep(5)
            continue
        for upd in updates:
            offset = max(offset, upd["update_id"] + 1)
            try:
                if "message" in upd:
                    t = await handle_message(ctx, upd["message"])
                    if t:
                        background.add(t)
                        t.add_done_callback(background.discard)
                elif "callback_query" in upd:
                    await handle_callback(ctx, upd["callback_query"])
            except Exception:  # noqa: BLE001
                logger.exception("telegram_update_failed")


if __name__ == "__main__":
    asyncio.run(run())
