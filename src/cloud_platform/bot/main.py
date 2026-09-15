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
from cloud_platform.core.container import Container, close_container, get_container
from cloud_platform.core.i18n import Translator
from cloud_platform.core.session_store import SessionStoreUnavailable
from cloud_platform.modules.navigation.domain import decode_callback
from cloud_platform.modules.users.domain import User
from cloud_platform.modules.users.onboarding import handle_start

if TYPE_CHECKING:
    from aiogram.types import User as TelegramUser

logger = logging.getLogger(__name__)

_t = Translator()

dp = Dispatcher()


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


async def _show_main_menu(
    message: Message,
    *,
    container: Container,
    monthly_ui: MonthlyBotUi,
    include_greeting: bool = False,
) -> None:
    """Resolve the user, then render the canonical storefront main menu.

    The single shared helper behind /start, /menu and every fallback, so
    the customer always lands on the same InlineKeyboard menu and no
    parallel navigation system can drift from it.
    """
    await _resolve_user(container, message.from_user)
    screen = monthly_ui.menu_screen()
    text = f"{_t.t('greeting.start')}\n\n{screen.text}" if include_greeting else screen.text
    await message.answer(text, reply_markup=screen.keyboard)


def _is_foreign_callback(data: str) -> bool:
    """True when no signature check can pass (tampered/foreign button data).

    Signature validation itself is NOT weakened: undecodable data is still
    rejected, it is only rendered as the main menu plus a safe notice
    instead of a dead end. (CallbackError subclasses ValueError, so one
    clause covers malformed data and a missing signing key alike.)
    """
    try:
        decode_callback(data, get_settings().callback_signing_key)
    except ValueError:
        return True
    return False


def register_handlers(
    dp: Dispatcher, ui: BotUi, monthly_ui: MonthlyBotUi, container: Container
) -> None:
    """Attach the menu and callback handlers to ``dp``.

    Handler order is the priority order (aiogram stops at the first match):
    specific commands first, then the active text flow, then the generic
    fallback that lands every unmatched update on the main menu.
    """

    @dp.message(CommandStart())
    async def _start(message: Message) -> None:
        await _show_main_menu(
            message, container=container, monthly_ui=monthly_ui, include_greeting=True
        )

    @dp.message(Command("menu"))
    async def _menu(message: Message) -> None:
        await _show_main_menu(message, container=container, monthly_ui=monthly_ui)

    @dp.message(Command("help"))
    async def _help(message: Message) -> None:
        await message.answer(_t.t("greeting.help"))

    @dp.message()
    async def _fallback(message: Message) -> None:
        """Active prompt answers first; everything else lands on the menu.

        Unknown slash commands, idle chat and unsupported media (photo,
        sticker, voice, ...) all resolve to the canonical main menu instead
        of being silently ignored.
        """
        if message.text and not message.text.startswith("/"):
            user = await _resolve_user(container, message.from_user)
            try:
                screen = await monthly_ui.handle_text(message.text, user)
            except SessionStoreUnavailable:
                logger.error("telegram session store unavailable; refusing text input")
                return
            if screen is not None:
                await message.answer(screen.text, reply_markup=screen.keyboard)
                return
        await _show_main_menu(message, container=container, monthly_ui=monthly_ui)

    @dp.callback_query()
    async def _callback(query: CallbackQuery) -> None:
        user = await _resolve_user(container, query.from_user)
        chat_id = query.message.chat.id if isinstance(query.message, Message) else None
        data = query.data or ""
        # Monthly flows first (LEASEWEB-MVP), everything else -> legacy UI.
        # Undecodable (tampered/foreign) button data is still rejected — it
        # is only rendered as the main menu plus a safe notice instead of a
        # dead end.
        try:
            screen = await monthly_ui.handle(data, user=user, chat_id=chat_id)
            if screen is None:
                if _is_foreign_callback(data):
                    menu = monthly_ui.menu_screen()
                    screen = BotScreen(
                        f"{_t.t('nav.expired')}\n\n{menu.text}",
                        menu.keyboard,
                    )
                else:
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
    gateways = container.payment_gateways()  # one HTTP client each per bot process
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
        # The SAME gateway instances this process built above: building a
        # second collection here would leak a set of HTTP clients that the
        # shutdown path below never closes.
        recharge=container.wallet_recharge_service(gateways=gateways),
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
        await Container.aclose_gateways(gateways)
        await close_container()


if __name__ == "__main__":
    asyncio.run(main())
