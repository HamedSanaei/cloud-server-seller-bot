"""Five bounded SQL aggregates; customer receipts, charges and company costs differ.

Payments are external gateway cash receipts. Ledger CHARGE rows are actual
settled customer purchases/usage, including monthly hold capture and prepaid
hours. Neither accrual snapshots nor provider order selling-price snapshots
are added again. Corporate expenses never join either customer revenue query.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Numeric, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import LedgerEntry, OperatorExpense, PaymentSession, ProviderOrder
from cloud_platform.db.timestamps import to_db_utc
from cloud_platform.modules.admin_console.reports import (
    CurrencyTotal,
    ExactCurrencyTotal,
    FinancialTotals,
)


def _integer(value: Any) -> int:
    if type(value) is int:
        return value
    if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        return int(value)
    raise ValueError("financial aggregate must be an exact integer")


def _minor_rows(rows: Any) -> tuple[CurrencyTotal, ...]:
    return tuple(
        CurrencyTotal(str(currency), _integer(amount), _integer(count))
        for currency, amount, count in rows
    )


class SqlAlchemyAdminReportRepository:
    def __init__(
        self, session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]]
    ) -> None:
        self._session_factory = session_factory

    async def summarize(self, start: datetime, end: datetime) -> FinancialTotals:
        if (
            start.tzinfo is None
            or end.tzinfo is None
            or not start <= end <= start + timedelta(days=32)
        ):
            raise ValueError("report window must be an aware UTC period of at most 32 days")
        legacy_start, legacy_end = to_db_utc(start), to_db_utc(end)
        payments = (
            select(PaymentSession.currency, func.sum(PaymentSession.amount_minor), func.count())
            .where(
                PaymentSession.status == "succeeded",
                PaymentSession.credited_at >= legacy_start,
                PaymentSession.credited_at < legacy_end,
            )
            .group_by(PaymentSession.currency)
            .order_by(PaymentSession.currency)
        )
        charges = (
            select(
                LedgerEntry.entry_type,
                LedgerEntry.currency,
                func.sum(LedgerEntry.amount),
                func.count(),
            )
            .where(
                LedgerEntry.entry_type.in_(["charge", "refund"]),
                LedgerEntry.created_at >= legacy_start,
                LedgerEntry.created_at < legacy_end,
            )
            .group_by(LedgerEntry.entry_type, LedgerEntry.currency)
            .order_by(LedgerEntry.entry_type, LedgerEntry.currency)
        )
        expenses = (
            select(
                OperatorExpense.nominal_currency,
                func.sum(OperatorExpense.nominal_amount_minor),
                func.count(),
            )
            .where(OperatorExpense.occurred_at >= start, OperatorExpense.occurred_at < end)
            .group_by(OperatorExpense.nominal_currency)
            .order_by(OperatorExpense.nominal_currency)
        )
        snapshot = OperatorExpense.provider_cost_snapshot
        cost_currency = snapshot["currency"].as_string()
        exact_cost = cast(snapshot["amount_exact"].as_string(), Numeric())
        costs = (
            select(cost_currency, func.sum(exact_cost), func.count())
            .where(OperatorExpense.occurred_at >= start, OperatorExpense.occurred_at < end)
            .group_by(cost_currency)
            .order_by(cost_currency)
        )
        completed_orders = (
            select(func.count())
            .select_from(ProviderOrder)
            .where(
                ProviderOrder.funding_source == "customer_wallet",
                ProviderOrder.status == "active",
                ProviderOrder.settlement_status == "complete",
                ProviderOrder.settlement_attempted_at >= start,
                ProviderOrder.settlement_attempted_at < end,
            )
        )
        async with self._session_factory() as db:
            receipts = _minor_rows((await db.execute(payments)).all())
            charge_rows = (await db.execute(charges)).all()
            customer_charges = _minor_rows(row[1:] for row in charge_rows if row[0] == "charge")
            refunds = _minor_rows(row[1:] for row in charge_rows if row[0] == "refund")
            nominal = _minor_rows((await db.execute(expenses)).all())
            provider_costs = []
            for currency, amount, count in (await db.execute(costs)).all():
                if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
                    raise ValueError("provider cost aggregate must be an exact nonnegative Decimal")
                text = format(amount, "f")
                text = text.rstrip("0").rstrip(".") if "." in text else text
                provider_costs.append(ExactCurrencyTotal(str(currency), text, _integer(count)))
            order_count = _integer((await db.execute(completed_orders)).scalar_one())
        return FinancialTotals(
            receipts, customer_charges, refunds, nominal, tuple(provider_costs), order_count
        )
