"""SQLAlchemy adapter for the CloudServer aggregate (M10-007).

The ``servers`` table stores the provider as a UUID foreign key, while the
domain aggregate carries the provider *name* key, so mappings join
``providers`` to resolve it.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import Catalog as _CatalogModel
from cloud_platform.db.base import MaintenanceBlock as _MaintenanceBlockModel
from cloud_platform.db.base import Provider as _ProviderModel
from cloud_platform.db.base import Server as _ServerModel
from cloud_platform.modules.compute.domain import (
    CloudServer,
    MaintenanceBlock,
    MaintenanceScope,
    ProvisioningSpec,
    ServerCreateError,
    ServerCreateIntent,
    ServerLifecycleState,
)


def _attr(row: Any, name: str) -> Any:
    """Read a legacy-style Column attribute; typed as Any at the boundary."""
    return getattr(row, name)


def _state_or_none(value: Any) -> ServerLifecycleState | None:
    if value is None:
        return None
    return ServerLifecycleState(value)


def _aware_or_none(value: Any) -> datetime | None:
    """DB timestamps are naive UTC; normalize to aware."""
    if value is None:
        return None
    dt: datetime = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _to_domain(row: _ServerModel, provider_name: str) -> CloudServer:
    return CloudServer(
        id=_attr(row, "id"),
        user_id=_attr(row, "user_id"),
        provider_key=provider_name,
        provider_account_id=_attr(row, "provider_account_id"),
        state=ServerLifecycleState(_attr(row, "state")),
        provider_server_id=_attr(row, "provider_server_id"),
        contained_from=_state_or_none(_attr(row, "contained_from")),
        idempotency_key=_attr(row, "idempotency_key"),
        created_at=_aware_or_none(_attr(row, "created_at")),
        last_accrued_at=_aware_or_none(_attr(row, "last_accrued_at")),
        deleted_at=_aware_or_none(_attr(row, "deleted_at")),
        low_balance_since=_aware_or_none(_attr(row, "low_balance_since")),
        quantum_seconds=int(_attr(row, "quantum_seconds") or 3600),
    )


def _server_stmt() -> Any:
    """SELECT server row + provider name for a server."""
    return select(_ServerModel, _ProviderModel.name).join(
        _ProviderModel, _ServerModel.provider_id == _ProviderModel.id
    )


class SqlAlchemyServerRepository:
    """Durable storage for CloudServer aggregates."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def get(self, server_id: UUID) -> CloudServer | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(_server_stmt().where(_ServerModel.id == server_id))
            ).first()
            if row is None:
                return None
            server_row, provider_name = row
            return _to_domain(server_row, str(provider_name))

    async def list_by_user(self, user_id: UUID) -> list[CloudServer]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(_server_stmt().where(_ServerModel.user_id == user_id))
            ).all()
            return [_to_domain(server_row, str(name)) for server_row, name in rows]

    async def list_by_user_paged(
        self, user_id: UUID, *, offset: int, limit: int
    ) -> tuple[list[CloudServer], int]:
        base = _server_stmt().where(_ServerModel.user_id == user_id)
        async with self._session_factory() as session:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(_ServerModel)
                    .where(_ServerModel.user_id == user_id)
                )
            ).scalar_one()
            rows = (
                await session.execute(
                    base.order_by(
                        _ServerModel.created_at.desc().nulls_last(),
                        _ServerModel.id.desc(),
                    )
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
        return (
            [_to_domain(server_row, str(name)) for server_row, name in rows],
            int(count),
        )

    async def count_active(self, user_id: UUID) -> int:
        async with self._session_factory() as session:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(_ServerModel)
                    .where(
                        _ServerModel.user_id == user_id,
                        _ServerModel.state != ServerLifecycleState.DELETED.value,
                    )
                )
            ).scalar_one()
            return int(count)

    async def count_total(self, user_id: UUID) -> int:
        async with self._session_factory() as session:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(_ServerModel)
                    .where(_ServerModel.user_id == user_id)
                )
            ).scalar_one()
            return int(count)

    async def list_requested(self) -> list[CloudServer]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    _server_stmt().where(_ServerModel.state == ServerLifecycleState.REQUESTED.value)
                )
            ).all()
            return [_to_domain(server_row, str(name)) for server_row, name in rows]

    async def list_provisioning(self) -> list[CloudServer]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    _server_stmt().where(
                        _ServerModel.state == ServerLifecycleState.PROVISIONING.value
                    )
                )
            ).all()
            return [_to_domain(server_row, str(name)) for server_row, name in rows]

    async def list_running(self) -> list[CloudServer]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    _server_stmt().where(_ServerModel.state == ServerLifecycleState.RUNNING.value)
                )
            ).all()
            return [_to_domain(server_row, str(name)) for server_row, name in rows]

    async def list_stopped(self) -> list[CloudServer]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    _server_stmt().where(_ServerModel.state == ServerLifecycleState.STOPPED.value)
                )
            ).all()
            return [_to_domain(server_row, str(name)) for server_row, name in rows]

    async def list_manual_review(self) -> list[CloudServer]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    _server_stmt().where(
                        _ServerModel.state == ServerLifecycleState.MANUAL_REVIEW.value
                    )
                )
            ).all()
            return [_to_domain(server_row, str(name)) for server_row, name in rows]

    async def list_deletion_in_progress(self) -> list[CloudServer]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    _server_stmt().where(
                        _ServerModel.state.in_(
                            [
                                ServerLifecycleState.DELETE_REQUESTED.value,
                                ServerLifecycleState.DELETING.value,
                            ]
                        )
                    )
                )
            ).all()
            return [_to_domain(server_row, str(name)) for server_row, name in rows]

    async def get_provisioning_spec(self, server_id: UUID) -> ProvisioningSpec | None:
        """Resolve the plan/location/currency the server's catalog offer maps to."""
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(
                        _CatalogModel.provider_plan_id,
                        _CatalogModel.provider_location_id,
                        _CatalogModel.currency,
                    )
                    .join(_ServerModel, _ServerModel.catalog_id == _CatalogModel.id)
                    .where(_ServerModel.id == server_id)
                )
            ).first()
            if row is None:
                return None
            plan_id, location_id, currency = row
            return ProvisioningSpec(
                plan_id=str(plan_id), location_id=str(location_id), currency=str(currency)
            )

    async def save(self, server: CloudServer) -> CloudServer:
        server_id: UUID = server.id
        async with self._session_factory() as session:
            row = (
                await session.execute(_server_stmt().where(_ServerModel.id == server_id))
            ).first()
            if row is None:
                raise LookupError(f"server {server_id} not found")
            server_row, provider_name = row
            cast_any: Any = server_row
            cast_any.state = server.state.value
            cast_any.contained_from = (
                server.contained_from.value if server.contained_from is not None else None
            )
            cast_any.provider_server_id = server.provider_server_id
            cast_any.last_accrued_at = server.last_accrued_at
            cast_any.deleted_at = server.deleted_at
            cast_any.low_balance_since = server.low_balance_since
            await session.commit()
            await session.refresh(server_row)
            return _to_domain(server_row, str(provider_name))

    async def create(self, server: CloudServer, intent: ServerCreateIntent) -> CloudServer:
        """Persist a new REQUESTED server row with its create intent.

        Raises:
            LookupError: If the provider is unknown.
            ServerCreateError: On a constraint violation (e.g. idempotency
                key already consumed), which makes retries fail loudly.
        """
        async with self._session_factory() as session:
            provider = (
                (
                    await session.execute(
                        select(_ProviderModel).where(_ProviderModel.name == server.provider_key)
                    )
                )
                .scalars()
                .first()
            )
            if provider is None:
                raise LookupError(f"provider {server.provider_key!r} not found")

            row = _ServerModel(
                id=server.id,
                user_id=server.user_id,
                provider_id=provider.id,
                provider_account_id=server.provider_account_id,
                catalog_id=intent.catalog_id,
                state=server.state.value,
                price_per_quantum=intent.cost_minor,
                currency=intent.currency,
                idempotency_key=intent.idempotency_key,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ServerCreateError(
                    f"could not persist create intent "
                    f"(idempotency key {intent.idempotency_key!r} may already be consumed)"
                ) from exc
            await session.refresh(row)
            return _to_domain(row, server.provider_key)

    async def get_by_idempotency_key(self, idempotency_key: str) -> CloudServer | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    _server_stmt().where(_ServerModel.idempotency_key == idempotency_key)
                )
            ).first()
            if row is None:
                return None
            server_row, provider_name = row
            return _to_domain(server_row, str(provider_name))


