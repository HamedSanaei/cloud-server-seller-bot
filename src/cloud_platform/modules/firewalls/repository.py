"""SQLAlchemy persistence for firewall rulebooks (M13-006).

Rules are stored as JSONB (validated domain dicts). Ownership is
structural: ``user_id`` on every row and unique names PER USER.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import FirewallRow
from cloud_platform.modules.firewalls.domain import (
    DuplicateFirewallError,
    Firewall,
)


def _attr(row: Any, name: str) -> Any:
    return getattr(row, name)


def _to_domain(row: FirewallRow) -> Firewall:
    raw_rules = _attr(row, "rules") or []
    return Firewall(
        id=_attr(row, "id"),
        user_id=_attr(row, "user_id"),
        name=_attr(row, "name"),
        rules=Firewall.rules_from_dicts(list(raw_rules)),
        provider_firewall_id=_attr(row, "provider_firewall_id"),
        created_at=_attr(row, "created_at"),
    )


def _to_row(domain: Firewall) -> FirewallRow:
    return FirewallRow(
        id=domain.id,
        user_id=domain.user_id,
        name=domain.name,
        rules=[rule.to_dict() for rule in domain.rules],
        provider_firewall_id=domain.provider_firewall_id,
    )


class SqlAlchemyFirewallRepository:
    """SQLAlchemy-backed firewall repository."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def add(self, firewall: Firewall) -> Firewall:
        row = _to_row(firewall)
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                raise DuplicateFirewallError("a firewall with this name already exists") from exc
            await session.refresh(row)
            return _to_domain(row)

    async def get(self, firewall_id: UUID) -> Firewall | None:
        async with self._session_factory() as session:
            row = await session.get(FirewallRow, firewall_id)
            if row is None:
                return None
            return _to_domain(row)

    async def list_for_user(self, user_id: UUID) -> list[Firewall]:
        stmt = (
            select(FirewallRow)
            .where(FirewallRow.user_id == user_id)
            .order_by(FirewallRow.name.asc())
        )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_domain(row) for row in rows]

    async def save(self, firewall: Firewall) -> Firewall:
        row = _to_row(firewall)
        async with self._session_factory() as session:
            merged = await session.merge(row)
            await session.commit()
            await session.refresh(merged)
            return _to_domain(merged)

    async def delete(self, firewall_id: UUID) -> None:
        stmt = delete(FirewallRow).where(FirewallRow.id == firewall_id)
        async with self._session_factory() as session:
            await session.execute(stmt)
            await session.commit()
