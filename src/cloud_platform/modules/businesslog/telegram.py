"""Telegram adapter for the shared durable event outbox.

The dispatcher chooses the verified recipient and already-rendered text.
This adapter only posts it, propagating failure for bounded retry. The
operator channel is optional; customer cards use the main bot's user chat.
"""

from __future__ import annotations

from typing import Any


class TelegramBusinessLogChannel:
    """Send claimed outbox cards to the operator or verified customer."""

    def __init__(self, bot: Any, chat_id: int = 0) -> None:
        self._bot = bot
        self._chat_id = int(chat_id)

    @property
    def chat_id(self) -> int:
        return self._chat_id

    async def send(self, text: str) -> None:
        """Send one operator card; raises on failure so the outbox retries it."""
        if not self._chat_id:
            raise ValueError("a logger channel chat_id is required")
        await self._bot.send_message(
            chat_id=self._chat_id,
            text=text,
            disable_web_page_preview=True,
        )

    async def send_customer(self, chat_id: int, text: str) -> None:
        """Send to the verified customer's private chat, not the operator channel."""
        await self._bot.send_message(
            chat_id=chat_id,
            text=text,
            disable_web_page_preview=True,
        )
