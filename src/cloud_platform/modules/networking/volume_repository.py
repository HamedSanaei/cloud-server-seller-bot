"""Persistence for user-owned volumes (M13-009)."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import VolumeRow
from cloud_platform.modules.networking.volumes import Volume


def _attr(row: Any, name: str) -> Any:
    return getattr(row, name)


def _to_domain(row: VolumeRow) -> Volume:
    return Volume(
        user_id=_attr(row, "user_id"),
        provider_account_id=_attr(row, "provider_account_id"),
        provider_key=_attr(row, "provider_key"),
        provider_volume_id=_attr(row, "provider_volume_id"),
        name=_attr(row, "name"),
        size_gb=_attr(row, "size_gb"),
        location_id=_attr(row, "location_id"),
        server_id=_attr(row, "server_id"),
        id=_attr(row, "id"),
        created_at=_attr(row, "created_at"),
    )


class SqlAlchemyVolumeRepository:
    """SQLAlchemy-backed volume store."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def add(self, volume: Volume) -> Volume:
        row = VolumeRow(
            user_id=volume.user_id,
            provider_account_id=volume.provider_account_id,
            provider_key=volume.provider_key,
            provider_volume_id=volume.provider_volume_id,
            name=volume.name,
            size_gb=volume.size_gb,
            location_id=volume.location_id,
            server_id=volume.server_id,
        )
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def get(self, volume_id: UUID) -> Volume | None:
        async with self._session_factory() as session:
            row = await session.get(VolumeRow, volume_id)
            return None if row is None else _to_domain(row)

    async def list_for_user(self, user_id: UUID) -> list[Volume]:
        stmt = select(VolumeRow).where(VolumeRow.user_id == user_id).order_by(VolumeRow.created_at)
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_domain(row) for row in rows]

    async def list_all(self) -> list[Volume]:
        stmt = select(VolumeRow).order_by(VolumeRow.created_at)
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_domain(row) for row in rows]

    async def save(self, volume: Volume) -> Volume:
        assert volume.id is not None, "only persisted volumes can be saved"
        async with self._session_factory() as session:
            row = await session.get(VolumeRow, volume.id)
            assert row is not None
            cast_any: Any = row
            cast_any.server_id = volume.server_id
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def delete(self, volume_id: UUID) -> None:
        async with self._session_factory() as session:
            row = await session.get(VolumeRow, volume_id)
            if row is not None:
                await session.delete(row)
                await session.commit()
