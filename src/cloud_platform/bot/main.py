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

from aiogram import Bot, Dispatcher
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message

from cloud_platform.backup.job import BackupError, PostgresBackupJob, build_backup_config
from cloud_platform.bot.admin_ui import AdminBotUi
from cloud_platform.bot.health import PollingHeartbeat
from cloud_platform.bot.identity_flow import IdentityFlow
from cloud_platform.bot.identity_ui import is_identity_cancel
from cloud_platform.bot.monthly_ui import MonthlyBotUi
from cloud_platform.bot.ui import BotScreen, BotUi, code_entities
from cloud_platform.core.config import get_settings
from cloud_platform.core.container import Container, close_container, get_container
from cloud_platform.core.i18n import Locale, Translator, get_catalog
from cloud_platform.core.session_store import SessionStoreUnavailable
from cloud_platform.modules.navigation.domain import Callback, CallbackError, decode_callback
from cloud_platform.modules.users.domain import PermissionDeniedError, User
from cloud_platform.modules.users.identity import IdentityService
from cloud_platform.modules.users.onboarding import handle_start
from cloud_platform.modules.users.superadmins import is_superadmin

if TYPE_CHECKING:
    from aiogram.types import User as TelegramUser

logger = logging.getLogger(__name__)

_t = Translator()

dp = Dispatcher()


def _configured_superadmins() -> tuple[int, ...]:
    return tuple(getattr(get_settings(), "telegram_superadmin_user_ids", ()))


def _trusted_sender(message: Message, actor: TelegramUser | None = None) -> bool:
    """Telegram's authenticated human sender, never a group/chat/forwarded identity."""
    sender = actor if actor is not None else message.from_user
    return (
        sender is not None
        and not sender.is_bot
        and type(sender.id) is int
        and 0 < sender.id <= 2**63 - 1
        and message.sender_chat is None
        and message.chat.type == ChatType.PRIVATE
        and message.chat.id == sender.id
    )


