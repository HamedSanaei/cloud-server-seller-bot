"""Telegram transport with conservative, explicit send-outcome classification."""

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)

from cloud_platform.modules.admin_console.messaging import MessageRejected


class TelegramAdminMessageSender:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send(self, telegram_user_id: int, body: str) -> None:
        try:
            await self._bot.send_message(telegram_user_id, body, parse_mode=None)
        except TelegramRetryAfter as exc:
            raise MessageRejected(retry_after=exc.retry_after) from None
        except (
            TelegramBadRequest,
            TelegramForbiddenError,
            TelegramNotFound,
            TelegramUnauthorizedError,
        ):
            raise MessageRejected() from None
        # Network/timeouts and lost acknowledgements deliberately propagate.
        # The durable worker records them as ambiguous, never auto-resends.
