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
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from cloud_platform.bot.admin_ui import AdminUi
from cloud_platform.bot.monthly_ui import MonthlyBotUi
from cloud_platform.bot.ui import BotScreen, BotUi, code_entities
from cloud_platform.core.config import get_settings
from cloud_platform.core.container import Container, close_container, get_container
from cloud_platform.core.i18n import Locale, Translator, get_catalog
from cloud_platform.core.session_store import SessionStoreUnavailable
from cloud_platform.modules.navigation.domain import CallbackError, decode_callback
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
    admin_ui: AdminUi | None = None,
    include_greeting: bool = False,
) -> None:
    """Resolve the user and retain the canonical storefront menu for everyone."""
    await _resolve_user(container, message.from_user)
    screen = monthly_ui.menu_screen()
    if admin_ui is not None and _admin_allowed(admin_ui, message):
        screen = admin_ui.with_menu_button(screen)
    text = f"{_t.t('greeting.start')}\n\n{screen.text}" if include_greeting else screen.text
    await message.answer(text, reply_markup=screen.keyboard)


def _admin_allowed(admin_ui: AdminUi, message: Message) -> bool:
    return admin_ui.allowed(
        message.from_user.id if message.from_user else None,
        message.chat.id,
        message.chat.type,
    )


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


def _button_matches(text: str, key: str) -> bool:
    """Check if message text matches a menu key in any supported locale."""
    target = text.strip()
    for loc in (Locale.FA, Locale.EN):
        try:
            if target == get_catalog(loc).table[key].strip():
                return True
        except Exception:
            pass
    return False


async def _send_ssh_password(
    query: CallbackQuery, monthly_ui: MonthlyBotUi, user: User | None, ref: str
) -> None:
    """Only a signed owner callback in a private chat can claim a one-time secret."""
    message = query.message
    if (
        user is None
        or user.id is None
        or not isinstance(message, Message)
        or message.chat.type != ChatType.PRIVATE
        or query.from_user is None
        or message.chat.id != query.from_user.id
    ):
        await query.answer(_t.t("servers.ssh_private_only"), show_alert=True)
        return
    try:
        claimed = await monthly_ui.claim_ssh_password(user, ref)
    except Exception:
        logger.warning("SSH password claim unavailable")
        await query.answer(_t.t("servers.ssh_unavailable"), show_alert=True)
        return
    if claimed is None:
        await query.answer(_t.t("servers.ssh_unavailable"), show_alert=True)
        return
    server_id, secret = claimed
    password = secret.reveal()
    if not password:
        await monthly_ui.finish_ssh_password(user, server_id, secret.claim_id, delivered=False)
        await query.answer(_t.t("servers.ssh_unavailable"), show_alert=True)
        return
    try:
        text = _t.t("servers.ssh_secret", username=secret.username, password=password)
        await message.answer(
            text,
            entities=code_entities(text, (secret.username, password)),
            protect_content=True,
        )
    except Exception:
        # Do not log exception text: a Telegram error may echo the message body.
        logger.warning("SSH password delivery failed")
        await monthly_ui.finish_ssh_password(user, server_id, secret.claim_id, delivered=False)
        await query.answer(_t.t("servers.err_retry"), show_alert=True)
        return
    await monthly_ui.finish_ssh_password(user, server_id, secret.claim_id, delivered=True)
    await query.answer()


