"""SQLAlchemy persistence for accrual-period records (M06-005)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import AccrualPeriod as _AccrualPeriodModel
from cloud_platform.modules.billing.service import AccrualPeriod, AccrualPeriodExistsError

_ACCRUAL_LOCK_KEY = 777_005


def _attr(row: Any, name: str) -> Any:
    return getattr(row, name, None)


def _currency_or_fallback(row: Any, name: str, fallback: str) -> str:
    """Read a nullable currency, using the legacy generic currency if absent."""
    value = _attr(row, name)
    if isinstance(value, str) and value.strip():
        return value
    return fallback


def _aware_or_none(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _to_domain(row: _AccrualPeriodModel) -> AccrualPeriod:
    period_start = _aware_or_none(_attr(row, "period_start"))
    period_end = _aware_or_none(_attr(row, "period_end"))
    if period_start is None or period_end is None:
        raise ValueError("accrual period row has a NULL period boundary")
    legacy_currency = str(_attr(row, "currency"))
    return AccrualPeriod(
        id=_attr(row, "id"),
        server_id=_attr(row, "server_id"),
        wallet_id=_attr(row, "wallet_id"),
        period_start=period_start,
        period_end=period_end,
        quanta=int(_attr(row, "quanta")),
        cost_minor=int(_attr(row, "cost_minor")),
        selling_minor=int(_attr(row, "selling_minor")),
        currency=legacy_currency,
        idempotency_key=str(_attr(row, "idempotency_key")),
        cost_currency=_currency_or_fallback(row, "cost_currency", legacy_currency),
        selling_currency=_currency_or_fallback(row, "selling_currency", legacy_currency),
        cost_amount=(
            Decimal(str(_attr(row, "cost_amount")))
            if _attr(row, "cost_amount") is not None
            else None
        ),
        rule_key=(str(_attr(row, "rule_key")) if _attr(row, "rule_key") else None),
    )


class SqlAlchemyAccrualPeriodRepository:
    """Durable storage for settled usage periods."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def get_by_key(self, idempotency_key: str) -> AccrualPeriod | None:
        async with self._session_factory() as session:
            stmt = select(_AccrualPeriodModel).where(
                _AccrualPeriodModel.idempotency_key == idempotency_key
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            return _to_domain(row) if row is not None else None

    async def add(self, period: AccrualPeriod) -> AccrualPeriod:
        async with self._session_factory() as session:
            existing = (
                await session.execute(
                    select(_AccrualPeriodModel.id).where(
                        _AccrualPeriodModel.idempotency_key == period.idempotency_key
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise AccrualPeriodExistsError(
                    f"accrual period {period.idempotency_key} already exists"
                )
            row = _AccrualPeriodModel(
                server_id=period.server_id,
                wallet_id=period.wallet_id,
                period_start=period.period_start,
                period_end=period.period_end,
                quanta=period.quanta,
                cost_minor=period.cost_minor,
                cost_amount=(str(period.cost_amount) if period.cost_amount is not None else None),
                selling_minor=period.selling_minor,
                # Keep the legacy column populated as the selling currency so
                # old readers remain correct; the explicit side columns carry
                # the native cost/customer split for new readers.
                currency=period.selling_currency,
                cost_currency=period.cost_currency,
                selling_currency=period.selling_currency,
                rule_key=period.rule_key,
                idempotency_key=period.idempotency_key,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                detail = str(getattr(exc, "orig", exc)).lower()
                if "idempotency" in detail or "uq_accrual" in detail:
                    raise AccrualPeriodExistsError(
                        f"accrual period {period.idempotency_key} already exists"
                    ) from exc
                raise
            except Exception:
                await session.rollback()
                raise
            await session.refresh(row)
            return _to_domain(row)

    async def list_between(self, start: datetime, end: datetime) -> list[AccrualPeriod]:
        async with self._session_factory() as session:
            stmt = (
                select(_AccrualPeriodModel)
                .where(
                    _AccrualPeriodModel.period_start >= start,
                    _AccrualPeriodModel.period_start < end,
                )
                .order_by(_AccrualPeriodModel.period_start.asc())
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_domain(row) for row in rows]

    async def month_total(
        self,
        wallet_id: UUID,
        month_start: datetime,
        currency: str | None = None,
        rule_key: str | None = None,
    ) -> int:
        """Sum selling amounts in one audited customer currency."""
        async with self._session_factory() as session:
            next_month = month_start.replace(
                year=month_start.year + (1 if month_start.month == 12 else 0),
                month=1 if month_start.month == 12 else month_start.month + 1,
            )
            filters = [
                _AccrualPeriodModel.wallet_id == wallet_id,
                _AccrualPeriodModel.period_start >= month_start,
                _AccrualPeriodModel.period_start < next_month,
            ]
            if rule_key is not None:
                normalized_rule = str(rule_key).strip()
                if not normalized_rule:
                    raise ValueError("cap rule identity must be explicit")
                filters.append(
                    or_(
                        _AccrualPeriodModel.rule_key == normalized_rule,
                        _AccrualPeriodModel.rule_key.is_(None),
                    )
                )
            if currency is not None:
                code = str(currency).strip().upper()
                if not code:
                    raise ValueError("cap currency must be explicit")
                filters.append(
                    func.coalesce(
                        _AccrualPeriodModel.selling_currency,
                        _AccrualPeriodModel.currency,
                    )
                    == code
                )
            total = await session.scalar(
                select(func.coalesce(func.sum(_AccrualPeriodModel.selling_minor), 0)).where(
                    *filters
                )
            )
            return int(total or 0)

    async def daily_cost_totals(
        self, day_start: datetime, day_end: datetime, server_ids: frozenset[UUID]
    ) -> dict[str, int]:
        """Provider cost totals grouped by native currency (never mixed)."""
        if not server_ids:
            return {}
        async with self._session_factory() as session:
            cost_currency = func.coalesce(
                _AccrualPeriodModel.cost_currency, _AccrualPeriodModel.currency
            )
            rows = (
                await session.execute(
                    select(cost_currency, func.sum(_AccrualPeriodModel.cost_minor))
                    .where(
                        _AccrualPeriodModel.period_start >= day_start,
                        _AccrualPeriodModel.period_start < day_end,
                        _AccrualPeriodModel.server_id.in_(list(server_ids)),
                    )
                    .group_by(cost_currency)
                )
            ).all()
            return {str(currency).upper(): int(total or 0) for currency, total in rows}

    async def daily_cost_total(
        self,
        day_start: datetime,
        day_end: datetime,
        server_ids: frozenset[UUID],
        currency: str | None = None,
    ) -> int:
        """Sum provider cost for one explicit currency (legacy callers may omit).

        Circuit-breaker callers always pass the limit currency, preventing
        EUR/USD/IRT minor units from being added together.
        """
        if not server_ids:
            return 0
        async with self._session_factory() as session:
            filters = [
                _AccrualPeriodModel.period_start >= day_start,
                _AccrualPeriodModel.period_start < day_end,
                _AccrualPeriodModel.server_id.in_(list(server_ids)),
            ]
            if currency is not None:
                filters.append(
                    func.coalesce(
                        _AccrualPeriodModel.cost_currency,
                        _AccrualPeriodModel.currency,
                    )
                    == currency.strip().upper()
                )
            total = await session.scalar(
                select(func.coalesce(func.sum(_AccrualPeriodModel.cost_minor), 0)).where(*filters)
            )
            return int(total or 0)

    async def daily_cost_exact_total(
        self,
        day_start: datetime,
        day_end: datetime,
        server_ids: frozenset[UUID],
        currency: str,
    ) -> Decimal:
        """Sum exact native cost text; a missing exact amount fails closed."""
        if not server_ids:
            return Decimal(0)
        code = currency.strip().upper()
        async with self._session_factory() as session:
            cost_currency = func.coalesce(
                _AccrualPeriodModel.cost_currency, _AccrualPeriodModel.currency
            )
            rows = (
                (
                    await session.execute(
                        select(_AccrualPeriodModel.cost_amount).where(
                            _AccrualPeriodModel.period_start >= day_start,
                            _AccrualPeriodModel.period_start < day_end,
                            _AccrualPeriodModel.server_id.in_(list(server_ids)),
                            cost_currency == code,
                        )
                    )
                )
                .scalars()
                .all()
            )
        if any(value is None or str(value).strip() == "" for value in rows):
            raise ValueError(
                f"exact provider cost is missing for {code}; refusing rounded breaker total"
            )
        amounts: list[Decimal] = []
        for value in rows:
            amount = Decimal(str(value))
            if not amount.is_finite() or amount < 0:
                raise ValueError(f"invalid exact provider cost for {code}")
            amounts.append(amount)
        precision = max(
            28,
            sum(len(amount.as_tuple().digits) for amount in amounts) + len(str(len(amounts))) + 2,
        )
        with localcontext() as context:
            context.prec = precision
            total = sum(amounts, Decimal(0))
        return total


class PostgresAdvisoryAccrualLock:
    """Accrual run lock backed by a Postgres session-level advisory lock.

    ``pg_try_advisory_lock`` is non-blocking: it returns True when this
    backend acquires the lock and False when another backend holds it, which
    serializes concurrent accrual runs across the whole process pool. The
    SAME session acquires and releases the lock for the whole guarded
    interval; closing the session is a backstop that releases any lock a
    crashed run left behind.
    """

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        key: int = _ACCRUAL_LOCK_KEY,
    ) -> None:
        if (
            isinstance(key, bool)
            or not isinstance(key, int)
            or not (-9_223_372_036_854_775_808 <= key <= 9_223_372_036_854_775_807)
        ):
            raise ValueError("advisory lock key must be a signed int64 integer")
        self._session_factory = session_factory
        self._key = key

    @asynccontextmanager
    async def guard(self) -> AsyncIterator[bool]:
        """Yield True if the lock was acquired; always releases on exit."""
        async with self._session_factory() as session:
            result = await session.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": self._key}
            )
            acquired = bool(result.scalar_one())
            try:
                yield acquired
            finally:
                if acquired:
                    await session.execute(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": self._key}
                    )
