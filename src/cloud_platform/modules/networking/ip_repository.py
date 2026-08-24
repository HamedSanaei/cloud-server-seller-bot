"""Persistence for user-owned IP resources (M13-008)."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import IpAddressRow
from cloud_platform.modules.networking.ips import IpAddress, IpKind


def _attr(row: Any, name: str) -> Any:
    return getattr(row, name)


def _to_domain(row: IpAddressRow) -> IpAddress:
    return IpAddress(
        user_id=_attr(row, "user_id"),
        provider_account_id=_attr(row, "provider_account_id"),
        provider_key=_attr(row, "provider_key"),
        provider_ip_id=_attr(row, "provider_ip_id"),
        ip=_attr(row, "ip"),
        kind=IpKind(str(_attr(row, "kind"))),
        location_id=_attr(row, "location_id"),
        server_id=_attr(row, "server_id"),
        id=_attr(row, "id"),
        created_at=_attr(row, "created_at"),
    )


class SqlAlchemyIpAddressRepository:
    """SQLAlchemy-backed IP resource store."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def add(self, record: IpAddress) -> IpAddress:
        row = IpAddressRow(
            user_id=record.user_id,
            provider_account_id=record.provider_account_id,
            provider_key=record.provider_key,
            provider_ip_id=record.provider_ip_id,
            ip=record.ip,
            kind=record.kind.value,
            location_id=record.location_id,
            server_id=record.server_id,
        )
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def get(self, ip_id: UUID) -> IpAddress | None:
        async with self._session_factory() as session:
            row = await session.get(IpAddressRow, ip_id)
            return None if row is None else _to_domain(row)

    async def list_for_user(self, user_id: UUID) -> list[IpAddress]:
        stmt = (
            select(IpAddressRow)
            .where(IpAddressRow.user_id == user_id)
            .order_by(IpAddressRow.created_at)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_domain(row) for row in rows]

    async def save(self, record: IpAddress) -> IpAddress:
        assert record.id is not None, "only persisted IPs can be saved"
        async with self._session_factory() as session:
            row = await session.get(IpAddressRow, record.id)
            assert row is not None
            cast_any: Any = row
            cast_any.server_id = record.server_id
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def delete(self, ip_id: UUID) -> None:
        async with self._session_factory() as session:
            row = await session.get(IpAddressRow, ip_id)
            if row is not None:
                await session.delete(row)
                await session.commit()
