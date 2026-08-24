"""SQLAlchemy persistence for API tokens (M14-002).

Only HASHES are stored. The hash column is UNIQUE (it is the lookup key);
names are unique PER USER; revocation is a timestamp, never a delete.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import ApiTokenRow
from cloud_platform.modules.tokens.domain import ApiToken, TokenScope


def _attr(row: Any, name: str) -> Any:
    return getattr(row, name)


def _to_domain(row: ApiTokenRow) -> ApiToken:
    raw_scopes = _attr(row, "scopes") or []
    return ApiToken(
        id=_attr(row, "id"),
        user_id=_attr(row, "user_id"),
        name=_attr(row, "name"),
        token_hash=_attr(row, "token_hash"),
        prefix=_attr(row, "prefix") or "",
        scopes=frozenset(TokenScope(s) for s in raw_scopes),
        created_at=_attr(row, "created_at"),
        revoked_at=_attr(row, "revoked_at"),
        last_used_at=_attr(row, "last_used_at"),
    )


def _to_row(domain: ApiToken) -> ApiTokenRow:
    return ApiTokenRow(
        id=domain.id,
        user_id=domain.user_id,
        name=domain.name,
        token_hash=domain.token_hash,
        prefix=domain.prefix,
        scopes=[s.value for s in domain.scopes],
        created_at=domain.created_at,
        revoked_at=domain.revoked_at,
        last_used_at=domain.last_used_at,
    )


class SqlAlchemyApiTokenRepository:
    """SQLAlchemy-backed API-token repository (hash-keyed)."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def add(self, token: ApiToken) -> ApiToken:
        row = _to_row(token)
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def get(self, token_id: UUID) -> ApiToken | None:
        async with self._session_factory() as session:
            row = await session.get(ApiTokenRow, token_id)
            if row is None:
                return None
            return _to_domain(row)

    async def get_by_hash(self, token_hash: str) -> ApiToken | None:
        stmt = select(ApiTokenRow).where(ApiTokenRow.token_hash == token_hash)
        async with self._session_factory() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            return _to_domain(row)

    async def list_for_user(self, user_id: UUID) -> list[ApiToken]:
        stmt = (
            select(ApiTokenRow)
            .where(ApiTokenRow.user_id == user_id)
            .order_by(ApiTokenRow.created_at.desc())
        )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_domain(row) for row in rows]

    async def save(self, token: ApiToken) -> ApiToken:
        row = _to_row(token)
        async with self._session_factory() as session:
            merged = await session.merge(row)
            await session.commit()
            await session.refresh(merged)
            return _to_domain(merged)
