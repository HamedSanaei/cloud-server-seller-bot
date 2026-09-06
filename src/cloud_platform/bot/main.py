"""Telegram bot entrypoint (M02-001): long-polling, aiogram 3.

``/start`` answers the catalog greeting (M02-007 contract); ``/menu``
resolves the Telegram identity (M02-002 onboarding) and renders the main
menu. Callbacks are split by flow: the MONTHLY flows (``offers``,
``servers``, ``wallet``, ``support``) are served by the LEASEWEB-MVP
:class:`MonthlyBotUi` (fixed-price monthly VPS storefront), every other
flow falls through to the legacy hourly :class:`BotUi`.

Running::

    uv run python -m cloud_platform.bot.main

Requires ``TELEGRAM_BOT_TOKEN`` (and ``CALLBACK_SIGNING_KEY`` for button
flows) in the environment or ``.env``. Selling monthly VPSes needs Postgres
(migrated), a synced+enabled sellable offer (``cloud_platform.cli
leaseweb sync-offers`` then ``offer enable/price``), and ``LEASEWEB_API_KEY``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from cloud_platform.bot.monthly_ui import MonthlyBotUi
from cloud_platform.bot.ui import BotUi
from cloud_platform.core.config import get_settings
from cloud_platform.core.container import close_container, get_container
from cloud_platform.core.i18n import Translator
from cloud_platform.modules.users.domain import User
from cloud_platform.modules.users.onboarding import handle_start

if TYPE_CHECKING:
    from aiogram.types import User as TelegramUser

    from cloud_platform.core.container import Container

logger = logging.getLogger(__name__)

_t = Translator()

dp = Dispatcher()


@dp.message(CommandStart())
async def start(message: Message) -> None:
    """Greeting (M02-007): always from the message catalog, Persian first."""
    await message.answer(_t.t("greeting.start"))


async def _resolve_user(container: Container, from_user: TelegramUser | None) -> User | None:
    """Map a Telegram user to a platform user, creating one on first contact."""
    if from_user is None:
        return None
    return await handle_start(
        container.user_repository(),
        container.wallet_repository(),
        telegram_user_id=from_user.id,
        username=from_user.username or "",
    )


def register_handlers(
    dp: Dispatcher, ui: BotUi, monthly_ui: MonthlyBotUi, container: Container
) -> None:
    """Attach the menu and callback handlers to ``dp``."""

    @dp.message(Command("menu"))
    async def _menu(message: Message) -> None:
        await _resolve_user(container, message.from_user)
        screen = ui.menu_screen()
        await message.answer(screen.text, reply_markup=screen.keyboard)

    @dp.message(Command("help"))
    async def _help(message: Message) -> None:
        await message.answer(_t.t("greeting.help"))

    @dp.callback_query()
    async def _callback(query: CallbackQuery) -> None:
        user = await _resolve_user(container, query.from_user)
        chat_id = query.message.chat.id if isinstance(query.message, Message) else None
        data = query.data or ""
        # Monthly flows first (LEASEWEB-MVP), everything else -> legacy UI.
        screen = await monthly_ui.handle(data, user=user, chat_id=chat_id)
        if screen is None:
            screen = await ui.handle(data, user=user, chat_id=chat_id)
        if isinstance(query.message, Message):
            try:
                await query.message.edit_text(screen.text, reply_markup=screen.keyboard)
            except Exception:
                logger.warning("failed to edit callback message", exc_info=True)
        await query.answer()


async def main() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required to run the bot")

    container = await get_container()
    await container.initialize()  # register provider adapters (Hetzner...)
    bot = Bot(token=settings.telegram_bot_token)
    ui = BotUi(
        settings.callback_signing_key,
        container.buy_flow_service(),
        os_selection=container.os_selection_service(),
        confirmation=container.purchase_confirmation_service(),
        catalog=container.catalog_repository(),
        create=container.create_server_service(),
    )
    monthly_ui = MonthlyBotUi(
        settings.callback_signing_key,
        offers_view=container.offer_catalog_view_service(),
        checkout=container.monthly_checkout_service(),
        servers=container.server_repository(),
        orders=container.provider_order_repository(),
        renewals=container.renewal_repository(),
        offers_repo=container.sellable_offer_repository(),
        wallet_history=container.wallet_history_service(),
        power=container.power_command_service(),
        support_contact=settings.support_contact,
    )
    register_handlers(dp, ui, monthly_ui, container)

    logger.info("starting Telegram bot polling")
    try:
        await dp.start_polling(bot)
    finally:
        await close_container()


if __name__ == "__main__":
    asyncio.run(main())
