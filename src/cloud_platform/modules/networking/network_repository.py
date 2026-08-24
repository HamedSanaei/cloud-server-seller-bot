"""Persistence for user-owned private networks (M13-010)."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import NetworkRow
from cloud_platform.modules.networking.networks import Network


def _attr(row: Any, name: str) -> Any:
    return getattr(row, name)


def _to_domain(row: NetworkRow) -> Network:
    return Network(
        user_id=_attr(row, "user_id"),
        provider_account_id=_attr(row, "provider_account_id"),
        provider_key=_attr(row, "provider_key"),
        provider_network_id=_attr(row, "provider_network_id"),
        name=_attr(row, "name"),
        ip_range=_attr(row, "ip_range"),
        server_ids=tuple(UUID(s) for s in (_attr(row, "server_ids") or [])),
        location_id=_attr(row, "location_id"),
        id=_attr(row, "id"),
        created_at=_attr(row, "created_at"),
    )


class SqlAlchemyNetworkRepository:
    """SQLAlchemy-backed private-network store."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def add(self, network: Network) -> Network:
        row = NetworkRow(
            user_id=network.user_id,
            provider_account_id=network.provider_account_id,
            provider_key=network.provider_key,
            provider_network_id=network.provider_network_id,
            name=network.name,
            ip_range=network.ip_range,
            server_ids=[str(sid) for sid in network.server_ids],
            location_id=network.location_id,
        )
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def get(self, network_id: UUID) -> Network | None:
        async with self._session_factory() as session:
            row = await session.get(NetworkRow, network_id)
            return None if row is None else _to_domain(row)

    async def list_for_user(self, user_id: UUID) -> list[Network]:
        stmt = (
            select(NetworkRow).where(NetworkRow.user_id == user_id).order_by(NetworkRow.created_at)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_domain(row) for row in rows]

    async def save(self, network: Network) -> Network:
        assert network.id is not None, "only persisted networks can be saved"
        async with self._session_factory() as session:
            row = await session.get(NetworkRow, network.id)
            assert row is not None
            cast_any: Any = row
            cast_any.server_ids = [str(sid) for sid in network.server_ids]
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def delete(self, network_id: UUID) -> None:
        async with self._session_factory() as session:
            row = await session.get(NetworkRow, network_id)
            if row is not None:
                await session.delete(row)
                await session.commit()
