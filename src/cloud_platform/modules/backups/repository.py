"""Persistence for per-server backup settings (M13-005)."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import ServerBackupSettingsRow
from cloud_platform.modules.backups.domain import BackupSettings


def _attr(row: Any, name: str) -> Any:
    return getattr(row, name)


def _to_domain(row: ServerBackupSettingsRow) -> BackupSettings:
    return BackupSettings(
        server_id=_attr(row, "server_id"),
        enabled=_attr(row, "enabled"),
        surcharge_bps_at_change=_attr(row, "surcharge_bps_at_change"),
        updated_by=_attr(row, "updated_by"),
        updated_at=_attr(row, "updated_at"),
    )


class SqlAlchemyBackupSettingsRepository:
    """SQLAlchemy-backed settings store (one row per server)."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def get(self, server_id: UUID) -> BackupSettings | None:
        async with self._session_factory() as session:
            row = await session.get(ServerBackupSettingsRow, server_id)
            if row is None:
                return None
            return _to_domain(row)

    async def upsert(self, settings: BackupSettings) -> BackupSettings:
        row = ServerBackupSettingsRow(
            server_id=settings.server_id,
            enabled=settings.enabled,
            surcharge_bps_at_change=settings.surcharge_bps_at_change,
            updated_by=settings.updated_by,
            updated_at=settings.updated_at,
        )
        async with self._session_factory() as session:
            merged = await session.merge(row)
            await session.commit()
            await session.refresh(merged)
            return _to_domain(merged)
