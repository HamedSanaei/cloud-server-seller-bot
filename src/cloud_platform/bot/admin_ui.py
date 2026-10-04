"""Private superadmin gateway controls and confirmed customer wallet credit."""

from __future__ import annotations

import re
import secrets
from decimal import Decimal, localcontext
from typing import Any
from uuid import UUID

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cloud_platform.bot.ui import BotScreen
from cloud_platform.core.i18n import Translator
from cloud_platform.core.session_store import BotSessionStore
from cloud_platform.modules.fx.domain import currency_exponent
from cloud_platform.modules.fx.formatting import format_minor
from cloud_platform.modules.navigation.domain import Callback, encode_telegram_callback
from cloud_platform.modules.users.domain import (
    Permission,
    PermissionChecker,
    Role,
    User,
    UserRepository,
)
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
    ) -> None:
        self._key = signing_key
        self._gateways = gateways
        self._users = users
        self._wallets = wallets
        self._credit = wallet_admin
        self._store = store
        self._ttl = ttl_seconds
        self._t = Translator()

    @staticmethod
    def _authorize(admin: User) -> None:
        checker = PermissionChecker(admin)
        checker.require_active()
        checker.require(Permission.ADMIN_MANAGE_SETTINGS)
        if admin.id is None:
            raise ValueError("persisted administrator required")

    def _button(self, text: str, screen: str, *args: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=text,
            callback_data=encode_telegram_callback(Callback("admin", screen, args), self._key),
        )

    def menu(self, admin: User) -> BotScreen:
        self._authorize(admin)
        rows = [
            [self._button(self._t.t("admin.gateways"), "gateways")],
            [self._button(self._t.t("admin.credit"), "credit")],
            [
                InlineKeyboardButton(
                    text=self._t.t("nav.menu"),
                    callback_data=encode_telegram_callback(Callback("main", "menu"), self._key),
                )
            ],
        ]
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

    async def handle(self, cb: Callback, admin: User) -> BotScreen:
        self._authorize(admin)
        if cb.screen == "gateways":
            return await self.gateway_screen(admin)
        if cb.screen == "toggle" and len(cb.args) == 2 and cb.args[1] in {"on", "off"}:
            await self._gateways.set_enabled(admin, cb.args[0], cb.args[1] == "on")
            return await self.gateway_screen(admin)
        if cb.screen == "credit":
            await self._store.put(
                "admin-credit", str(admin.id), {"step": "target"}, ttl_seconds=self._ttl
            )
            return BotScreen(self._t.t("admin.target_prompt"), self._cancel_keyboard())
        if cb.screen == "cancel":
            if len(cb.args) == 1:
                await self._store.delete("admin-credit-confirm", f"{admin.id}:{cb.args[0]}")
            else:
                await self._store.delete("admin-credit", str(admin.id))
            return self.menu(admin)
        if cb.screen == "apply" and len(cb.args) == 1:
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
