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
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from aiogram import Bot, Dispatcher
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message

from cloud_platform.backup.job import BackupError, PostgresBackupJob, build_backup_config
from cloud_platform.bot.admin_ui import AdminBotUi
from cloud_platform.bot.identity_flow import IdentityFlow
from cloud_platform.bot.monthly_ui import MonthlyBotUi
from cloud_platform.bot.ui import BotScreen, BotUi
from cloud_platform.core.config import get_settings
from cloud_platform.core.container import Container, close_container, get_container
from cloud_platform.core.i18n import Locale, Translator, get_catalog
from cloud_platform.core.session_store import SessionStoreUnavailable
from cloud_platform.modules.navigation.domain import CallbackError, decode_callback
from cloud_platform.modules.users.domain import PermissionDeniedError, Role, User, UserStatus
from cloud_platform.modules.users.identity import IdentityService
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
    user = await _resolve_user(container, message.from_user)
    screen = monthly_ui.menu_screen(user)
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
        await message.answer(
            _t.t("servers.ssh_secret", username=secret.username, password=password),
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


async def _super_admin(message: Message, container: Container) -> User | None:
    """Require a private account with an active database administrator role."""
    actor = message.from_user
    # Database role is the authority; admin_chat_id is an alert destination,
    # not a restriction to a single privileged account.
    if (
        actor is None
        or message.chat.type != ChatType.PRIVATE
        or message.chat.id != actor.id
        or actor.is_bot
    ):
        return None
    user = await container.user_repository().get_by_telegram_user_id(actor.id)
    if user is None or user.role != Role.ADMIN or user.status != UserStatus.ACTIVE:
        return None
    return user


async def _admin_credit(message: Message, container: Container) -> None:
    admin = await _super_admin(message, container)
    if admin is None:
        await message.answer("دسترسی مجاز نیست.")
        return
    parts = (message.text or "").split(maxsplit=3)
    try:
        if len(parts) != 4:
            raise ValueError("missing fields")
        target_ref = parts[1]
        amount = int(parts[2])
        reason = parts[3].strip()
        if amount <= 0 or amount > 2**63 - 1 or not reason:
            raise ValueError("invalid amount or reason")
    except ValueError:
        await message.answer("فرمت: /admin_credit <شناسه تلگرام یا UUID مشتری> <واحد خرد> <دلیل>")
        return
    try:
        target = (
            await container.user_repository().get_by_telegram_user_id(int(target_ref))
            if target_ref.isascii() and target_ref.isdigit() and 0 < int(target_ref) <= 2**63 - 1
            else await container.user_repository().get(UUID(target_ref))
        )
    except ValueError:
        target = None
    if target is None:
        await message.answer("کاربر پیدا نشد.")
        return
    if target.id == admin.id or getattr(target, "role", Role.USER) == Role.ADMIN:
        await message.answer(_t.t("recharge.admin_disabled"))
        return
    user_id = target.id
    try:
        wallet, _ = await container.wallet_admin_service().adjust_balance(
            admin=admin,
            user_id=user_id,
            amount=amount,
            reason=reason,
            idempotency_key=f"telegram-admin-credit:{message.chat.id}:{message.message_id}",
        )
    except ValueError:
        logger.warning("admin credit rejected")
        await message.answer("شارژ انجام نشد؛ کیف پول یا مبلغ را بررسی کنید.")
        return
    await message.answer(f"شارژ انجام شد. موجودی: {wallet.balance} {wallet.currency}")


async def _admin_dump(message: Message, container: Container) -> None:
    if await _super_admin(message, container) is None:
        await message.answer("دسترسی مجاز نیست.")
        return
    try:
        with tempfile.TemporaryDirectory(prefix="cloud-admin-dump-") as directory:
            config = build_backup_config(get_settings())
            result = await PostgresBackupJob(replace(config, output_dir=Path(directory))).run()
            await message.answer_document(
                FSInputFile(Path(directory) / result.filename),
                caption="بکاپ رمزگذاری‌شده پایگاه داده؛ کلید رمزگشایی را جداگانه نگهداری کنید.",
                protect_content=True,
            )
    except BackupError:
        logger.warning("admin database dump failed")
        await message.answer("تهیه بکاپ ناموفق بود؛ تنظیمات بکاپ و دسترسی pg_dump را بررسی کنید.")


def register_handlers(
    dp: Dispatcher,
    ui: BotUi,
    monthly_ui: MonthlyBotUi,
    container: Container,
    *,
    admin_ui: AdminBotUi | None = None,
    identity_flow: IdentityFlow | None = None,
) -> None:
    """Attach the menu and callback handlers to ``dp``.

    Handler order is the priority order (aiogram stops at the first match):
    specific commands first, then the active text flow, then the generic
    fallback that lands every unmatched update on the main menu.
    """

    @dp.message(CommandStart())
    async def _start(message: Message) -> None:
        user = await _resolve_user(container, message.from_user)
        await message.answer(_t.t("greeting.start"), reply_markup=monthly_ui.reply_keyboard(user))
        screen = monthly_ui.menu_screen(user)
        await message.answer(screen.text, reply_markup=screen.keyboard)

    @dp.message(Command("admin_credit"))
    async def _credit_command(message: Message) -> None:
        await _admin_credit(message, container)

    @dp.message(Command("admin_dump"))
    async def _dump_command(message: Message) -> None:
        await _admin_dump(message, container)

    @dp.message(Command("admin"))
    async def _admin_command(message: Message) -> None:
        admin = await _super_admin(message, container)
        if admin is None or admin_ui is None:
            await message.answer(_t.t("admin.denied"))
            return
        screen = admin_ui.menu(admin)
        await message.answer(screen.text, reply_markup=screen.keyboard)

    @dp.message(Command("menu"))
    async def _menu(message: Message) -> None:
        await _show_main_menu(message, container=container, monthly_ui=monthly_ui)

    @dp.message(Command("help"))
    async def _help(message: Message) -> None:
        user = await _resolve_user(container, message.from_user)
        await message.answer(_t.t("greeting.help"), reply_markup=monthly_ui.reply_keyboard(user))

    @dp.message()
    async def _fallback(message: Message) -> None:
        """Active prompt answers first; reply buttons next; everything else lands on menu.

        Unknown slash commands, idle chat and unsupported media (photo,
        sticker, voice, ...) all resolve to the canonical main menu instead
        of being silently ignored.
        """
        user = await _resolve_user(container, message.from_user)
        try:
            if identity_flow is not None and user is not None:
                handled, resume = await identity_flow.handle(message, user)
                if handled:
                    if resume is not None:
                        user = await _resolve_user(container, message.from_user)
                        screen = await monthly_ui.handle(resume, user=user, chat_id=message.chat.id)
                        if screen is None:
                            screen = await ui.handle(resume, user=user, chat_id=message.chat.id)
                        await message.answer(screen.text, reply_markup=screen.keyboard)
                    return
            admin = await _super_admin(message, container)
            if admin_ui is not None and admin is not None and message.text:
                if _button_matches(message.text, "menu.admin"):
                    screen = admin_ui.menu(admin)
                else:
                    screen = await admin_ui.handle_text(message.text, admin)
                if screen is not None:
                    await message.answer(screen.text, reply_markup=screen.keyboard)
                    return
        except SessionStoreUnavailable:
            await message.answer(_t.t("servers.err_retry"))
            return
        except (ValueError, PermissionDeniedError):
            await message.answer(_t.t("admin.credit_failed"))
            return
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

            if _button_matches(message.text, "menu.buy"):
                screen = await monthly_ui.markets_screen()
                await message.answer(screen.text, reply_markup=screen.keyboard)
                return
            if _button_matches(message.text, "menu.servers"):
                screen = await monthly_ui.servers_screen(user)
                await message.answer(screen.text, reply_markup=screen.keyboard)
                return
            if _button_matches(message.text, "menu.wallet"):
                screen = await monthly_ui.wallet_screen(user)
                await message.answer(screen.text, reply_markup=screen.keyboard)
                return
            if _button_matches(message.text, "menu.recharge"):
                screen = await monthly_ui.recharge_screen(user)
                await message.answer(screen.text, reply_markup=screen.keyboard)
                return
            if _button_matches(message.text, "menu.support"):
                screen = monthly_ui.support_screen()
                await message.answer(screen.text, reply_markup=screen.keyboard)
                return
            if _button_matches(message.text, "nav.menu"):
                await _show_main_menu(message, container=container, monthly_ui=monthly_ui)
                return
        await _show_main_menu(message, container=container, monthly_ui=monthly_ui)

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
        if cb is not None and isinstance(query.message, Message):
            try:
                if cb.flow == "admin":
                    actor = query.from_user
                    private = query.message.chat.type == ChatType.PRIVATE
                    if (
                        admin_ui is None
                        or user is None
                        or user.role != Role.ADMIN
                        or user.status != UserStatus.ACTIVE
                        or not private
                        or actor is None
                        or query.message.chat.id != actor.id
                    ):
                        await query.answer(_t.t("admin.denied"), show_alert=True)
                        return
                    admin_screen = await admin_ui.handle(cb, user)
                    await query.message.edit_text(
                        admin_screen.text, reply_markup=admin_screen.keyboard
                    )
                    await query.answer()
                    return
                purchase = (
                    (
                        cb.flow == "store"
                        and cb.screen in {"confirm", "buy", "cloud_confirm", "cloud_buy"}
                    )
                    or (cb.flow == "offers" and cb.screen in {"confirm", "buy"})
                    or (cb.flow == "buy" and cb.screen in {"os", "confirm"})
                )
                if purchase and user is not None and identity_flow is not None:
                    if await identity_flow.begin(query.message, user, data):
                        await query.answer()
                        return
            except SessionStoreUnavailable:
                await query.answer(_t.t("servers.err_retry"), show_alert=True)
                return
            except (ValueError, PermissionDeniedError):
                await query.answer(_t.t("admin.credit_failed"), show_alert=True)
                return
        if cb is not None and cb.flow == "servers" and cb.screen == "ssh" and len(cb.args) == 1:
            await _send_ssh_password(query, monthly_ui, user, cb.args[0])
            return
        # Monthly flows first (LEASEWEB-MVP), everything else -> legacy UI.
        # Undecodable (tampered/foreign) button data is still rejected — it
        # is only rendered as the main menu plus a safe notice instead of a
        # dead end.
        try:
            screen = await monthly_ui.handle(data, user=user, chat_id=chat_id)
            if screen is None:
                if _is_foreign_callback(data):
                    menu = monthly_ui.menu_screen(user)
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
        payment_inquiry=container.payment_inquiry_service(gateways),
        # My Servers: the application service owns ownership, policy,
        # confirmations, idempotency and audit; the UI only renders.
        server_management=container.server_management_service(),
        server_page_size=settings.server_management_page_size,
        # PROD-HARDENING §2: callback references, pending confirmations and
        # prompts live in the SHARED store, so a restart or a second replica
        # does not lose the buttons the customer is holding.
        sessions=container.server_sessions(),
        checkout_sessions=container.checkout_sessions(),
        # Catalog display equivalents (supplementary; the DB price stays
        # authoritative). The SAME resolver instance as recharge: one
        # AbanTether + one cache client per process, closed once below.
        fx_resolver=fx_resolver,
        fx_display_currency=settings.fx_default_display_currency,
    )
    admin_ui = AdminBotUi(
        settings.callback_signing_key,
        gateways=container.gateway_management_service(gateways.keys()),
        users=container.user_repository(),
        wallets=container.wallet_repository(),
        wallet_admin=container.wallet_admin_service(),
        store=container.session_store(),
        ttl_seconds=settings.telegram_sessions_prompt_ttl_seconds,
    )
    identity_flow = IdentityFlow(
        IdentityService(container.user_repository()),
        container.session_store(),
        ttl_seconds=settings.telegram_sessions_prompt_ttl_seconds,
    )
    register_handlers(dp, ui, monthly_ui, container, admin_ui=admin_ui, identity_flow=identity_flow)

    logger.info("starting Telegram bot polling")
    try:
        await dp.start_polling(bot)
    finally:
        await Container.aclose_gateways(gateways)
        await Container.aclose_fx(fx_resolver)
        await close_container()


if __name__ == "__main__":
    asyncio.run(main())
