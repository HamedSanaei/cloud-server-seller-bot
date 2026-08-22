from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import User as SQLAlchemyUser
from cloud_platform.modules.users.domain import (
    Role,
    User,
    UserNotFound,
    UserStatus,
)


def _attr(row: SQLAlchemyUser, name: str) -> Any:
    """Read a legacy-style Column attribute; typed as Any at the boundary."""
    return getattr(row, name)


def _to_domain(row: SQLAlchemyUser) -> User:
    """Map a SQLAlchemy ORM row to a domain User aggregate."""
    return User(
        id=_attr(row, "id"),
        username=_attr(row, "username"),
        email=_attr(row, "email"),
        status=UserStatus(_attr(row, "status")),
        role=Role(_attr(row, "role")),
        terms_accepted_at=_attr(row, "terms_accepted_at"),
        created_at=_attr(row, "created_at"),
        updated_at=_attr(row, "updated_at"),
    )


def _to_row(domain: User) -> SQLAlchemyUser:
    """Map a domain User aggregate to a SQLAlchemy ORM row."""
    if domain.id is None:
        row = SQLAlchemyUser(
            username=domain.username,
            email=domain.email,
            status=domain.status.value,
            role=domain.role.value,
        )
    else:
        row = SQLAlchemyUser(
            id=domain.id,
            username=domain.username,
            email=domain.email,
            status=domain.status.value,
            role=domain.role.value,
            terms_accepted_at=domain.terms_accepted_at,
            created_at=domain.created_at,
            updated_at=datetime.now(UTC),
        )
    return row


class SqlAlchemyUserRepository:
    """SQLAlchemy-backed implementation of the User repository port."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def create(self, user: User) -> User:
        """Create a new user. Commits and returns the refreshed aggregate."""
        row = _to_row(user)
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def get(self, user_id: UUID) -> User | None:
        """Get a user by ID. Returns None when absent."""
        async with self._session_factory() as session:
            stmt = select(SQLAlchemyUser).where(SQLAlchemyUser.id == user_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return _to_domain(row) if row is not None else None

    async def get_by_username(self, username: str) -> User | None:
        """Get a user by username. Returns None when absent."""
        async with self._session_factory() as session:
            stmt = select(SQLAlchemyUser).where(SQLAlchemyUser.username == username)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return _to_domain(row) if row is not None else None

    async def get_by_email(self, email: str) -> User | None:
        """Get a user by email. Returns None when absent."""
        async with self._session_factory() as session:
            stmt = select(SQLAlchemyUser).where(SQLAlchemyUser.email == email)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return _to_domain(row) if row is not None else None

    async def update_status(self, user_id: UUID, status: UserStatus) -> User:
        """Update a user's status. Raises UserNotFound if missing."""
        async with self._session_factory() as session:
            stmt = select(SQLAlchemyUser).where(SQLAlchemyUser.id == user_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()

            if row is None:
                raise UserNotFound(f"User with id {user_id} not found")

            cast(Any, row).status = status.value  # legacy Column attribute
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def get_by_telegram_user_id(self, telegram_user_id: int) -> User | None:
        """Get a user by their Telegram user id. Returns None when absent."""
        async with self._session_factory() as session:
            stmt = select(SQLAlchemyUser).where(SQLAlchemyUser.telegram_user_id == telegram_user_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return _to_domain(row) if row is not None else None
