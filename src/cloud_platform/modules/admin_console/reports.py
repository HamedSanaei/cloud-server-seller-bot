"""Calendar UTC financial reports with deliberately separate money meanings."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from cloud_platform.modules.users.domain import PermissionDeniedError, User, UserRepository
from cloud_platform.modules.users.superadmins import require_superadmin, validate_telegram_user_ids


@dataclass(frozen=True, slots=True)
class CurrencyTotal:
    currency: str
    amount_minor: int
    count: int


@dataclass(frozen=True, slots=True)
class ExactCurrencyTotal:
    currency: str
    amount_exact: str
    count: int


@dataclass(frozen=True, slots=True)
class FinancialTotals:
    payment_receipts: tuple[CurrencyTotal, ...] = ()
    customer_charges: tuple[CurrencyTotal, ...] = ()
    customer_refunds: tuple[CurrencyTotal, ...] = ()
    corporate_nominal_expenses: tuple[CurrencyTotal, ...] = ()
    corporate_provider_costs: tuple[ExactCurrencyTotal, ...] = ()
    completed_customer_monthly_orders: int = 0

    @property
    def net_customer_revenue(self) -> tuple[CurrencyTotal, ...]:
        """Wallet-funded service charges less actual refunds, per currency.

        Receipts and corporate expenses never participate in this measure.
        A refund in a later window reduces that window without rewriting the
        original charge. Counts describe source charge/refund ledger events.
        """
        totals: dict[str, int] = {}
        counts: dict[str, int] = {}
        for sign, rows in ((1, self.customer_charges), (-1, self.customer_refunds)):
            for row in rows:
                totals[row.currency] = totals.get(row.currency, 0) + sign * row.amount_minor
                counts[row.currency] = counts.get(row.currency, 0) + row.count
        return tuple(
            CurrencyTotal(currency, totals[currency], counts[currency])
            for currency in sorted(totals)
        )


@dataclass(frozen=True, slots=True)
class AdminFinancialReport:
    period: str
    start: datetime
    end: datetime
    generated_at: datetime
    totals: FinancialTotals


class AdminReportRepository(Protocol):
    async def summarize(self, start: datetime, end: datetime) -> FinancialTotals: ...


def report_window(period: str, moment: datetime, now: datetime) -> tuple[datetime, datetime]:
    if any(
        not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None
        for value in (moment, now)
    ):
        raise ValueError("report dates must be timezone-aware")
    moment, now = moment.astimezone(UTC), now.astimezone(UTC)
    if moment > now:
        raise ValueError("future report dates are not supported")
    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        start = day - timedelta(days=day.weekday())
        end = start + timedelta(days=7)
    elif period == "month":
        start = day.replace(day=1)
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    else:
        raise ValueError("report period must be week or month")
    return start, min(end, now)


class AdminReportsService:
    def __init__(
        self,
        users: UserRepository,
        repository: AdminReportRepository,
        *,
        configured_ids: Iterable[int] = (),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._users, self._repository = users, repository
        self._ids = validate_telegram_user_ids(tuple(configured_ids))
        self._clock = clock or (lambda: datetime.now(UTC))

    async def _authorize(self, actor: User) -> None:
        claimed = require_superadmin(actor, self._ids)
        if claimed.id is None:
            raise PermissionDeniedError("persisted administrator required")
        fresh = require_superadmin(await self._users.get(claimed.id), self._ids)
        if fresh.id != claimed.id or fresh.telegram_user_id != claimed.telegram_user_id:
            raise PermissionDeniedError("persisted administrator identity changed")

    async def summary(
        self,
        actor: User,
        period: str = "week",
        *,
        anchor: datetime | None = None,
    ) -> AdminFinancialReport:
        await self._authorize(actor)
        now = self._clock()
        start, end = report_window(period, anchor if anchor is not None else now, now)
        totals = await self._repository.summarize(start, end)
        return AdminFinancialReport(period, start, end, now.astimezone(UTC), totals)
