"""SQLAlchemy adapter for the operation ledger (M07-002)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import Operation as _OperationModel
from cloud_platform.modules.operations.domain import (
    Operation,
    OperationStatus,
    OperationType,
)


def _attr(row: Any, name: str) -> Any:
    """Read a legacy-style Column attribute; typed as Any at the boundary."""
    return getattr(row, name)


def _to_domain(row: _OperationModel) -> Operation:
    return Operation(
        id=_attr(row, "id"),
        operation_key=str(_attr(row, "operation_key")),
        operation_type=OperationType(str(_attr(row, "operation_type"))),
        resource_type=str(_attr(row, "resource_type")),
        resource_id=_attr(row, "resource_id"),
        provider_key=str(_attr(row, "provider_key")),
        status=OperationStatus(str(_attr(row, "status"))),
        provider_response=_attr(row, "provider_response"),
        error=_attr(row, "error"),
        attempts=int(_attr(row, "attempts")),
        created_at=_attr(row, "created_at"),
        updated_at=_attr(row, "updated_at"),
    )


class SqlAlchemyOperationRepository:
    """Durable operation ledger with atomic claims."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def get_or_create(
        self,
        *,
        operation_key: str,
        operation_type: OperationType,
        resource_type: str,
        resource_id: UUID,
        provider_key: str,
    ) -> Operation:
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(_OperationModel).where(
                            _OperationModel.operation_key == operation_key
                        )
                    )
                )
                .scalars()
                .first()
            )
            if row is not None:
                return _to_domain(row)
            row = _OperationModel(
                operation_key=operation_key,
                operation_type=operation_type.value,
                resource_type=resource_type,
                resource_id=resource_id,
                provider_key=provider_key,
                status=OperationStatus.PENDING.value,
                attempts=0,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                # A concurrent creator won; the unique key resolves it.
                await session.rollback()
                row = (
                    (
                        await session.execute(
                            select(_OperationModel).where(
                                _OperationModel.operation_key == operation_key
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                assert row is not None  # the constraint guarantees a winner
                return _to_domain(row)
            await session.refresh(row)
            return _to_domain(row)

    async def get(self, operation_id: UUID) -> Operation | None:
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(_OperationModel).where(_OperationModel.id == operation_id)
                    )
                )
                .scalars()
                .first()
            )
            return None if row is None else _to_domain(row)

    async def get_by_key(self, operation_key: str) -> Operation | None:
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(_OperationModel).where(
                            _OperationModel.operation_key == operation_key
                        )
                    )
                )
                .scalars()
                .first()
            )
            return None if row is None else _to_domain(row)

    async def list_in_flight(self, operation_type: OperationType) -> list[Operation]:
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(_OperationModel).where(
                            _OperationModel.operation_type == operation_type.value,
                            _OperationModel.status == OperationStatus.IN_FLIGHT.value,
                        )
                    )
                )
                .scalars()
                .all()
            )
            return [_to_domain(row) for row in rows]

    async def list_pending(self, operation_types: Sequence[OperationType]) -> list[Operation]:
        async with self._session_factory() as session:
            values = [t.value for t in operation_types]
            rows = (
                (
                    await session.execute(
                        select(_OperationModel).where(
                            _OperationModel.operation_type.in_(values),
                            _OperationModel.status == OperationStatus.PENDING.value,
                        )
                    )
                )
                .scalars()
                .all()
            )
            return [_to_domain(row) for row in rows]

    async def claim(self, operation_id: UUID) -> Operation | None:
        """Atomically claim a PENDING operation; None when the race is lost."""
        async with self._session_factory() as session:
            result = await session.execute(
                update(_OperationModel)
                .where(
                    _OperationModel.id == operation_id,
                    _OperationModel.status == OperationStatus.PENDING.value,
                )
                .values(
                    status=OperationStatus.IN_FLIGHT.value,
                    attempts=_OperationModel.attempts + 1,
                    updated_at=datetime.now(UTC),
                )
            )
            rowcount: int = cast(Any, result).rowcount
            if rowcount != 1:
                return None
            await session.commit()
            row = (
                (
                    await session.execute(
                        select(_OperationModel).where(_OperationModel.id == operation_id)
                    )
                )
                .scalars()
                .first()
            )
            assert row is not None
            return _to_domain(row)

    async def save(self, operation: Operation) -> Operation:
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(_OperationModel).where(_OperationModel.id == operation.id)
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                raise LookupError(f"operation {operation.id} not found")
            cast_any: Any = row
            cast_any.status = operation.status.value
            cast_any.provider_response = operation.provider_response
            cast_any.error = operation.error
            cast_any.attempts = operation.attempts
            cast_any.updated_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def list_failed(
        self,
        *,
        operation_types: Sequence[OperationType] | None = None,
        limit: int = 50,
    ) -> list[Operation]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        async with self._session_factory() as session:
            stmt = select(_OperationModel).where(
                _OperationModel.status == OperationStatus.FAILED.value
            )
            if operation_types is not None:
                stmt = stmt.where(
                    _OperationModel.operation_type.in_([t.value for t in operation_types])
                )
            rows = (
                (
                    await session.execute(
                        stmt.order_by(_OperationModel.updated_at.desc()).limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [_to_domain(row) for row in rows]
