"""Private superadmin gateway controls and confirmed customer wallet credit."""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterable
from decimal import Decimal, localcontext
from typing import Any
from uuid import UUID

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)

from cloud_platform.bot.admin_operations_ui import (
    AdminHealthUi,
    AdminMessagingUi,
    AdminSalesUi,
    AdminSupportUi,
)
from cloud_platform.bot.admin_payments_ui import AdminPaymentsUi
from cloud_platform.bot.admin_reports_ui import AdminReportsUi
from cloud_platform.bot.admin_users_ui import AdminUsersUi
from cloud_platform.bot.admin_wallet_ui import AdminWalletUi
from cloud_platform.bot.monthly_ui import build_admin_reply_keyboard
from cloud_platform.bot.ui import BotScreen
from cloud_platform.core.i18n import Locale, Translator, get_catalog
from cloud_platform.core.session_store import BotSessionStore
from cloud_platform.modules.admin_console.users import AdminUserService
from cloud_platform.modules.admin_console.wallet import AdminWalletAdjustmentService
from cloud_platform.modules.fx.domain import currency_exponent
from cloud_platform.modules.fx.formatting import format_minor
from cloud_platform.modules.navigation.domain import Callback, encode_telegram_callback
from cloud_platform.modules.users.domain import (
    Role,
    User,
    UserRepository,
)
from cloud_platform.modules.users.superadmins import require_superadmin
from cloud_platform.modules.wallet.domain import WalletRepository
from cloud_platform.modules.wallet.service import WalletAdminService

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


