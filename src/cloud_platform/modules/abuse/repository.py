"""SQLAlchemy adapters for the abuse workflow.

``SqlAlchemyOwnershipResolver`` is the "quick map" half of M10-006: it turns a
reported provider resource (server id / IPv4 / IPv6) into the responsible
user by querying the ``servers`` table. ``SqlAlchemyAbuseCaseRepository``
persists the audited case itself.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import AbuseCase as _AbuseModel
from cloud_platform.db.base import Provider as _ProviderModel
from cloud_platform.db.base import Server as _ServerModel
from cloud_platform.modules.abuse.domain import (
    AbuseCase,
    AbuseStatus,
    Ownership,
    ResourceRef,
    ResourceType,
)


def _attr(row: Any, name: str) -> Any:
    """Read a legacy-style Column attribute; typed as Any at the boundary."""
    return getattr(row, name)


def _to_domain(row: _AbuseModel) -> AbuseCase:
    resource = ResourceRef(
        provider_key=str(_attr(row, "provider_key")),
        resource_type=ResourceType(_attr(row, "resource_type")),
        resource_id=str(_attr(row, "resource_id")),
    )
    return AbuseCase(
        resource=resource,
        user_id=_attr(row, "user_id"),
        server_id=_attr(row, "server_id"),
        reason=str(_attr(row, "reason")),
        reporter=str(_attr(row, "reporter")),
        status=AbuseStatus(_attr(row, "status")),
        id=_attr(row, "id"),
        created_at=_attr(row, "created_at"),
        updated_at=_attr(row, "updated_at"),
        resolved_at=_attr(row, "resolved_at"),
    )


def _to_row(case: AbuseCase) -> _AbuseModel:
    return _AbuseModel(
        provider_key=case.resource.provider_key,
        resource_type=case.resource.resource_type.value,
        resource_id=case.resource.resource_id,
        user_id=case.user_id,
        server_id=case.server_id,
        reason=case.reason,
        reporter=case.reporter,
        status=case.status.value,
        created_at=case.created_at,
        updated_at=case.updated_at,
        resolved_at=case.resolved_at,
    )


class SqlAlchemyOwnershipResolver:
    """Resolves a reported provider resource to the responsible user."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def resolve(self, ref: ResourceRef) -> Ownership | None:
        async with self._session_factory() as session:
            provider = (
                (
                    await session.execute(
                        select(_ProviderModel).where(_ProviderModel.name == ref.provider_key)
                    )
                )
                .scalars()
                .first()
            )
            if provider is None:
                return None

            stmt = select(_ServerModel).where(_ServerModel.provider_id == provider.id)
            if ref.resource_type is ResourceType.PROVIDER_SERVER:
                stmt = stmt.where(_ServerModel.provider_server_id == ref.resource_id)
            elif ref.resource_type is ResourceType.IPV4:
                stmt = stmt.where(_ServerModel.ipv4 == ref.resource_id)
            else:  # IPV6
                stmt = stmt.where(_ServerModel.ipv6 == ref.resource_id)

            row = (await session.execute(stmt)).scalars().first()
            if row is None:
                return None
            return Ownership(user_id=_attr(row, "user_id"), server_id=_attr(row, "id"))


class SqlAlchemyAbuseCaseRepository:
    """Durable storage for abuse cases."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def create(self, case: AbuseCase) -> AbuseCase:
        row = _to_row(case)
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def get(self, case_id: UUID) -> AbuseCase | None:
        async with self._session_factory() as session:
            row = (
                (await session.execute(select(_AbuseModel).where(_AbuseModel.id == case_id)))
                .scalars()
                .first()
            )
            return None if row is None else _to_domain(row)

    async def get_by_user(self, user_id: UUID) -> list[AbuseCase]:
        async with self._session_factory() as session:
            stmt = (
                select(_AbuseModel)
                .where(_AbuseModel.user_id == user_id)
                .order_by(_AbuseModel.created_at.desc())
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_domain(row) for row in rows]

    async def list_open(self) -> list[AbuseCase]:
        async with self._session_factory() as session:
            open_statuses = [AbuseStatus.OPEN.value, AbuseStatus.INVESTIGATING.value]
            stmt = (
                select(_AbuseModel)
                .where(_AbuseModel.status.in_(open_statuses))
                .order_by(_AbuseModel.created_at.asc())
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_domain(row) for row in rows]

    async def save(self, case: AbuseCase) -> AbuseCase:
        assert case.id is not None  # only persisted cases can be saved
        case_id: UUID = case.id
        async with self._session_factory() as session:
            row = (
                (await session.execute(select(_AbuseModel).where(_AbuseModel.id == case_id)))
                .scalars()
                .first()
            )
            if row is None:
                raise LookupError(f"abuse case {case_id} not found")
            cast_any: Any = row
            cast_any.status = case.status.value
            cast_any.updated_at = case.updated_at
            cast_any.resolved_at = case.resolved_at
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)
