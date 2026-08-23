"""Audit domain: immutable event records for sensitive actions.

Every audit event captures who did what to which resource, when, and why.
Events are append-only: once persisted they are never updated or deleted
(enforced at the database level by a trigger, and by this module exposing
no mutation operations).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID


class AuditError(Exception):
    """Base error for audit operations."""


class ActorType(StrEnum):
    """Kind of actor that performed the audited action."""

    USER = "user"
    ADMIN = "admin"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """Immutable record of one sensitive action.

    Attributes:
        actor_type: Kind of actor (user/admin/system).
        action: Machine-readable action name (e.g. "wallet.adjust").
        resource_type: Kind of resource affected (e.g. "wallet").
        id: Unique event id (assigned on persistence).
        actor_id: Id of the acting user, when applicable.
        resource_id: Identifier of the affected resource.
        reason: Human-readable justification for the action.
        metadata: Additional structured metadata.
        occurred_at: When the event occurred (assigned on persistence).
    """

    actor_type: ActorType
    action: str
    resource_type: str
    id: UUID | None = None
    actor_id: UUID | None = None
    resource_id: str = ""
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    occurred_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.action or not self.action.strip():
            raise ValueError("audit action must not be empty")
        if not self.resource_type or not self.resource_type.strip():
            raise ValueError("audit resource_type must not be empty")


class AuditRepository(Protocol):
    """Port for the append-only audit log."""

    async def append(self, event: AuditEvent) -> AuditEvent:
        """Persist an event. Returns the persisted event with its id."""
        ...

    async def get_by_resource(self, resource_type: str, resource_id: str) -> list[AuditEvent]:
        """Return all events for a resource, oldest first."""
        ...

    async def get_by_actor(self, actor_id: UUID) -> list[AuditEvent]:
        """Return all events triggered by an actor, oldest first."""
        ...