async def _resolve_user(container: Container, from_user: TelegramUser | None) -> User | None:
    """Map a Telegram user to a platform user, creating one on first contact."""
    if from_user is None or from_user.is_bot or from_user.id <= 0:
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
    if not _trusted_sender(message):
        await message.answer(_t.t("admin.denied"))
        return
    user = await _resolve_user(container, message.from_user)
    # Reinstall Telegram's persistent keyboard even after a role revocation.
    actor = user if is_superadmin(user, _configured_superadmins()) else None
    screen = monthly_ui.menu_screen(actor)
    await message.answer(screen.text, reply_markup=monthly_ui.reply_keyboard(actor))
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
        text = _t.t("servers.ssh_secret", username=secret.username, password=password)
        await message.answer(
            text,
            protect_content=True,
            entities=list(code_entities(text, (secret.username, password))),
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
    if not _trusted_sender(message):
        return None
    actor = message.from_user
    assert actor is not None
    user = await container.user_repository().get_by_telegram_user_id(actor.id)
    if (
        user is None
        or not is_superadmin(user, _configured_superadmins())
        or user.telegram_user_id != actor.id
    ):
        return None
    return user


async def _admin_credit(message: Message, container: Container) -> None:
    admin = await _super_admin(message, container)
    if admin is None:
        await message.answer("دسترسی مجاز نیست.")
        return
    # Historical direct credits bypassed the audited confirmation screen.
    # Keep a safe explanatory command, never a second money-mutation path.
    await message.answer(_t.t("admin.credit_use_management"))


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
    from cloud_platform.bot.platform_bridge import register_bridge_handlers

    register_bridge_handlers(dp, container, _resolve_user)

    async def clear_admin_navigation(admin: User) -> None:
        if admin_ui is not None:
            await admin_ui.clear_prompt(admin)
        clearer = getattr(monthly_ui, "clear_transient_input", None)
        if clearer is not None:
            await clearer(admin)
        if identity_flow is not None:
            await identity_flow.clear_prompt(admin)

    async def prepare_root(message: Message) -> bool:
        try:
            admin = await _super_admin(message, container)
            if admin is not None:
                await clear_admin_navigation(admin)
            return True
        except SessionStoreUnavailable:
            await message.answer(_t.t("servers.err_retry"))
            return False

    async def show_admin_root(
        message: Message, admin: User, *, notice_key: str | None = None
    ) -> None:
        await clear_admin_navigation(admin)
        await message.answer(
            _t.t("admin.root_title"), reply_markup=monthly_ui.reply_keyboard(admin)
        )
        screen = monthly_ui.menu_screen(admin)
        text = f"{_t.t(notice_key)}\n\n{screen.text}" if notice_key else screen.text
        await message.answer(text, reply_markup=screen.keyboard)

    async def show_admin_menu(message: Message, admin: User) -> None:
        await clear_admin_navigation(admin)
        if admin_ui is None:
            await show_admin_root(message, admin, notice_key="admin.denied")
            return
        screen = admin_ui.menu(admin)
        await message.answer(screen.text, reply_markup=admin_ui.reply_keyboard(admin))
        await message.answer(screen.text, reply_markup=screen.keyboard)

    async def handle_admin_text(message: Message, admin: User) -> bool:
        """Route a Super Admin's reply-keyboard text to the admin console.

        Returns True when a screen was emitted. The root console preempts the
        identity/verification flow for admins so a stale prompt cannot swallow
        a management action; verified identity facts are never cleared.
        """
        assert message.text is not None
        root_keys = (
            "admin.create_server",
            "admin.renew_server",
            "menu.buy",
            "menu.servers",
            "menu.support",
            "menu.admin",
            "nav.menu",
            "nav.back",
            "nav.cancel",
            "menu.wallet",
            "menu.recharge",
        )
        if any(_button_matches(message.text, key) for key in root_keys) or is_identity_cancel(
            message.text
        ):
            await clear_admin_navigation(admin)
        if _button_matches(message.text, "admin.create_server") or _button_matches(
            message.text, "menu.buy"
        ):
            await message.answer(
                _t.t("admin.root_title"), reply_markup=monthly_ui.reply_keyboard(admin)
            )
            screen = await monthly_ui.markets_screen()
            await message.answer(screen.text, reply_markup=screen.keyboard)
            return True
        if _button_matches(message.text, "admin.renew_server") or _button_matches(
            message.text, "menu.servers"
        ):
            await message.answer(
                _t.t("admin.root_title"), reply_markup=monthly_ui.reply_keyboard(admin)
            )
            screen = await monthly_ui.servers_screen(admin)
            await message.answer(screen.text, reply_markup=screen.keyboard)
            return True
        if _button_matches(message.text, "menu.support"):
            await message.answer(
                _t.t("admin.root_title"), reply_markup=monthly_ui.reply_keyboard(admin)
            )
            screen = monthly_ui.support_screen()
            await message.answer(screen.text, reply_markup=screen.keyboard)
            return True
        if _button_matches(message.text, "menu.admin"):
            await show_admin_menu(message, admin)
            return True
        if any(
            _button_matches(message.text, key) for key in ("nav.menu", "nav.back", "nav.cancel")
        ) or is_identity_cancel(message.text):
            await show_admin_root(message, admin)
            return True
        if _button_matches(message.text, "menu.wallet") or _button_matches(
            message.text, "menu.recharge"
        ):
            await show_admin_root(message, admin, notice_key="admin.no_personal_wallet")
            return True
        if message.text.startswith("/"):
            await show_admin_root(message, admin)
            return True
        return False

    async def private_message(message: Message) -> bool:
        if _trusted_sender(message):
            return True
        await message.answer(_t.t("admin.denied"))
        return False

    @dp.message(CommandStart())
    async def _start(message: Message) -> None:
        if not await private_message(message):
            return
        if not await prepare_root(message):
            return
        user = await _resolve_user(container, message.from_user)
        if is_superadmin(user, _configured_superadmins()):
            await message.answer(
                _t.t("admin.root_title"), reply_markup=monthly_ui.reply_keyboard(user)
            )
            screen = monthly_ui.menu_screen(user)
            await message.answer(screen.text, reply_markup=screen.keyboard)
            return
        await message.answer(_t.t("greeting.start"), reply_markup=monthly_ui.reply_keyboard(None))
        screen = monthly_ui.menu_screen(None)
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
        if admin is None:
            if _trusted_sender(message):
                await message.answer(
                    _t.t("admin.denied"), reply_markup=monthly_ui.reply_keyboard(None)
                )
            else:
                await message.answer(_t.t("admin.denied"))
            return
        try:
            await show_admin_menu(message, admin)
        except SessionStoreUnavailable:
            await message.answer(_t.t("servers.err_retry"))

    @dp.message(Command("menu"))
    async def _menu(message: Message) -> None:
        if not await private_message(message):
            return
        if not await prepare_root(message):
            return
        await _show_main_menu(message, container=container, monthly_ui=monthly_ui)

    @dp.message(Command("help"))
    async def _help(message: Message) -> None:
        if not await private_message(message):
            return
        if not await prepare_root(message):
            return
        user = await _resolve_user(container, message.from_user)
        actor = user if is_superadmin(user, _configured_superadmins()) else None
        await message.answer(_t.t("greeting.help"), reply_markup=monthly_ui.reply_keyboard(actor))

    @dp.message()
    async def _fallback(message: Message) -> None:
        """Active prompt answers first; reply buttons next; everything else lands on menu.

        Unknown slash commands, idle chat and unsupported media (photo,
        sticker, voice, ...) all resolve to the canonical main menu instead
        of being silently ignored.
        """
        if not await private_message(message):
            return
        user = await _resolve_user(container, message.from_user)
        try:
            admin = await _super_admin(message, container)
            if (
                admin is None
                and message.text
                and any(
                    _button_matches(message.text, key)
                    for key in ("menu.admin", "admin.credit", "admin.gateways")
                )
            ):
                await message.answer(
                    _t.t("admin.denied"), reply_markup=monthly_ui.reply_keyboard(None)
                )
                return
            if admin is not None and message.text:
                # The Super Admin root console (and its sub-flows) preempt the
                # identity/verification flow so a stale prompt cannot swallow a
                # management action. Verified identity facts are untouched.
                if await handle_admin_text(message, admin):
                    return
                if admin_ui is not None:
                    navigator = getattr(admin_ui, "handle_navigation", None)
                    screen = await navigator(message.text, admin) if navigator is not None else None
                    if screen is None and _button_matches(message.text, "admin.gateways"):
                        await admin_ui.clear_prompt(admin)
                        screen = await admin_ui.gateway_screen(admin)
                    elif screen is None and _button_matches(message.text, "admin.credit"):
                        screen = await admin_ui.handle(Callback("admin", "credit"), admin)
                    if screen is None:
                        screen = await admin_ui.handle_text(message.text, admin)
                    if screen is not None:
                        if identity_flow is not None:
                            await identity_flow.clear_prompt(admin)
                        clearer = getattr(monthly_ui, "clear_transient_input", None)
                        if clearer is not None:
                            await clearer(admin)
                        await message.answer(
                            _t.t("admin.root_title"), reply_markup=monthly_ui.reply_keyboard(admin)
                        )
                        await message.answer(screen.text, reply_markup=screen.keyboard)
                        return
            if identity_flow is not None and user is not None:
                handled, resume = await identity_flow.handle(message, user)
                if handled:
                    if resume is not None:
                        user = await _resolve_user(container, message.from_user)
                        if admin is not None:
                            actor = user if is_superadmin(user, _configured_superadmins()) else None
                            await message.answer(
                                _t.t("admin.root_title"),
                                reply_markup=monthly_ui.reply_keyboard(actor),
                            )
                        screen = await monthly_ui.handle(resume, user=user, chat_id=message.chat.id)
                        if screen is None:
                            screen = await ui.handle(resume, user=user, chat_id=message.chat.id)
                        await message.answer(screen.text, reply_markup=screen.keyboard)
                    elif admin is not None and not await identity_flow.has_prompt(admin):
                        await show_admin_root(message, admin)
                    return
            if admin is not None:
                if message.text and not message.text.startswith("/"):
                    cancel_wallet = getattr(monthly_ui, "cancel_custom_amount", None)
                    if cancel_wallet is not None:
                        await cancel_wallet(admin)
                    screen = await monthly_ui.handle_text(message.text, admin)
                    if screen is not None:
                        await message.answer(screen.text, reply_markup=screen.keyboard)
                        return
                await show_admin_root(message, admin)
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
                await message.answer(_t.t("servers.err_retry"))
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
        if not isinstance(query.message, Message) or not _trusted_sender(
            query.message, query.from_user
        ):
            await query.answer(_t.t("admin.denied"), show_alert=True)
            return
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
                    if not is_superadmin(user, _configured_superadmins()):
                        await query.message.answer(
                            _t.t("admin.denied"), reply_markup=monthly_ui.reply_keyboard(None)
                        )
                        await query.answer(_t.t("admin.denied"), show_alert=True)
                        return
                    assert user is not None
                    # Reload the authorization record for every privileged action;
                    # a stale screen or callback never carries its own authority.
                    current = await container.user_repository().get_by_telegram_user_id(
                        query.from_user.id
                    )
                    if (
                        current is None
                        or not is_superadmin(current, _configured_superadmins())
                        or current.telegram_user_id != query.from_user.id
                    ):
                        await query.message.answer(
                            _t.t("admin.denied"), reply_markup=monthly_ui.reply_keyboard(None)
                        )
                        await query.answer(_t.t("admin.denied"), show_alert=True)
                        return
                    assert isinstance(current, User)
                    if admin_ui is None:
                        await show_admin_root(query.message, current, notice_key="admin.denied")
                        await query.answer(_t.t("admin.denied"), show_alert=True)
                        return
                    clearer = getattr(monthly_ui, "clear_transient_input", None)
                    if clearer is not None:
                        await clearer(current)
                    if identity_flow is not None:
                        await identity_flow.clear_prompt(current)
                    admin_screen = await admin_ui.handle(cb, current)
                    # The persistent reply keyboard was installed on entry.
                    # Re-sending it creates chat noise below the inline panel.
                    try:
                        await query.message.edit_text(
                            admin_screen.text, reply_markup=admin_screen.keyboard
                        )
                    except TelegramBadRequest as exc:
                        if "message is not modified" not in exc.message.casefold():
                            raise
                    await query.answer()
                    return
                if is_superadmin(user, _configured_superadmins()):
                    assert user is not None
                    await clear_admin_navigation(user)
                    if cb.flow in {"wallet", "recharge"}:
                        # A stale customer wallet/top-up button must never open
                        # a personal wallet screen for a superadmin.
                        await query.message.answer(
                            _t.t("admin.no_personal_wallet"),
                            reply_markup=monthly_ui.reply_keyboard(user),
                        )
                        menu = monthly_ui.menu_screen(user)
                        await query.message.answer(menu.text, reply_markup=menu.keyboard)
                        await query.answer()
                        return
                    if cb.flow != "main" or cb.screen != "menu":
                        await query.message.answer(
                            _t.t("admin.root_title"), reply_markup=monthly_ui.reply_keyboard(user)
                        )
                if cb.flow == "main" and cb.screen == "menu":
                    actor = user if is_superadmin(user, _configured_superadmins()) else None
                    await query.message.answer(
                        monthly_ui.menu_screen(actor).text,
                        reply_markup=monthly_ui.reply_keyboard(actor),
                    )
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
            if cb is not None and cb.flow == "main" and cb.screen == "menu":
                actor = user if is_superadmin(user, _configured_superadmins()) else None
                screen = monthly_ui.menu_screen(actor)
            if screen is None:
                if _is_foreign_callback(data):
                    actor = user if is_superadmin(user, _configured_superadmins()) else None
                    if actor is not None:
                        await clear_admin_navigation(actor)
                    menu = monthly_ui.menu_screen(actor)
                    await query.message.answer(
                        menu.text, reply_markup=monthly_ui.reply_keyboard(actor)
                    )
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
    # Apply configured grants/revocations before Telegram can receive any update.
    await container.reconcile_superadmins()
    bot = Bot(token=settings.telegram_bot_token)
    heartbeat = PollingHeartbeat()
    bot.session.middleware(heartbeat)
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
        configured_ids=settings.telegram_superadmin_user_ids,
    )
    admin_ui = AdminBotUi(
        settings.callback_signing_key,
        gateways=container.gateway_management_service(gateways.keys()),
        users=container.user_repository(),
        wallets=container.wallet_repository(),
        wallet_admin=container.wallet_admin_service(),
        user_admin=container.admin_user_service(),
        wallet_adjustments=container.admin_wallet_adjustment_service(fx_resolver=fx_resolver),
        services=container.admin_server_support_service(),
        sales=container.admin_sales_service(),
        health=container.admin_health_service(),
        messaging=container.admin_messaging_service(),
        payments=container.admin_payment_service(gateways),
        reports=container.admin_reports_service(),
        store=container.session_store(),
        ttl_seconds=settings.telegram_sessions_prompt_ttl_seconds,
        configured_ids=settings.telegram_superadmin_user_ids,
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
        heartbeat.clear()
        await Container.aclose_gateways(gateways)
        await Container.aclose_fx(fx_resolver)
        await close_container()


if __name__ == "__main__":
    asyncio.run(main())
