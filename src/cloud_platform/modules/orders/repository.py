"""SQLAlchemy adapter for the provider-order port (LEASEWEB-MVP)."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import ProviderOrder as _ProviderOrderModel
from cloud_platform.modules.orders.domain import (
    OrderStatus,
    ProviderOrder,
    ProviderOrderRepository,
    SettlementStatus,
)


def _attr(row: Any, name: str) -> Any:
    return getattr(row, name)


def _to_domain(row: _ProviderOrderModel) -> ProviderOrder:
    return ProviderOrder(
        id=_attr(row, "id"),
        server_id=_attr(row, "server_id"),
        operation_key=str(_attr(row, "operation_key")),
        provider_key=str(_attr(row, "provider_key")),
        offer_id=_attr(row, "offer_id"),
        status=OrderStatus(str(_attr(row, "status"))),
        provider_order_id=_attr(row, "provider_order_id"),
        delivery_estimate=_attr(row, "delivery_estimate"),
        provider_contract_id=_attr(row, "provider_contract_id"),
        provider_service_id=_attr(row, "provider_service_id"),
        error=_attr(row, "error"),
        attempts=int(_attr(row, "attempts") or 0),
        last_polled_at=_attr(row, "last_polled_at"),
        created_at=_attr(row, "created_at"),
        updated_at=_attr(row, "updated_at"),
        product_id=_attr(row, "product_id"),
        location_id=_attr(row, "location_id"),
        os_name=_attr(row, "os_name"),
        contract_term=_attr(row, "contract_term"),
        billing_cycle=_attr(row, "billing_cycle"),
        provider_cost_minor=_attr(row, "provider_cost_minor"),
        provider_cost_currency=_attr(row, "provider_cost_currency"),
        selling_price_minor=_attr(row, "selling_price_minor"),
        selling_currency=_attr(row, "selling_currency"),
        post_attempted_at=_attr(row, "post_attempted_at"),
        settlement_status=SettlementStatus(str(_attr(row, "settlement_status") or "pending")),
        settlement_attempted_at=_attr(row, "settlement_attempted_at"),
        settlement_attempts=int(_attr(row, "settlement_attempts") or 0),
        settlement_error=_attr(row, "settlement_error"),
    )


class SqlAlchemyProviderOrderRepository(ProviderOrderRepository):
    """Durable storage for provider orders."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def get(self, order_id: UUID) -> ProviderOrder | None:
        async with self._session_factory() as session:
            row = await session.get(_ProviderOrderModel, order_id)
            return _to_domain(row) if row is not None else None

    async def get_by_server(self, server_id: UUID) -> ProviderOrder | None:
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(_ProviderOrderModel).where(
                            _ProviderOrderModel.server_id == server_id
                        )
                    )
                )
                .scalars()
                .first()
            )
            return _to_domain(row) if row is not None else None

    async def get_by_operation_key(self, operation_key: str) -> ProviderOrder | None:
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(_ProviderOrderModel).where(
                            _ProviderOrderModel.operation_key == operation_key
                        )
                    )
                )
                .scalars()
                .first()
            )
            return _to_domain(row) if row is not None else None

    async def create(
        self,
        *,
        server_id: UUID,
        operation_key: str,
        provider_key: str,
        offer_id: UUID,
        product_id: str | None = None,
        location_id: str | None = None,
        os_name: str | None = None,
        contract_term: str | None = None,
        billing_cycle: str | None = None,
        provider_cost_minor: int | None = None,
        provider_cost_currency: str | None = None,
        selling_price_minor: int | None = None,
        selling_currency: str | None = None,
    ) -> ProviderOrder:
        async with self._session_factory() as session:
            row = _ProviderOrderModel(
                server_id=server_id,
                operation_key=operation_key,
                provider_key=provider_key,
                offer_id=offer_id,
                status=OrderStatus.PENDING_SUBMIT.value,
                product_id=product_id,
                location_id=location_id,
                os_name=os_name,
                contract_term=contract_term,
                billing_cycle=billing_cycle,
                provider_cost_minor=provider_cost_minor,
                provider_cost_currency=provider_cost_currency,
                selling_price_minor=selling_price_minor,
                selling_currency=selling_currency,
            )
            session.add(row)
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                existing = (
                    (
                        await session.execute(
                            select(_ProviderOrderModel).where(
                                _ProviderOrderModel.server_id == server_id
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if existing is not None:
                    return _to_domain(existing)
                raise
            await session.refresh(row)
            return _to_domain(row)

    async def save(self, order: ProviderOrder) -> ProviderOrder:
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(_ProviderOrderModel).where(_ProviderOrderModel.id == order.id)
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                raise LookupError(f"provider order {order.id} not found")
            cast_any: Any = row
            cast_any.status = order.status.value
            cast_any.provider_order_id = order.provider_order_id
            cast_any.delivery_estimate = order.delivery_estimate
            cast_any.provider_contract_id = order.provider_contract_id
            cast_any.provider_service_id = order.provider_service_id
            cast_any.error = order.error
            cast_any.attempts = order.attempts
            cast_any.product_id = order.product_id
            cast_any.location_id = order.location_id
            cast_any.os_name = order.os_name
            cast_any.contract_term = order.contract_term
            cast_any.billing_cycle = order.billing_cycle
            cast_any.provider_cost_minor = order.provider_cost_minor
            cast_any.provider_cost_currency = order.provider_cost_currency
            cast_any.selling_price_minor = order.selling_price_minor
            cast_any.selling_currency = order.selling_currency
            cast_any.post_attempted_at = order.post_attempted_at
            cast_any.settlement_status = order.settlement_status.value
            cast_any.settlement_attempted_at = order.settlement_attempted_at
            cast_any.settlement_attempts = order.settlement_attempts
            cast_any.settlement_error = order.settlement_error
            cast_any.updated_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def _list(
        self, session: AsyncSession, statuses: tuple[OrderStatus, ...], limit: int
    ) -> list[ProviderOrder]:
        rows = (
            (
                await session.execute(
                    select(_ProviderOrderModel)
                    .where(_ProviderOrderModel.status.in_([s.value for s in statuses]))
                    .order_by(_ProviderOrderModel.created_at)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_to_domain(row) for row in rows]

    async def list_open(self, provider_key: str, limit: int = 100) -> list[ProviderOrder]:
        del provider_key
        async with self._session_factory() as session:
            return list(
                await self._list(session, (OrderStatus.SUBMITTED, OrderStatus.PROVISIONING), limit)
            )

    async def list_by_status(
        self, provider_key: str, status: OrderStatus, limit: int = 100
    ) -> list[ProviderOrder]:
        del provider_key
        async with self._session_factory() as session:
            return list(await self._list(session, (status,), limit))

    async def list_failed(self, provider_key: str, limit: int = 50) -> list[ProviderOrder]:
        del provider_key
        async with self._session_factory() as session:
            return list(await self._list(session, (OrderStatus.FAILED,), limit))

    async def list_needs_review(self, provider_key: str, limit: int = 50) -> list[ProviderOrder]:
        del provider_key
        async with self._session_factory() as session:
            return list(await self._list(session, (OrderStatus.NEEDS_REVIEW,), limit))

    async def list_outcome_unknown(self, provider_key: str, limit: int = 50) -> list[ProviderOrder]:
        del provider_key
        async with self._session_factory() as session:
            return list(await self._list(session, (OrderStatus.OUTCOME_UNKNOWN,), limit))

    async def list_attention(self, provider_key: str, limit: int = 100) -> list[ProviderOrder]:
        del provider_key
        async with self._session_factory() as session:
            return list(
                await self._list(
                    session,
                    (OrderStatus.FAILED, OrderStatus.NEEDS_REVIEW, OrderStatus.OUTCOME_UNKNOWN),
                    limit,
                )
            )
