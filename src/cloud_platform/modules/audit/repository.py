"""SQLAlchemy repository adapter for the append-only audit log.

Immutability is enforced in three layers:
- the domain ``AuditEvent`` is a frozen dataclass,
- this repository exposes no update/delete operations,
- the ``audit_events`` table carries a trigger that rejects UPDATE/DELETE.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import AuditEvent as _AuditModel
from cloud_platform.modules.audit.domain import ActorType, AuditEvent


def _attr(row: Any, name: str) -> Any:
    """Read a legacy-style Column attribute; typed as Any at the boundary."""
    return getattr(row, name)


def _to_domain(row: _AuditModel) -> AuditEvent:
    return AuditEvent(
        actor_type=ActorType(_attr(row, "actor_type")),
        action=str(_attr(row, "action")),
        resource_type=str(_attr(row, "resource_type")),
        id=_attr(row, "id"),
        actor_id=_attr(row, "actor_id"),
        resource_id=_attr(row, "resource_id") or "",
        reason=_attr(row, "reason") or "",
        metadata=dict(_attr(row, "event_metadata") or {}),
        occurred_at=_attr(row, "occurred_at"),
    )


class SqlAlchemyAuditRepository:
    """Append-only audit log backed by SQLAlchemy."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def append(self, event: AuditEvent) -> AuditEvent:
        """Persist an event. Returns the persisted event with its id."""
        row = _AuditModel(
            actor_type=event.actor_type.value,
            action=event.action,
            resource_type=event.resource_type,
            resource_id=event.resource_id or None,
            actor_id=event.actor_id,
            reason=event.reason,
            event_metadata=event.metadata,
        )
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def get_by_resource(self, resource_type: str, resource_id: str) -> list[AuditEvent]:
        """Return all events for a resource, oldest first."""
        async with self._session_factory() as session:
            stmt = (
                select(_AuditModel)
                .where(
                    _AuditModel.resource_type == resource_type,
                    _AuditModel.resource_id == str(resource_id),
                )
                .order_by(_AuditModel.occurred_at.asc())
            )
            result = await session.execute(stmt)
            return [_to_domain(row) for row in result.scalars().all()]

    async def get_by_actor(self, actor_id: UUID) -> list[AuditEvent]:
        """Return all events triggered by an actor, oldest first."""
        async with self._session_factory() as session:
            stmt = (
                select(_AuditModel)
                .where(_AuditModel.actor_id == actor_id)
                .order_by(_AuditModel.occurred_at.asc())
            )
            result = await session.execute(stmt)
            return [_to_domain(row) for row in result.scalars().all()]

    async def list_recent(self, limit: int = 20, offset: int = 0) -> list[AuditEvent]:
        """Return the newest events first (operations feed, M14-005)."""
        async with self._session_factory() as session:
            stmt = (
                select(_AuditModel)
                .order_by(_AuditModel.occurred_at.desc(), _AuditModel.id.desc())
                .limit(max(1, min(int(limit), 100)))
                .offset(max(0, int(offset)))
            )
            result = await session.execute(stmt)
            return [_to_domain(row) for row in result.scalars().all()]
