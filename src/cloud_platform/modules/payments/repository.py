"""SQLAlchemy repository adapter for payment sessions."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import PaymentSession as _PaymentModel
from cloud_platform.modules.payments.domain import (
    DuplicateExternalIdError,
    PaymentSession,
    PaymentSessionStatus,
)


def _attr(row: Any, name: str) -> Any:
    """Read a legacy-style Column attribute; typed as Any at the boundary."""
    return getattr(row, name)


def _to_domain(row: _PaymentModel) -> PaymentSession:
    status = PaymentSessionStatus(_attr(row, "status"))
    credited_at = _attr(row, "credited_at")
    created = _attr(row, "created_at")
    updated = _attr(row, "updated_at")
    session = PaymentSession(
        user_id=_attr(row, "user_id"),
        gateway_key=str(_attr(row, "gateway_key")),
        amount_minor=int(_attr(row, "amount_minor")),
        currency=str(_attr(row, "currency")),
        idempotency_key=str(_attr(row, "idempotency_key")),
        id=_attr(row, "id"),
        gateway_payment_id=_attr(row, "gateway_payment_id"),
        status=status,
        created_at=created,
        updated_at=updated,
    )
    if credited_at is not None:
        session = session.mark_credited(at=credited_at)
    return session


def _to_row(aggregate: PaymentSession) -> _PaymentModel:
    return _PaymentModel(
        user_id=aggregate.user_id,
        gateway_key=aggregate.gateway_key,
        gateway_payment_id=aggregate.gateway_payment_id,
        amount_minor=aggregate.amount_minor,
        currency=aggregate.currency,
        status=aggregate.status.value,
        idempotency_key=aggregate.idempotency_key,
        credited_at=aggregate.credited_at,
    )


class SqlAlchemyPaymentSessionRepository:
    """Durable storage for payment sessions with unique external ids."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def create(self, session: PaymentSession) -> PaymentSession:
        row = _to_row(session)
        async with self._session_factory() as db:
            db.add(row)
            try:
                await db.commit()
            except IntegrityError as exc:
                await db.rollback()
                raise DuplicateExternalIdError(
                    f"payment already exists for "
                    f"({session.gateway_key}, {session.gateway_payment_id})"
                ) from exc
            await db.refresh(row)
            return _to_domain(row)

    async def get(self, session_id: UUID) -> PaymentSession | None:
        async with self._session_factory() as db:
            result = await db.execute(select(_PaymentModel).where(_PaymentModel.id == session_id))
            row = result.scalar_one_or_none()
            return None if row is None else _to_domain(row)

    async def get_by_external_id(
        self, gateway_key: str, gateway_payment_id: str
    ) -> PaymentSession | None:
        async with self._session_factory() as db:
            stmt = select(_PaymentModel).where(
                _PaymentModel.gateway_key == gateway_key,
                _PaymentModel.gateway_payment_id == gateway_payment_id,
            )
            result = await db.execute(stmt)
            row = result.scalar_one_or_none()
            return None if row is None else _to_domain(row)

    async def save(self, session: PaymentSession) -> PaymentSession:
        assert session.id is not None  # only persisted sessions can be saved
        session_id: UUID = session.id
        async with self._session_factory() as db:
            result = await db.execute(select(_PaymentModel).where(_PaymentModel.id == session_id))
            row = result.scalar_one_or_none()
            if row is None:
                raise LookupError(f"payment session {session_id} not found")
            cast_any: Any = row
            cast_any.status = session.status.value
            cast_any.gateway_payment_id = session.gateway_payment_id
            cast_any.credited_at = session.credited_at
            await db.commit()
            await db.refresh(row)
            return _to_domain(row)