class SqlAlchemyMaintenanceSwitchRepository:
    """Durable storage for maintenance switches (M10-005).

    A provider-wide switch stores an empty ``location_id`` so the unique
    scope constraint works for both switch kinds.
    """

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _to_domain(row: _MaintenanceBlockModel) -> MaintenanceBlock:
        location_id = str(row.location_id) if row.location_id else None
        return MaintenanceBlock(
            scope=MaintenanceScope(provider_key=str(row.provider_key), location_id=location_id),
            reason=str(row.reason),
            created_by=_attr(row, "created_by"),
            created_at=_attr(row, "created_at"),
            updated_at=_aware_or_none(_attr(row, "updated_at")),
        )

    async def list_blocks(self) -> list[MaintenanceBlock]:
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(_MaintenanceBlockModel).order_by(
                            _MaintenanceBlockModel.created_at,
                            _MaintenanceBlockModel.provider_key,
                        )
                    )
                )
                .scalars()
                .all()
            )
            return [self._to_domain(row) for row in rows]

    async def save_block(self, block: MaintenanceBlock) -> MaintenanceBlock:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(_MaintenanceBlockModel).where(
                        _MaintenanceBlockModel.provider_key == block.scope.provider_key,
                        _MaintenanceBlockModel.location_id == (block.scope.location_id or ""),
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = _MaintenanceBlockModel(
                    provider_key=block.scope.provider_key,
                    location_id=block.scope.location_id or "",
                    reason=block.reason,
                    created_by=block.created_by,
                )
                session.add(row)
            else:
                cast_any: Any = row
                cast_any.reason = block.reason
                cast_any.created_by = block.created_by
            await session.commit()
            await session.refresh(row)
            return self._to_domain(row)

    async def remove_block(self, scope: MaintenanceScope) -> bool:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(_MaintenanceBlockModel).where(
                        _MaintenanceBlockModel.provider_key == scope.provider_key,
                        _MaintenanceBlockModel.location_id == (scope.location_id or ""),
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True
