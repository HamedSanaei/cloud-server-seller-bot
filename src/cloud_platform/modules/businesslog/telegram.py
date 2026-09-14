"""Telegram adapter for the business-log channel (release hardening).

The adapter is deliberately dumb: it renders nothing and decides nothing, it
just posts the already-rendered card to the configured private channel. Any
failure propagates so the dispatcher can retry with backoff.

``TelegramBusinessLogChannel`` reuses the main bot token (the bot is an
administrator/member of the logger channel) — a second token would only add
another secret to rotate with no architectural benefit.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class TelegramBusinessLogChannel:
    """Posts business events into the private operator channel."""

    def __init__(self, bot: Any, chat_id: int) -> None:
        if not chat_id:
            raise ValueError("a logger channel chat_id is required")
        self._bot = bot
        self._chat_id = int(chat_id)

    @property
    def chat_id(self) -> int:
        return self._chat_id

    async def send(self, text: str) -> None:
        """Send one card; raises on failure so the outbox retries it."""
        await self._bot.send_message(
            chat_id=self._chat_id,
            text=text,
            disable_web_page_preview=True,
        )
