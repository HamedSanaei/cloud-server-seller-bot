"""Signed Toman customer adjustments with frozen FX confirmation."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cloud_platform.bot.ui import BotScreen
from cloud_platform.core.i18n import Translator
from cloud_platform.core.session_store import BotSessionStore
from cloud_platform.modules.admin_console.wallet import AdminWalletAdjustmentService
from cloud_platform.modules.fx.domain import FxError
from cloud_platform.modules.fx.formatting import format_minor
from cloud_platform.modules.navigation.domain import Callback, encode_telegram_callback
from cloud_platform.modules.users.domain import User
from cloud_platform.modules.wallet.domain import InsufficientBalanceError

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


class AdminWalletUi:
    def __init__(
        self,
        key: str,
        service: AdminWalletAdjustmentService,
        store: BotSessionStore,
        translator: Translator,
        *,
        ttl_seconds: int = 900,
    ):
        self._key, self._service, self._store, self._t = key, service, store, translator
        self._ttl = ttl_seconds

    def _screen(self, text: str, nonce: str | None = None) -> BotScreen:
        rows = []
        if nonce:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=self._t.t("admin.confirm_action"),
                        callback_data=encode_telegram_callback(
                            Callback("admin", "w_apply", (nonce,)), self._key
                        ),
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text=self._t.t("nav.cancel"),
                    callback_data=encode_telegram_callback(Callback("admin", "menu"), self._key),
                )
            ]
        )
        return BotScreen(text, InlineKeyboardMarkup(inline_keyboard=rows))

    async def clear_prompt(self, admin: User) -> None:
        await self._service.clear(admin)
        await self._store.delete("admin-adjust-input", str(admin.id))

    async def handle(self, cb: Callback, admin: User) -> BotScreen | None:
        if cb.screen == "wallet_adjust":
            await self._store.put(
                "admin-adjust-input", str(admin.id), {"step": "target"}, ttl_seconds=self._ttl
            )
            return self._screen(self._t.t("admin.target_prompt"))
        if cb.screen == "w_target" and len(cb.args) == 1:
            await self._service.target(admin, int(cb.args[0]))
            await self._store.put(
                "admin-adjust-input",
                str(admin.id),
                {"step": "amount", "tid": int(cb.args[0])},
                ttl_seconds=self._ttl,
            )
            return self._screen(self._t.t("admin.toman_amount_prompt"))
        if cb.screen == "w_apply" and len(cb.args) == 1:
            try:
                wallet, _entry = await self._service.apply(admin, cb.args[0])
            except InsufficientBalanceError:
                return self._screen(self._t.t("admin.debit_insufficient"))
            return self._screen(
                self._t.t(
                    "admin.credit_done", balance=format_minor(wallet.balance, wallet.currency)
                )
            )
        return None

    async def handle_text(self, text: str, admin: User) -> BotScreen | None:
        pending = await self._store.get("admin-adjust-input", str(admin.id))
        if pending is None:
            return None
        value = text.strip().translate(_DIGITS)
        if pending["step"] == "target":
            if not value.isascii() or not value.isdigit() or len(value) > 19:
                return self._screen(self._t.t("admin.invalid_target"))
            try:
                await self._service.target(admin, int(value))
            except ValueError:
                return self._screen(self._t.t("admin.invalid_target"))
            await self._store.put(
                "admin-adjust-input",
                str(admin.id),
                {"step": "amount", "tid": int(value)},
                ttl_seconds=self._ttl,
            )
            return self._screen(self._t.t("admin.toman_amount_prompt"))
        if pending["step"] == "amount":
            digits = value[1:] if value[:1] in {"+", "-"} else value
            if (
                not digits.isascii()
                or not digits.isdigit()
                or len(digits) > 19
                or not 0 < abs(int(value)) <= 2**63 - 1
            ):
                return self._screen(self._t.t("admin.toman_amount_prompt"))
            await self._service.target(admin, pending["tid"])
            await self._store.put(
                "admin-adjust-input",
                str(admin.id),
                {**pending, "step": "reason", "amount": int(value)},
                ttl_seconds=self._ttl,
            )
            return self._screen(self._t.t("admin.reason_prompt"))
        if not 1 <= len(text.strip()) <= 500:
            return self._screen(self._t.t("admin.reason_prompt"))
        try:
            confirmation = await self._service.prepare(
                admin, pending["tid"], pending["amount"], text.strip()
            )
        except FxError:
            return self._screen(self._t.t("admin.fx_unavailable"))
        await self._store.delete("admin-adjust-input", str(admin.id))
        return self._screen(
            self._t.t(
                "admin.toman_confirm",
                tid=confirmation.telegram_user_id,
                toman=f"{confirmation.amount_toman:+,}",
                amount=format_minor(confirmation.amount_minor, confirmation.currency),
                rate=str(confirmation.snapshot.rate),
                source=confirmation.snapshot.source,
                observed=confirmation.snapshot.observed_at.isoformat(),
                reason=confirmation.reason,
            ),
            confirmation.nonce,
        )