def register_handlers(
    dp: Dispatcher,
    ui: BotUi,
    monthly_ui: MonthlyBotUi,
    container: Container,
    admin_ui: AdminUi | None = None,
) -> None:
    """Attach specific commands, active text prompts and the generic fallback."""

    @dp.message(CommandStart())
    async def _start(message: Message) -> None:
        keyboard = monthly_ui.reply_keyboard()
        if admin_ui is not None and _admin_allowed(admin_ui, message):
            await admin_ui.clear_prompt(message.from_user.id)
            keyboard = admin_ui.with_reply_button(keyboard)
        await message.answer(_t.t("greeting.start"), reply_markup=keyboard)
        await _show_main_menu(
            message,
            container=container,
            monthly_ui=monthly_ui,
            admin_ui=admin_ui,
            include_greeting=False,
        )

    @dp.message(Command("menu"))
    async def _menu(message: Message) -> None:
        if admin_ui is not None and _admin_allowed(admin_ui, message):
            await admin_ui.clear_prompt(message.from_user.id)
        await _show_main_menu(
            message, container=container, monthly_ui=monthly_ui, admin_ui=admin_ui
        )

    @dp.message(Command("help"))
    async def _help(message: Message) -> None:
        keyboard = monthly_ui.reply_keyboard()
        if admin_ui is not None and _admin_allowed(admin_ui, message):
            keyboard = admin_ui.with_reply_button(keyboard)
        await message.answer(_t.t("greeting.help"), reply_markup=keyboard)

    @dp.message()
    async def _fallback(message: Message) -> None:
        """Active prompt answers first; reply buttons next; everything else lands on menu.

        Unknown slash commands, idle chat and unsupported media (photo,
        sticker, voice, ...) all resolve to the canonical main menu instead
        of being silently ignored.
        """
        if message.text and not message.text.startswith("/"):
            user = await _resolve_user(container, message.from_user)
            try:
                if admin_ui is not None and _admin_allowed(admin_ui, message):
                    if _button_matches(message.text, "admin.menu"):
                        await admin_ui.clear_prompt(message.from_user.id)
                        admin_screen = admin_ui.menu_screen()
                        await message.answer(admin_screen.text, reply_markup=admin_screen.keyboard)
                        return
                    if _button_matches(message.text, "nav.menu") or any(
                        _button_matches(message.text, key)
                        for key in (
                            "menu.buy",
                            "menu.servers",
                            "menu.wallet",
                            "menu.recharge",
                            "menu.support",
                        )
                    ):
                        await admin_ui.clear_prompt(message.from_user.id)
                    else:
                        admin_screen = await admin_ui.handle_text(
                            message.from_user.id, message.text
                        )
                        if admin_screen is not None:
                            await message.answer(
                                admin_screen.text, reply_markup=admin_screen.keyboard
                            )
                            return
                screen = await monthly_ui.handle_text(message.text, user)
            except SessionStoreUnavailable:
                logger.error("telegram session store unavailable; refusing text input")
                return
            if screen is not None:
                await message.answer(
                    screen.text, reply_markup=screen.keyboard, entities=screen.entities
                )
                return
            if _button_matches(message.text, "menu.buy"):
                screen = await monthly_ui.markets_screen()
                await message.answer(
                    screen.text, reply_markup=screen.keyboard, entities=screen.entities
                )
                return
            if _button_matches(message.text, "menu.servers"):
                screen = await monthly_ui.servers_screen(user)
                await message.answer(
                    screen.text, reply_markup=screen.keyboard, entities=screen.entities
                )
                return
            if _button_matches(message.text, "menu.wallet"):
                screen = await monthly_ui.wallet_screen(user)
                await message.answer(
                    screen.text, reply_markup=screen.keyboard, entities=screen.entities
                )
                return
            if _button_matches(message.text, "menu.recharge"):
                screen = await monthly_ui.recharge_screen(user)
                await message.answer(
                    screen.text, reply_markup=screen.keyboard, entities=screen.entities
                )
                return
            if _button_matches(message.text, "menu.support"):
                screen = monthly_ui.support_screen()
                await message.answer(
                    screen.text, reply_markup=screen.keyboard, entities=screen.entities
                )
                return
            if _button_matches(message.text, "nav.menu"):
                await _show_main_menu(
                    message, container=container, monthly_ui=monthly_ui, admin_ui=admin_ui
                )
                return
        await _show_main_menu(
            message, container=container, monthly_ui=monthly_ui, admin_ui=admin_ui
        )

    @dp.callback_query()
    async def _callback(query: CallbackQuery) -> None:
        user = await _resolve_user(container, query.from_user)
        chat_id = query.message.chat.id if isinstance(query.message, Message) else None
        data = query.data or ""
        cb = None
        if "|" in data:
            try:
                cb = decode_callback(data, get_settings().callback_signing_key)
            except CallbackError:
                pass
        if cb is not None and cb.flow == "servers" and cb.screen == "ssh" and len(cb.args) == 1:
            await _send_ssh_password(query, monthly_ui, user, cb.args[0])
            return
        if cb is not None and cb.flow == "admin":
            if (
                admin_ui is None
                or query.from_user is None
                or not isinstance(query.message, Message)
                or not admin_ui.allowed(
                    query.from_user.id, query.message.chat.id, query.message.chat.type
                )
            ):
                await query.answer(_t.t("admin.denied"), show_alert=True)
                return
            try:
                if cb.screen == "cancel" and len(cb.args) <= 1:
                    if cb.args:
                        await admin_ui.cancel_action(query.from_user.id, cb.args[0])
                    await admin_ui.clear_prompt(query.from_user.id)
                    screen = admin_ui.with_menu_button(monthly_ui.menu_screen())
                else:
                    screen = await admin_ui.handle_callback(query.from_user.id, cb.screen, cb.args)
            except SessionStoreUnavailable:
                logger.error("telegram session store unavailable; refusing admin callback")
                screen = BotScreen(_t.t("admin.failed"), InlineKeyboardMarkup(inline_keyboard=[]))
            try:
                await query.message.edit_text(
                    screen.text, reply_markup=screen.keyboard, entities=screen.entities
                )
            except Exception:
                logger.warning("failed to edit admin callback message", exc_info=True)
            await query.answer()
            return
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
            if (
                admin_ui is not None
                and cb is not None
                and cb.flow == "main"
                and cb.screen == "menu"
                and isinstance(query.message, Message)
                and admin_ui.allowed(
                    query.from_user.id, query.message.chat.id, query.message.chat.type
                )
            ):
                screen = admin_ui.with_menu_button(screen)
        except SessionStoreUnavailable:
            # Defence in depth: the shared session store is unreachable. Refuse
            # with a safe message; nothing is executed and nothing is queued.
            logger.error("telegram session store unavailable; refusing callback")
            screen = BotScreen(_t.t("servers.err_retry"), InlineKeyboardMarkup(inline_keyboard=[]))
        if isinstance(query.message, Message):
            try:
                await query.message.edit_text(
                    screen.text, reply_markup=screen.keyboard, entities=screen.entities
                )
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
    fx_resolver = container.fx_resolver_or_none()  # one FX source+cache per bot process
    monthly_ui = MonthlyBotUi(
        settings.callback_signing_key,
        offers_view=container.offer_catalog_view_service(),
        checkout=container.monthly_checkout_service(),
        hourly=container.hourly_cloud_service(),
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
        recharge=container.wallet_recharge_service(gateways=gateways, fx_resolver=fx_resolver),
        # My Servers: the application service owns ownership, policy,
        # confirmations, idempotency and audit; the UI only renders.
        server_management=container.server_management_service(),
        server_page_size=settings.server_management_page_size,
        # PROD-HARDENING §2: callback references, pending confirmations and
        # prompts live in the SHARED store, so a restart or a second replica
        # does not lose the buttons the customer is holding.
        sessions=container.server_sessions(),
        # Catalog display equivalents (supplementary; the DB price stays
        # authoritative). The SAME resolver instance as recharge: one
        # AbanTether + one cache client per process, closed once below.
        fx_resolver=fx_resolver,
        fx_display_currency=settings.fx_default_display_currency,
    )
    admin_ui = AdminUi(
        settings.callback_signing_key,
        sessions=container.session_store(),
        users=container.user_repository(),
        credit=container.admin_wallet_service(),
    )
    register_handlers(dp, ui, monthly_ui, container, admin_ui)

    logger.info("starting Telegram bot polling")
    try:
        await dp.start_polling(bot)
    finally:
        await Container.aclose_gateways(gateways)
        await Container.aclose_fx(fx_resolver)
        await close_container()


if __name__ == "__main__":
    asyncio.run(main())