class AdminBotUi:
    def __init__(
        self,
        signing_key: str,
        *,
        gateways: Any,
        users: UserRepository,
        wallets: WalletRepository,
        wallet_admin: WalletAdminService,
        store: BotSessionStore,
        ttl_seconds: int = 900,
        configured_ids: Iterable[int] = (),
        user_admin: AdminUserService | None = None,
        wallet_adjustments: AdminWalletAdjustmentService | None = None,
        payments: Any = None,
        reports: Any = None,
        services: Any = None,
        sales: Any = None,
        health: Any = None,
        messaging: Any = None,
    ) -> None:
        self._key = signing_key
        self._gateways = gateways
        self._users = users
        self._wallets = wallets
        self._credit = wallet_admin
        self._store = store
        self._ttl = ttl_seconds
        self._t = Translator()
        self._configured_ids = tuple(configured_ids)
        self._users_ui = (
            AdminUsersUi(
                signing_key,
                user_admin,
                store,
                self._t,
                ttl_seconds=ttl_seconds,
                wallet_enabled=wallet_adjustments is not None,
            )
            if user_admin is not None
            else None
        )
        self._wallet_ui = (
            AdminWalletUi(signing_key, wallet_adjustments, store, self._t, ttl_seconds=ttl_seconds)
            if wallet_adjustments is not None
            else None
        )
        self._extra_sections = {}
        for name, cls, service in (
            ("payments", AdminPaymentsUi, payments),
            ("reports", AdminReportsUi, reports),
            ("services", AdminSupportUi, services),
            ("sales", AdminSalesUi, sales),
            ("health", AdminHealthUi, health),
            ("messages", AdminMessagingUi, messaging),
        ):
            if service is not None:
                self._extra_sections[name] = cls(
                    signing_key, service, store, self._t, ttl_seconds=ttl_seconds
                )
        self._sections = tuple(
            section
            for section in (self._users_ui, self._wallet_ui, *self._extra_sections.values())
            if section is not None
        )

    def _authorize(self, admin: User) -> None:
        require_superadmin(admin, self._configured_ids)

    def reply_keyboard(self, admin: User) -> ReplyKeyboardMarkup:
        """Management keeps the corporate root available at all times."""
        self._authorize(admin)
        return build_admin_reply_keyboard(self._t)

    def _button(self, text: str, screen: str, *args: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=text,
            callback_data=encode_telegram_callback(Callback("admin", screen, args), self._key),
        )

    def menu(self, admin: User) -> BotScreen:
        self._authorize(admin)
        rows = [
            [self._button(self._t.t("admin.gateways"), "gateways")],
            [
                self._button(
                    self._t.t("admin.wallet_adjust" if self._wallet_ui else "admin.credit"),
                    "wallet_adjust" if self._wallet_ui else "credit",
                )
            ],
            [
                InlineKeyboardButton(
                    text=self._t.t("nav.menu"),
                    callback_data=encode_telegram_callback(Callback("main", "menu"), self._key),
                )
            ],
        ]
        if self._users_ui is not None:
            rows.insert(0, [self._button(self._t.t("admin.users"), "users")])
        for name in self._extra_sections:
            rows.insert(-1, [self._button(self._t.t(f"admin.{name}"), name)])
        return BotScreen(self._t.t("admin.title"), InlineKeyboardMarkup(inline_keyboard=rows))

    async def gateway_screen(self, admin: User) -> BotScreen:
        self._authorize(admin)
        states = await self._gateways.list(admin)
        rows = []
        lines = [self._t.t("admin.gateways")]
        for state in states:
            key = state.key
            name = self._t.t(f"payments.gateway.{key}")
            enabled = state.enabled
            label = self._t.t("admin.enabled" if enabled else "admin.disabled")
            lines.append(f"{name}: {label}")
            rows.append(
                [
                    self._button(
                        self._t.t("admin.disable" if enabled else "admin.enable", name=name),
                        "toggle",
                        key,
                        "off" if enabled else "on",
                    )
                ]
            )
        if not states:
            lines.append(self._t.t("recharge.unavailable"))
        rows.append([self._button(self._t.t("nav.back"), "menu")])
        return BotScreen("\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))

    async def clear_prompt(self, admin: User) -> None:
        self._authorize(admin)
        active = await self._store.get("admin-credit-active", str(admin.id))
        if active is not None:
            await self._store.delete("admin-credit-confirm", f"{admin.id}:{active['nonce']}")
            await self._store.delete("admin-credit-active", str(admin.id))
        await self._store.delete("admin-credit", str(admin.id))
        for section in self._sections:
            if section is not None:
                await section.clear_prompt(admin)

    async def handle_navigation(self, text: str, admin: User) -> BotScreen | None:
        """Dispatch management labels before active identity/customer input.

        New management sections extend this hook and ``handle`` for callbacks;
        ``handle_text`` remains reserved for active management input.
        """
        self._authorize(admin)
        navigation = [
            ("admin.gateways", "gateways"),
            ("admin.credit", "wallet_adjust" if self._wallet_ui else "credit"),
        ]
        if self._users_ui is not None:
            navigation.append(("admin.users", "users"))
        if self._wallet_ui is not None:
            navigation.append(("admin.wallet_adjust", "wallet_adjust"))
        navigation.extend((f"admin.{name}", name) for name in self._extra_sections)
        for key, screen in navigation:
            if any(text.strip() == get_catalog(loc).table[key] for loc in (Locale.FA, Locale.EN)):
                await self.clear_prompt(admin)
                return await self.handle(Callback("admin", screen), admin)
        return None

    async def handle(self, cb: Callback, admin: User) -> BotScreen:
        self._authorize(admin)
        if self._wallet_ui is not None and cb.screen == "credit":
            cb = Callback("admin", "wallet_adjust")
        if self._wallet_ui is not None and cb.screen == "apply":
            return BotScreen(self._t.t("nav.expired"), self.menu(admin).keyboard)
        if cb.screen in {
            "users",
            "u_search",
            "u_page",
            "u_profile",
            "u_status",
            "wallet_adjust",
            "w_target",
            "payments",
            "p_search",
            "p_pending",
            "p_review",
            "p_all",
            "p_page",
            "p_profile",
            "reports",
            "r_week",
            "r_month",
            "services",
            "s_reason",
            "sales",
            "c_page",
            "c_offer",
            "c_reason",
            "health",
            "h_failed",
            "messages",
        }:
            await self.clear_prompt(admin)
        for section in self._sections:
            if section is not None and (screen := await section.handle(cb, admin)) is not None:
                return screen
        if cb.screen in {"menu", "gateways"}:
            await self.clear_prompt(admin)
        if cb.screen == "gateways":
            return await self.gateway_screen(admin)
        if cb.screen == "toggle" and len(cb.args) == 2 and cb.args[1] in {"on", "off"}:
            await self._gateways.set_enabled(admin, cb.args[0], cb.args[1] == "on")
            return await self.gateway_screen(admin)
        if cb.screen == "credit":
            await self.clear_prompt(admin)
            await self._store.put(
                "admin-credit", str(admin.id), {"step": "target"}, ttl_seconds=self._ttl
            )
            return BotScreen(self._t.t("admin.target_prompt"), self._cancel_keyboard())
        if cb.screen == "cancel":
            if len(cb.args) == 1:
                await self._store.delete("admin-credit-confirm", f"{admin.id}:{cb.args[0]}")
                active = await self._store.get("admin-credit-active", str(admin.id))
                if active is not None and active.get("nonce") == cb.args[0]:
                    await self.clear_prompt(admin)
            else:
                await self.clear_prompt(admin)
            return self.menu(admin)
        if cb.screen == "apply" and len(cb.args) == 1:
            active = await self._store.get("admin-credit-active", str(admin.id))
            if active is None or active.get("nonce") != cb.args[0]:
                return BotScreen(self._t.t("nav.expired"), self.menu(admin).keyboard)
            record = await self._store.get("admin-credit-confirm", f"{admin.id}:{cb.args[0]}")
            if record is None:
                return BotScreen(self._t.t("nav.expired"), self.menu(admin).keyboard)
            target = await self._users.get(UUID(record["user_id"]))
            if (
                target is None
                or target.id is None
                or target.role == Role.ADMIN
                or target.id == admin.id
            ):
                return BotScreen(self._t.t("admin.invalid_target"), self.menu(admin).keyboard)
            wallet = await self._wallets.get(target.id)
            if wallet is None or wallet.currency != record["currency"]:
                return BotScreen(self._t.t("admin.credit_failed"), self.menu(admin).keyboard)
            wallet, _entry = await self._credit.adjust_balance(
                admin=admin,
                user_id=target.id,
                amount=record["amount"],
                reason=record["reason"],
                idempotency_key=f"admin-credit:{admin.id}:{cb.args[0]}",
                expected_currency=record["currency"],
            )
            # Keep the frozen confirmation until TTL so double-click/restart
            # replays the same immutable ledger fact rather than minting a key.
            return BotScreen(
                self._t.t(
                    "admin.credit_done", balance=format_minor(wallet.balance, wallet.currency)
                ),
                self.menu(admin).keyboard,
            )
        return self.menu(admin)

    def _cancel_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[[self._button(self._t.t("nav.cancel"), "cancel")]]
        )

    async def handle_text(self, text: str, admin: User) -> BotScreen | None:
        self._authorize(admin)
        for section in self._sections:
            if (
                section is not None
                and (screen := await section.handle_text(text, admin)) is not None
            ):
                return screen
        if self._wallet_ui is not None:
            return None
        pending = await self._store.get("admin-credit", str(admin.id))
        if pending is None:
            return None
        value = text.strip().translate(_DIGITS)
        if pending["step"] == "target":
            try:
                if value.isascii() and value.isdigit() and 0 < int(value) <= 2**63 - 1:
                    target = await self._users.get_by_telegram_user_id(int(value))
                else:
                    target = await self._users.get(UUID(value))
            except ValueError:
                target = None
            if (
                target is None
                or target.id is None
                or target.role == Role.ADMIN
                or target.id == admin.id
            ):
                return BotScreen(self._t.t("admin.invalid_target"), self._cancel_keyboard())
            wallet = await self._wallets.get(target.id)
            if wallet is None:
                return BotScreen(self._t.t("admin.invalid_target"), self._cancel_keyboard())
            await self._store.put(
                "admin-credit",
                str(admin.id),
                {"step": "amount", "user_id": str(target.id), "currency": wallet.currency},
                ttl_seconds=self._ttl,
            )
            return BotScreen(
                self._t.t("admin.amount_prompt", currency=wallet.currency), self._cancel_keyboard()
            )
        if pending["step"] == "amount":
            if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
                return BotScreen(self._t.t("recharge.invalid_amount"), self._cancel_keyboard())
            exponent = currency_exponent(pending["currency"])
            with localcontext() as context:
                context.prec = max(28, len(value) + exponent + 1)
                minor = Decimal(value) * Decimal(10) ** exponent
            if minor != minor.to_integral_value() or not 0 < minor <= 2**63 - 1:
                return BotScreen(self._t.t("recharge.invalid_amount"), self._cancel_keyboard())
            await self._store.put(
                "admin-credit",
                str(admin.id),
                {**pending, "step": "reason", "amount": int(minor)},
                ttl_seconds=self._ttl,
            )
            return BotScreen(self._t.t("admin.reason_prompt"), self._cancel_keyboard())
        if pending["step"] == "reason":
            if not value or len(value) > 500:
                return BotScreen(self._t.t("admin.reason_prompt"), self._cancel_keyboard())
            nonce = secrets.token_urlsafe(6)
            record = {**pending, "reason": text.strip()}
            await self._store.put(
                "admin-credit-confirm", f"{admin.id}:{nonce}", record, ttl_seconds=self._ttl
            )
            await self._store.put(
                "admin-credit-active", str(admin.id), {"nonce": nonce}, ttl_seconds=self._ttl
            )
            await self._store.delete("admin-credit", str(admin.id))
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [self._button(self._t.t("admin.confirm_credit"), "apply", nonce)],
                    [self._button(self._t.t("nav.cancel"), "cancel", nonce)],
                ]
            )
            return BotScreen(
                self._t.t(
                    "admin.credit_confirm",
                    user_id=pending["user_id"],
                    amount=format_minor(pending["amount"], pending["currency"]),
                    reason=text.strip(),
                ),
                keyboard,
            )
        return None
