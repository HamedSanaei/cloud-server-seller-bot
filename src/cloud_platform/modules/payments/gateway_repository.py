"""SQLAlchemy adapter for durable payment-gateway availability overrides."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import GatewaySetting
from cloud_platform.db.timestamps import utc_now


class SqlAlchemyGatewaySettingsRepository:
    def __init__(
        self, session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]]
    ) -> None:
        self._session_factory = session_factory

    async def get(self, key: str) -> bool | None:
        async with self._session_factory() as db:
            result = await db.execute(
                select(GatewaySetting.enabled).where(GatewaySetting.key == key)
            )
            value = result.scalar_one_or_none()
            return None if value is None else bool(value)

    async def set_enabled(self, key: str, enabled: bool, actor_id: UUID) -> None:
        moment = utc_now()
        statement = insert(GatewaySetting).values(
            key=key, enabled=enabled, updated_by=actor_id, created_at=moment, updated_at=moment
        )
        statement = statement.on_conflict_do_update(
            index_elements=[GatewaySetting.key],
            set_={"enabled": enabled, "updated_by": actor_id, "updated_at": moment},
        )
        async with self._session_factory() as db:
            await db.execute(statement)
            await db.commit()

    async def list(self) -> dict[str, bool]:
        async with self._session_factory() as db:
            result = await db.execute(select(GatewaySetting.key, GatewaySetting.enabled))
            return {key: bool(enabled) for key, enabled in result.all()}
