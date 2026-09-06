"""SQLAlchemy adapter for provider accounts."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import Provider as _ProviderModel
from cloud_platform.db.base import ProviderAccount as _ProviderAccountModel
from cloud_platform.modules.provider_accounts.domain import (
    ProviderAccount,
    ProviderAccountStatus,
)


def _attr(row: Any, name: str) -> Any:
    """Read a legacy-style Column attribute; typed as Any at the boundary."""
    return getattr(row, name)


def _to_domain(row: _ProviderAccountModel, provider_name: str) -> ProviderAccount:
    return ProviderAccount(
        id=_attr(row, "id"),
        user_id=_attr(row, "user_id"),
        provider_key=str(provider_name),
        status=ProviderAccountStatus(str(_attr(row, "status"))),
    )


class SqlAlchemyProviderAccountRepository:
    """Durable storage for provider accounts (read path for creation)."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def get_active(self, user_id: UUID, provider_key: str) -> ProviderAccount | None:
        async with self._session_factory() as session:
            stmt = (
                select(_ProviderAccountModel, _ProviderModel.name)
                .join(
                    _ProviderModel,
                    _ProviderAccountModel.provider_id == _ProviderModel.id,
                )
                .where(
                    _ProviderAccountModel.user_id == user_id,
                    _ProviderModel.name == provider_key,
                    _ProviderAccountModel.status == ProviderAccountStatus.ACTIVE.value,
                )
                .limit(1)
            )
            result = (await session.execute(stmt)).first()
            if result is None:
                return None
            row, provider_name = result
            return _to_domain(row, str(provider_name))

    async def get_or_create_active(self, user_id: UUID, provider_key: str) -> ProviderAccount:
        existing = await self.get_active(user_id, provider_key)
        if existing is not None:
            return existing
        async with self._session_factory() as session:
            provider_stmt = select(_ProviderModel).where(_ProviderModel.name == provider_key)
            provider_row = (await session.execute(provider_stmt)).scalars().first()
            if provider_row is None:
                from cloud_platform.modules.catalog.repository import provider_key_to_uuid

                provider_row = _ProviderModel(
                    id=provider_key_to_uuid(provider_key),
                    name=provider_key,
                    region="global",
                )
                session.add(provider_row)
                await session.flush()
            account_stmt = select(_ProviderAccountModel).where(
                _ProviderAccountModel.user_id == user_id,
                _ProviderAccountModel.provider_id == provider_row.id,
            )
            account_row = (await session.execute(account_stmt)).scalars().first()
            if account_row is None:
                account_row = _ProviderAccountModel(
                    user_id=user_id,
                    provider_id=provider_row.id,
                    status=ProviderAccountStatus.ACTIVE.value,
                )
                session.add(account_row)
                await session.commit()
            await session.refresh(account_row)
            return _to_domain(account_row, provider_key)
