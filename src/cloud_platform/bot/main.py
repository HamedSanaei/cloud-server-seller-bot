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
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from cloud_platform.bot.monthly_ui import MonthlyBotUi
from cloud_platform.bot.ui import BotScreen, BotUi
from cloud_platform.core.config import get_settings
from cloud_platform.core.container import close_container, get_container
from cloud_platform.core.i18n import Translator
from cloud_platform.core.session_store import SessionStoreUnavailable
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
        # The customer menu is the monthly storefront's (market selector first).
        screen = monthly_ui.menu_screen()
        await message.answer(screen.text, reply_markup=screen.keyboard)

    @dp.message(Command("help"))
    async def _help(message: Message) -> None:
        await message.answer(_t.t("greeting.help"))

    @dp.message()
    async def _text(message: Message) -> None:
        """Answer a pending prompt (server name, reverse DNS) or stay silent."""
        if not message.text or message.text.startswith("/"):
            return
        user = await _resolve_user(container, message.from_user)
        try:
            screen = await monthly_ui.handle_text(message.text, user)
        except SessionStoreUnavailable:
            logger.error("telegram session store unavailable; refusing text input")
            return
        if screen is None:
            return
        await message.answer(screen.text, reply_markup=screen.keyboard)

    @dp.callback_query()
    async def _callback(query: CallbackQuery) -> None:
        user = await _resolve_user(container, query.from_user)
        chat_id = query.message.chat.id if isinstance(query.message, Message) else None
        data = query.data or ""
        # Monthly flows first (LEASEWEB-MVP), everything else -> legacy UI.
        try:
            screen = await monthly_ui.handle(data, user=user, chat_id=chat_id)
            if screen is None:
                screen = await ui.handle(data, user=user, chat_id=chat_id)
        except SessionStoreUnavailable:
            # Defence in depth: the shared session store is unreachable. Refuse
            # with a safe message; nothing is executed and nothing is queued.
            logger.error("telegram session store unavailable; refusing callback")
            screen = BotScreen(_t.t("servers.err_retry"), InlineKeyboardMarkup(inline_keyboard=[]))
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

    # get_container() returns the process-wide container fully initialized
    # (providers registered); initializing again would register every
    # adapter a second time and fail at the registry boundary.
    container = await get_container()
    bot = Bot(token=settings.telegram_bot_token)
    ui = BotUi(
        settings.callback_signing_key,
        container.buy_flow_service(),
        os_selection=container.os_selection_service(),
        confirmation=container.purchase_confirmation_service(),
        catalog=container.catalog_repository(),
        create=container.create_server_service(),
    )
    gateway = container.payment_gateway()  # one HTTP client per bot process
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
        recharge=container.wallet_recharge_service(gateway),
        # My Servers: the application service owns ownership, policy,
        # confirmations, idempotency and audit; the UI only renders.
        server_management=container.server_management_service(),
        server_page_size=settings.server_management_page_size,
        # PROD-HARDENING §2: callback references, pending confirmations and
        # prompts live in the SHARED store, so a restart or a second replica
        # does not lose the buttons the customer is holding.
        sessions=container.server_sessions(),
    )
    register_handlers(dp, ui, monthly_ui, container)

    logger.info("starting Telegram bot polling")
    try:
        await dp.start_polling(bot)
    finally:
        if gateway is not None:
            await gateway.close()
        await close_container()


if __name__ == "__main__":
    asyncio.run(main())
