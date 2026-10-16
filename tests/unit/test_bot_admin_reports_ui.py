"""Weekly/monthly UI shows separate currencies and exact corporate native costs."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_admin_console_payments import ADMIN_TID, admin
from test_admin_console_reports import NOW

from cloud_platform.bot.admin_reports_ui import AdminReportsUi
from cloud_platform.core.i18n import Locale, Translator
from cloud_platform.core.session_store import InMemoryBotSessionStore
from cloud_platform.modules.admin_console.reports import (
    AdminReportsService,
    CurrencyTotal,
    ExactCurrencyTotal,
    FinancialTotals,
)
from cloud_platform.modules.navigation.domain import Callback, decode_callback

KEY = "admin-report-fixture-signing-key"


@pytest.mark.parametrize("locale", [Locale.FA, Locale.EN])
async def test_report_localizes_distinct_measures_and_preserves_exact_native_amount(locale):
    actor = admin()
    totals = FinancialTotals(
        payment_receipts=(CurrencyTotal("USD", 1050, 1), CurrencyTotal("EUR", 12345, 2)),
        customer_charges=(CurrencyTotal("EUR", 700, 3),),
        customer_refunds=(CurrencyTotal("EUR", 100, 1),),
        corporate_nominal_expenses=(CurrencyTotal("IRT", 1000000, 1),),
        corporate_provider_costs=(ExactCurrencyTotal("USD", "0.123456789012345678901", 1),),
        completed_customer_monthly_orders=2,
    )
    repo = SimpleNamespace(summarize=AsyncMock(return_value=totals))
    facade = AdminReportsService(
        SimpleNamespace(get=AsyncMock(return_value=actor)),
        repo,
        configured_ids=(ADMIN_TID,),
        clock=lambda: NOW,
    )
    ui = AdminReportsUi(KEY, facade, InMemoryBotSessionStore(), Translator(locale))
    result = await ui.handle(Callback("admin", "reports"), actor)
    assert result is not None
    assert "0.123456789012345678901 USD" in result.text
    assert "EUR" in result.text and "UTC" in result.text
    assert "2026-10-12" in result.text and "2026-10-16" in result.text
    assert "admin.report" not in result.text
    for row in result.keyboard.inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode()) <= 64
            decode_callback(button.callback_data, KEY)


async def test_previous_month_navigation_uses_real_calendar_bounds_and_next_period():
    actor = admin()
    repo = SimpleNamespace(summarize=AsyncMock(return_value=FinancialTotals()))
    facade = AdminReportsService(
        SimpleNamespace(get=AsyncMock(return_value=actor)),
        repo,
        configured_ids=(ADMIN_TID,),
        clock=lambda: NOW,
    )
    ui = AdminReportsUi(KEY, facade, InMemoryBotSessionStore(), Translator(Locale.EN))
    result = await ui.handle(Callback("admin", "r_month", ("2026-09-15",)), actor)
    repo.summarize.assert_awaited_once_with(
        datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)
    )
    actions = [
        decode_callback(button.callback_data, KEY)
        for row in result.keyboard.inline_keyboard
        for button in row
    ]
    assert Callback("admin", "r_month", ("2026-08-31",)) in actions
    assert Callback("admin", "r_month", ("2026-10-01",)) in actions
    assert await ui.handle_text("unrelated customer text", actor) is None
    assert await ui.handle(Callback("buy", "reports"), actor) is None
