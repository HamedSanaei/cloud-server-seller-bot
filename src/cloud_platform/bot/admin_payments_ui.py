"""Invoice search and provider verification, without a manual mark-paid action."""

from __future__ import annotations

import secrets

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cloud_platform.bot.ui import BotScreen
from cloud_platform.core.i18n import Translator
from cloud_platform.core.session_store import BotSessionStore
from cloud_platform.modules.admin_console.payments import AdminPaymentService, AdminPaymentSummary
from cloud_platform.modules.fx.formatting import format_minor
from cloud_platform.modules.navigation.domain import (
    Callback,
    decode_offer_ref,
    encode_offer_ref,
    encode_telegram_callback,
)
from cloud_platform.modules.users.domain import User


class AdminPaymentsUi:
    def __init__(
        self,
        key: str,
        service: AdminPaymentService,
        store: BotSessionStore,
        translator: Translator,
        *,
        ttl_seconds: int = 900,
    ) -> None:
        self._key, self._service, self._store, self._t = key, service, store, translator
        self._ttl = ttl_seconds

    def _button(self, key: str, screen: str, *args: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=self._t.t(key),
            callback_data=encode_telegram_callback(Callback("admin", screen, args), self._key),
        )

    def _screen(self, text: str, rows: list[list[InlineKeyboardButton]] | None = None) -> BotScreen:
        rows = list(rows or [])
        rows.append([self._button("nav.back", "menu")])
        return BotScreen(text, InlineKeyboardMarkup(inline_keyboard=rows))

    async def clear_prompt(self, admin: User) -> None:
        await self._store.delete("admin-payment-input", str(admin.id))

    async def _search(
        self,
        admin: User,
        query: str = "",
        status: str | None = None,
        offset: int = 0,
    ) -> BotScreen:
        page = await self._service.search(admin, query, status=status, offset=offset, limit=10)
        nonce = secrets.token_urlsafe(6)
        await self._store.put(
            "admin-payment-search",
            f"{admin.id}:{nonce}",
            {"query": query, "status": status},
            ttl_seconds=self._ttl,
        )
        rows = []
        for item in page.items:
            label = f"{item.gateway_key} · {item.status.value} · " + format_minor(
                item.amount_minor, item.currency
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        text=label[:100],
                        callback_data=encode_telegram_callback(
                            Callback("admin", "p_profile", (encode_offer_ref(item.session_id),)),
                            self._key,
                        ),
                    )
                ]
            )
        navigation = []
        if offset:
            navigation.append(
                self._button("nav.prev", "p_page", nonce, str(max(0, offset - page.limit)))
            )
        if page.has_more:
            navigation.append(self._button("nav.next", "p_page", nonce, str(offset + page.limit)))
        if navigation:
            rows.append(navigation)
        rows.extend(
            [
                [self._button("admin.payments_search", "p_search")],
                [
                    self._button("admin.payments_pending", "p_pending"),
                    self._button("admin.payments_review", "p_review"),
                ],
                [self._button("admin.payments_all", "p_all")],
            ]
        )
        return self._screen(self._t.t("admin.payments_page", count=len(page.items)), rows)

    def _profile(self, payment: AdminPaymentSummary, *, notice: str = "") -> BotScreen:
        text = self._t.t(
            "admin.payment_profile",
            id=payment.session_id,
            owner=payment.user_id,
            gateway=payment.gateway_key,
            status=payment.status.value,
            charge=format_minor(payment.amount_minor, payment.currency),
            credit=format_minor(payment.credit_amount_minor, payment.credit_currency),
            created=payment.created_at.isoformat() if payment.created_at else "—",
            credited=payment.credited_at.isoformat() if payment.credited_at else "—",
        )
        if notice:
            text += "\n\n" + notice
        return self._screen(
            text,
            [
                [
                    self._button(
                        "admin.payment_inquire", "p_inquire", encode_offer_ref(payment.session_id)
                    )
                ],
                [self._button("admin.payments_all", "p_all")],
            ],
        )

    async def handle(self, cb: Callback, admin: User) -> BotScreen | None:
        if cb.flow != "admin":
            return None
        if cb.screen in {"payments", "p_all", "p_pending", "p_review"} and not cb.args:
            await self.clear_prompt(admin)
            status = {"p_pending": "pending", "p_review": "manual_review"}.get(cb.screen)
            return await self._search(admin, status=status)
        if cb.screen == "p_search" and not cb.args:
            await self._service.search(admin, limit=1)
            await self._store.put(
                "admin-payment-input", str(admin.id), {"step": "search"}, ttl_seconds=self._ttl
            )
            return self._screen(self._t.t("admin.payments_search_prompt"))
        if cb.screen == "p_page" and len(cb.args) == 2:
            await self._service.search(admin, limit=1)
            record = await self._store.get("admin-payment-search", f"{admin.id}:{cb.args[0]}")
            if record is None:
                return self._screen(self._t.t("nav.expired"))
            await self.clear_prompt(admin)
            return await self._search(admin, record["query"], record["status"], int(cb.args[1]))
        if cb.screen == "p_profile" and len(cb.args) == 1:
            await self.clear_prompt(admin)
            return self._profile(await self._service.profile(admin, decode_offer_ref(cb.args[0])))
        if cb.screen == "p_inquire" and len(cb.args) == 1:
            result = await self._service.inquire(admin, decode_offer_ref(cb.args[0]))
            notice = self._t.t("admin.payment_inquiry_result", status=result.observed_status)
            if result.settlement_held:
                notice += "\n" + self._t.t("admin.payment_review_held")
            return self._profile(result.payment, notice=notice)
        return None

    async def handle_text(self, text: str, admin: User) -> BotScreen | None:
        pending = await self._store.get("admin-payment-input", str(admin.id))
        if pending is None:
            return None
        if len(text.strip()) > 100:
            await self._service.search(admin, limit=1)
            return self._screen(self._t.t("admin.payments_search_prompt"))
        screen = await self._search(admin, text.strip())
        await self.clear_prompt(admin)
        return screen
