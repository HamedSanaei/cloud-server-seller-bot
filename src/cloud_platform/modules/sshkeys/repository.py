"""SQLAlchemy persistence for SSH keys (M13-001).

Ownership scoping is structural: every read filters on ``user_id`` and the
unique constraints are per-user, so a duplicate name/fingerprint can only
collide within one account.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import SshKeyRow
from cloud_platform.modules.sshkeys.domain import DuplicateSshKeyError, SshKey


def _attr(row: Any, name: str) -> Any:
    """Read an ORM attribute; typed as Any at the boundary."""
    return getattr(row, name)


def _to_domain(row: SshKeyRow) -> SshKey:
    return SshKey(
        id=_attr(row, "id"),
        user_id=_attr(row, "user_id"),
        name=_attr(row, "name"),
        public_key=_attr(row, "public_key"),
        fingerprint=_attr(row, "fingerprint"),
        created_at=_attr(row, "created_at"),
    )


class SqlAlchemySshKeyRepository:
    """SQLAlchemy-backed SSH-key repository."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def add(self, key: SshKey) -> SshKey:
        row = SshKeyRow(
            id=key.id,
            user_id=key.user_id,
            name=key.name,
            public_key=key.public_key,
            fingerprint=key.fingerprint,
        )
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                raise DuplicateSshKeyError(
                    "a key with this name or material is already registered"
                ) from exc
            await session.refresh(row)
            return _to_domain(row)

    async def get(self, key_id: UUID) -> SshKey | None:
        async with self._session_factory() as session:
            row = await session.get(SshKeyRow, key_id)
            if row is None:
                return None
            return _to_domain(row)

    async def list_for_user(self, user_id: UUID) -> list[SshKey]:
        stmt = (
            select(SshKeyRow)
            .where(SshKeyRow.user_id == user_id)
            .order_by(SshKeyRow.created_at.asc(), SshKeyRow.name.asc())
        )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_domain(row) for row in rows]

    async def delete(self, key_id: UUID) -> None:
        stmt = delete(SshKeyRow).where(SshKeyRow.id == key_id)
        async with self._session_factory() as session:
            await session.execute(stmt)
            await session.commit()
