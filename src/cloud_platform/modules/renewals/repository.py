"""SQLAlchemy adapters for the renewals ports (LEASEWEB-MVP)."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import RenewalNotification as _RenewalNotificationModel
from cloud_platform.db.base import RenewalRecord as _RenewalRecordModel
from cloud_platform.modules.renewals.domain import (
    RenewalKind,
    RenewalRecord,
    RenewalStatus,
)


def _attr(row: Any, name: str) -> Any:
    return getattr(row, name)


def _aware_or_none(value: Any) -> datetime | None:
    if value is None:
        return None
    dt: datetime = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _to_domain(row: _RenewalRecordModel) -> RenewalRecord:
    return RenewalRecord(
        server_id=_attr(row, "server_id"),
        provider_contract_id=_attr(row, "provider_contract_id"),
        provider_order_ref=_attr(row, "provider_order_ref"),
        purchased_at=_aware_or_none(_attr(row, "purchased_at")) or datetime.now(UTC),
        provider_renewal_at=_aware_or_none(_attr(row, "provider_renewal_at")),
        renewal_date_estimated=bool(_attr(row, "renewal_date_estimated")),
        customer_price_minor=int(_attr(row, "customer_price_minor")),
        currency=str(_attr(row, "currency")),
        status=RenewalStatus(str(_attr(row, "status"))),
        auto_charge_enabled=bool(_attr(row, "auto_charge_enabled")),
        last_checked_at=_aware_or_none(_attr(row, "last_checked_at")),
    )


class SqlAlchemyRenewalRepository:
    """Durable storage for renewal records (1:1 with servers)."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def get(self, server_id: UUID) -> RenewalRecord | None:
        async with self._session_factory() as session:
            row = await session.get(_RenewalRecordModel, server_id)
            return _to_domain(row) if row is not None else None

    async def upsert(self, record: RenewalRecord) -> RenewalRecord:
        async with self._session_factory() as session:
            row = await session.get(_RenewalRecordModel, record.server_id)
            if row is None:
                row = _RenewalRecordModel(
                    server_id=record.server_id,
                    provider_contract_id=record.provider_contract_id,
                    provider_order_ref=record.provider_order_ref,
                    purchased_at=record.purchased_at,
                    provider_renewal_at=record.provider_renewal_at,
                    renewal_date_estimated=record.renewal_date_estimated,
                    customer_price_minor=record.customer_price_minor,
                    currency=record.currency,
                    status=record.status.value,
                    auto_charge_enabled=record.auto_charge_enabled,
                )
                session.add(row)
            else:
                cast_any: Any = row
                cast_any.provider_contract_id = record.provider_contract_id
                cast_any.provider_order_ref = record.provider_order_ref
                cast_any.purchased_at = record.purchased_at
                cast_any.provider_renewal_at = record.provider_renewal_at
                cast_any.renewal_date_estimated = record.renewal_date_estimated
                cast_any.customer_price_minor = record.customer_price_minor
                cast_any.currency = record.currency
                cast_any.status = record.status.value
                cast_any.auto_charge_enabled = record.auto_charge_enabled
                cast_any.last_checked_at = record.last_checked_at
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def list_active(self) -> list[RenewalRecord]:
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(_RenewalRecordModel).where(
                            _RenewalRecordModel.status != RenewalStatus.CANCELLED.value
                        )
                    )
                )
                .scalars()
                .all()
            )
            return [_to_domain(row) for row in rows]

    async def list_needing_attention(self, limit: int = 100) -> list[RenewalRecord]:
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(_RenewalRecordModel)
                        .where(
                            _RenewalRecordModel.status.in_(
                                [
                                    RenewalStatus.INSUFFICIENT_FUNDS.value,
                                    RenewalStatus.MANUAL_CANCELLATION_REQUIRED.value,
                                ]
                            )
                        )
                        .order_by(_RenewalRecordModel.provider_renewal_at)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [_to_domain(row) for row in rows]


class SqlAlchemyRenewalNotificationRepository:
    """Exactly-once renewal notification log (unique server/kind/period)."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def record(self, server_id: UUID, kind: RenewalKind, for_period: datetime) -> bool:
        if for_period.tzinfo is None:
            for_period = for_period.replace(tzinfo=UTC)
        async with self._session_factory() as session:
            row = _RenewalNotificationModel(
                server_id=server_id,
                kind=kind.value,
                for_period=for_period,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return False
            return True
