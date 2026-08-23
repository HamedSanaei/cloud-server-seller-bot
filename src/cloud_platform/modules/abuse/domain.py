"""Abuse-case domain: map reported provider resources to responsible users.

When a provider (or an operator) reports abuse against a resource — a server
id, an IPv4, or an IPv6 — the platform must quickly identify the responsible
user and run a review workflow. The core value is the *mapping*: a
``ResourceOwnershipResolver`` turns a raw provider resource reference into an
``Ownership`` (responsible user + internal server), and ``AbuseIntakeService``
records an audited ``AbuseCase`` that carries that mapping.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class AbuseError(Exception):
    """Base error for abuse workflow operations."""


class InvalidAbuseTransition(AbuseError):
    """Raised when a status transition violates the case lifecycle."""


class ResourceNotManagedError(AbuseError):
    """Raised when a reported resource does not map to any managed server."""


class AbuseStatus(StrEnum):
    """Lifecycle of an abuse case."""

    OPEN = "open"
    INVESTIGATING = "investigating"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


#: Allowed transitions for the abuse workflow.
_TRANSITIONS: dict[AbuseStatus, frozenset[AbuseStatus]] = {
    AbuseStatus.OPEN: frozenset({AbuseStatus.INVESTIGATING, AbuseStatus.DISMISSED}),
    AbuseStatus.INVESTIGATING: frozenset({AbuseStatus.RESOLVED, AbuseStatus.DISMISSED}),
    AbuseStatus.RESOLVED: frozenset(),
    AbuseStatus.DISMISSED: frozenset(),
}


class ResourceType(StrEnum):
    """Kind of provider resource being reported."""

    PROVIDER_SERVER = "provider_server"
    IPV4 = "ipv4"
    IPV6 = "ipv6"


@dataclass(frozen=True, slots=True)
class ResourceRef:
    """A provider resource reported as abused."""

    provider_key: str
    resource_type: ResourceType
    resource_id: str

    def __post_init__(self) -> None:
        if not self.provider_key or not self.provider_key.strip():
            raise ValueError("provider_key must not be empty")
        if not self.resource_id or not self.resource_id.strip():
            raise ValueError("resource_id must not be empty")


@dataclass(frozen=True, slots=True)
class Ownership:
    """Result of mapping a provider resource to a responsible user."""

    user_id: UUID
    server_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class AbuseCase:
    """One reported abuse finding, linked to the responsible user.

    ``user_id`` and ``server_id`` are populated at intake from the ownership
    resolver, so the mapping from provider resource to responsible user is
    captured once and travels with the case through its lifecycle.
    """

    resource: ResourceRef
    user_id: UUID
    reason: str
    reporter: str
    status: AbuseStatus = AbuseStatus.OPEN
    id: UUID | None = None
    server_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    resolved_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.reason or not self.reason.strip():
            raise ValueError("reason must not be empty")
        if not self.reporter or not self.reporter.strip():
            raise ValueError("reporter must not be empty")

    def can_transition_to(self, target: AbuseStatus) -> bool:
        return target in _TRANSITIONS[self.status]

    def transition(self, target: AbuseStatus, *, at: datetime) -> AbuseCase:
        """Return a copy in ``target`` status, or raise on an invalid move."""
        if not self.can_transition_to(target):
            raise InvalidAbuseTransition(
                f"abuse case {self.id} cannot move {self.status.value} -> {target.value}"
            )
        resolved_at = self.resolved_at
        if target in (AbuseStatus.RESOLVED, AbuseStatus.DISMISSED):
            resolved_at = at
        return AbuseCase(
            resource=self.resource,
            user_id=self.user_id,
            reason=self.reason,
            reporter=self.reporter,
            status=target,
            id=self.id,
            server_id=self.server_id,
            created_at=self.created_at,
            updated_at=at,
            resolved_at=resolved_at,
        )


class ResourceOwnershipResolver(Protocol):
    """Maps a reported provider resource to the responsible user, quickly."""

    async def resolve(self, ref: ResourceRef) -> Ownership | None:
        """Return the ownership for ``ref`` or None if it is not managed."""
        ...


class AbuseCaseRepository(Protocol):
    """Port for durable abuse cases."""

    async def create(self, case: AbuseCase) -> AbuseCase:
        """Persist a new case. Returns it with its id."""
        ...

    async def get(self, case_id: UUID) -> AbuseCase | None:
        """Fetch by primary key."""
        ...

    async def get_by_user(self, user_id: UUID) -> list[AbuseCase]:
        """All cases for a responsible user, newest first."""
        ...

    async def list_open(self) -> list[AbuseCase]:
        """All non-terminal cases (open or investigating), oldest first."""
        ...

    async def save(self, case: AbuseCase) -> AbuseCase:
        """Persist the current state of a case aggregate."""
        ...
