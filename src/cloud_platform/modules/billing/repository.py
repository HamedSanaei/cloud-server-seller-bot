"""SQLAlchemy persistence for accrual-period records (M06-005)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import AccrualPeriod as _AccrualPeriodModel
from cloud_platform.modules.billing.service import AccrualPeriod, AccrualPeriodExistsError

_ACCRUAL_LOCK_KEY = 777_005


def _attr(row: Any, name: str) -> Any:
    return getattr(row, name)


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
    return AccrualPeriod(
        id=_attr(row, "id"),
        server_id=_attr(row, "server_id"),
        wallet_id=_attr(row, "wallet_id"),
        period_start=period_start,
        period_end=period_end,
        quanta=int(_attr(row, "quanta")),
        cost_minor=int(_attr(row, "cost_minor")),
        selling_minor=int(_attr(row, "selling_minor")),
        currency=str(_attr(row, "currency")),
        idempotency_key=str(_attr(row, "idempotency_key")),
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
                selling_minor=period.selling_minor,
                currency=period.currency,
                idempotency_key=period.idempotency_key,
            )
            session.add(row)
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                raise AccrualPeriodExistsError(
                    f"accrual period {period.idempotency_key} already exists"
                ) from None
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

    async def month_total(self, wallet_id: UUID, month_start: datetime) -> int:
        """Sum of selling_minor billed to the wallet at/after month_start."""
        async with self._session_factory() as session:
            total = await session.scalar(
                select(func.coalesce(func.sum(_AccrualPeriodModel.selling_minor), 0)).where(
                    _AccrualPeriodModel.wallet_id == wallet_id,
                    _AccrualPeriodModel.period_start >= month_start,
                )
            )
            return int(total or 0)

    async def daily_cost_total(
        self, day_start: datetime, day_end: datetime, server_ids: frozenset[UUID]
    ) -> int:
        """Sum of provider cost (cost_minor) accrued in [day_start, day_end).

        Empty ``server_ids`` yields 0 (no servers in the scope = no spend).
        """
        if not server_ids:
            return 0
        async with self._session_factory() as session:
            total = await session.scalar(
                select(func.coalesce(func.sum(_AccrualPeriodModel.cost_minor), 0)).where(
                    _AccrualPeriodModel.period_start >= day_start,
                    _AccrualPeriodModel.period_start < day_end,
                    _AccrualPeriodModel.server_id.in_(list(server_ids)),
                )
            )
            return int(total or 0)


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
