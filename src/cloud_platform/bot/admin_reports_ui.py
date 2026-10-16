"""Currency-separated weekly/monthly admin summaries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cloud_platform.bot.ui import BotScreen
from cloud_platform.core.i18n import Translator
from cloud_platform.core.session_store import BotSessionStore
from cloud_platform.modules.admin_console.reports import AdminReportsService
from cloud_platform.modules.fx.formatting import format_minor
from cloud_platform.modules.navigation.domain import Callback, encode_telegram_callback
from cloud_platform.modules.users.domain import User


class AdminReportsUi:
    def __init__(
        self,
        key: str,
        service: AdminReportsService,
        store: BotSessionStore,
        translator: Translator,
        *,
        ttl_seconds: int = 900,
    ) -> None:
        self._key, self._service, self._store, self._t = key, service, store, translator

    async def clear_prompt(self, admin: User) -> None:
        await self._store.delete("admin-report-input", str(admin.id))

    def _button(self, key: str, screen: str, *args: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=self._t.t(key),
            callback_data=encode_telegram_callback(Callback("admin", screen, args), self._key),
        )

    async def handle(self, cb: Callback, admin: User) -> BotScreen | None:
        if (
            cb.flow != "admin"
            or cb.screen not in {"reports", "r_week", "r_month"}
            or len(cb.args) > 1
        ):
            return None
        period = "month" if cb.screen == "r_month" else "week"
        anchor = datetime.fromisoformat(cb.args[0]).replace(tzinfo=UTC) if cb.args else None
        report = await self._service.summary(admin, period, anchor=anchor)
        lines = [
            self._t.t(
                "admin.report_title", start=report.start.isoformat(), end=report.end.isoformat()
            )
        ]
        for key, totals in (
            ("admin.report_receipts", report.totals.payment_receipts),
            ("admin.report_charges", report.totals.customer_charges),
            ("admin.report_refunds", report.totals.customer_refunds),
            ("admin.report_net", report.totals.net_customer_revenue),
            ("admin.report_corporate_nominal", report.totals.corporate_nominal_expenses),
        ):
            lines.append("\n" + self._t.t(key))
            lines.extend(
                f"{total.currency}: {format_minor(total.amount_minor, total.currency)}"
                for total in totals
            )
            if not totals:
                lines.append(self._t.t("admin.report_empty"))
        lines.append("\n" + self._t.t("admin.report_corporate_native"))
        lines.extend(
            f"{cost.amount_exact} {cost.currency}"
            for cost in report.totals.corporate_provider_costs
        )
        if not report.totals.corporate_provider_costs:
            lines.append(self._t.t("admin.report_empty"))
        lines.append(
            "\n"
            + self._t.t(
                "admin.report_monthly_orders", count=report.totals.completed_customer_monthly_orders
            )
        )
        lines.append(self._t.t("admin.report_separate"))
        previous = (report.start - timedelta(days=1)).date().isoformat()
        navigation = [self._button("nav.prev", "r_" + period, previous)]
        if report.end < report.generated_at:
            navigation.append(
                self._button("nav.next", "r_" + period, report.end.date().isoformat())
            )
        return BotScreen(
            "\n".join(lines),
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        self._button("admin.report_week", "r_week"),
                        self._button("admin.report_month", "r_month"),
                    ],
                    navigation,
                    [self._button("nav.back", "menu")],
                ]
            ),
        )

    async def handle_text(self, text: str, admin: User) -> BotScreen | None:
        return None
