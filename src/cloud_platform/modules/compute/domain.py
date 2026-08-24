from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class ServerLifecycleState(StrEnum):
    REQUESTED = "requested"
    PROVISIONING = "provisioning"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"
    DELETE_REQUESTED = "delete_requested"
    DELETING = "deleting"
    DELETED = "deleted"
    MANUAL_REVIEW = "manual_review"


#: States from which a server may be placed under containment (manual review).
#: ``REQUESTED`` is excluded (not yet provisioned; nothing to contain) and
#: ``DELETED`` is excluded (no resource left to hold).
CONTAINABLE_STATES: frozenset[ServerLifecycleState] = frozenset(
    {
        ServerLifecycleState.PROVISIONING,
        ServerLifecycleState.RUNNING,
        ServerLifecycleState.STOPPED,
        ServerLifecycleState.ERROR,
        ServerLifecycleState.DELETE_REQUESTED,
        ServerLifecycleState.DELETING,
    }
)


_ALLOWED: dict[ServerLifecycleState, frozenset[ServerLifecycleState]] = {
    ServerLifecycleState.REQUESTED: frozenset(
        {ServerLifecycleState.PROVISIONING, ServerLifecycleState.ERROR}
    ),
    ServerLifecycleState.PROVISIONING: frozenset(
        {
            ServerLifecycleState.RUNNING,
            ServerLifecycleState.ERROR,
            ServerLifecycleState.MANUAL_REVIEW,
        }
    ),
    ServerLifecycleState.RUNNING: frozenset(
        {
            ServerLifecycleState.STOPPED,
            ServerLifecycleState.DELETE_REQUESTED,
            ServerLifecycleState.ERROR,
            ServerLifecycleState.MANUAL_REVIEW,
        }
    ),
    ServerLifecycleState.STOPPED: frozenset(
        {
            ServerLifecycleState.RUNNING,
            ServerLifecycleState.DELETE_REQUESTED,
            ServerLifecycleState.ERROR,
            ServerLifecycleState.MANUAL_REVIEW,
        }
    ),
    ServerLifecycleState.ERROR: frozenset(
        {
            ServerLifecycleState.PROVISIONING,
            ServerLifecycleState.DELETE_REQUESTED,
            ServerLifecycleState.MANUAL_REVIEW,
        }
    ),
    ServerLifecycleState.DELETE_REQUESTED: frozenset(
        {
            ServerLifecycleState.DELETING,
            ServerLifecycleState.DELETED,
            ServerLifecycleState.MANUAL_REVIEW,
        }
    ),
    ServerLifecycleState.DELETING: frozenset(
        {ServerLifecycleState.DELETED, ServerLifecycleState.MANUAL_REVIEW}
    ),
    ServerLifecycleState.DELETED: frozenset(),
    # Containment is reversible: release returns the server to exactly the
    # state it was in when contained (tracked in ``contained_from``), so every
    # containable state is a valid release target.
    ServerLifecycleState.MANUAL_REVIEW: frozenset(
        {
            ServerLifecycleState.PROVISIONING,
            ServerLifecycleState.RUNNING,
            ServerLifecycleState.STOPPED,
            ServerLifecycleState.ERROR,
            ServerLifecycleState.DELETE_REQUESTED,
            ServerLifecycleState.DELETING,
            ServerLifecycleState.DELETED,
        }
    ),
}


@dataclass(slots=True)
class CloudServer:
    id: UUID
    user_id: UUID
    provider_key: str
    provider_account_id: UUID
    state: ServerLifecycleState
    provider_server_id: str | None = None
    contained_from: ServerLifecycleState | None = None
    idempotency_key: str | None = None
    created_at: datetime | None = None

    def transition_to(self, target: ServerLifecycleState) -> None:
        if target not in _ALLOWED[self.state]:
            raise ValueError(f"invalid lifecycle transition: {self.state} -> {target}")
        self.state = target

    @property
    def is_containable(self) -> bool:
        """Whether this server can currently be placed under containment."""
        return self.state in CONTAINABLE_STATES

    def contain(self) -> ServerLifecycleState:
        """Move under containment (MANUAL_REVIEW), remembering the prior state.

        Returns the state the server was in before containment. Idempotent: a
        server already under containment keeps its original ``contained_from``.
        """
        if self.state is ServerLifecycleState.MANUAL_REVIEW:
            if self.contained_from is None:
                raise ValueError("server under containment has no recorded prior state")
            return self.contained_from
        if not self.is_containable:
            raise ValueError(f"cannot contain server in state {self.state.value}")
        prior = self.state
        self.contained_from = prior
        self.transition_to(ServerLifecycleState.MANUAL_REVIEW)
        return prior

    def release(self) -> ServerLifecycleState:
        """Release from containment back to the pre-containment state.

        Raises:
            ValueError: If the server is not under containment, or the
                recorded pre-containment state is unknown.
        """
        if self.state is not ServerLifecycleState.MANUAL_REVIEW:
            raise ValueError(f"cannot release server in state {self.state.value}")
        if self.contained_from is None:
            raise ValueError("server under containment has no recorded prior state")
        target = self.contained_from
        self.transition_to(target)
        self.contained_from = None
        return target


class ServerCreateError(Exception):
    """Raised when a server creation intent cannot be persisted."""


@dataclass(frozen=True, slots=True)
class ServerCreateIntent:
    """The durable intent of the create-server command (M07-001).

    Persisted alongside the ``REQUESTED`` server row so the intent is
    atomic with the server. ``idempotency_key`` is unique across the
    table: a retried command can never create a second server.
    ``cost_minor`` is the provider cost per quantum at purchase time
    (informational; the price snapshot is authoritative for billing).
    """

    catalog_id: UUID
    cost_minor: int
    currency: str
    idempotency_key: str

    def __post_init__(self) -> None:
        if self.cost_minor < 0:
            raise ValueError("cost_minor must not be negative")
        if not self.currency or not self.currency.strip():
            raise ValueError("currency must not be empty")
        if not self.idempotency_key or not self.idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")


@dataclass(frozen=True, slots=True)
class ProvisioningSpec:
    """The provider-side spec to provision a server from its catalog offer.

    Resolved from the server's pinned catalog row, so provisioning always
    matches the offer the server was bought from.
    """

    plan_id: str
    location_id: str
    currency: str

    def __post_init__(self) -> None:
        for name, value in (
            ("plan_id", self.plan_id),
            ("location_id", self.location_id),
            ("currency", self.currency),
        ):
            if not value or not value.strip():
                raise ValueError(f"{name} must not be empty")


@dataclass(frozen=True, slots=True)
class QuotaPolicy:
    """Per-user provisioning quota (M10-003).

    ``max_active`` caps the concurrent servers (every state except DELETED —
    a deleting server still occupies provider capacity). ``max_total`` caps
    lifetime creations (including DELETED rows). Zero means the user may not
    create anything.
    """

    max_active: int = 10
    max_total: int = 50

    def __post_init__(self) -> None:
        if self.max_active < 0:
            raise ValueError("max_active must not be negative")
        if self.max_total < 0:
            raise ValueError("max_total must not be negative")


@dataclass(frozen=True, slots=True)
class MaintenanceScope:
    """A maintenance switch scope (M10-005).

    ``location_id is None`` means the whole provider; otherwise only that
    provider/location pair.
    """

    provider_key: str
    location_id: str | None = None

    def __post_init__(self) -> None:
        if not self.provider_key or not self.provider_key.strip():
            raise ValueError("provider_key must not be empty")
        if self.location_id is not None and (not self.location_id or not self.location_id.strip()):
            raise ValueError("location_id must not be empty")


@dataclass(frozen=True, slots=True)
class MaintenanceBlock:
    """An active maintenance switch: new orders for the scope are blocked."""

    scope: MaintenanceScope
    reason: str
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime | None = None


def is_order_blocked(
    blocks: Sequence[MaintenanceBlock], provider_key: str, location_id: str
) -> bool:
    """True when a block covers the provider or the provider/location pair."""
    for block in blocks:
        if block.scope.provider_key != provider_key:
            continue
        if block.scope.location_id is None or block.scope.location_id == location_id:
            return True
    return False


class ServerRepository(Protocol):
    """Port for CloudServer persistence."""

    async def get(self, server_id: UUID) -> CloudServer | None:
        """One server by id, or None."""
        ...

    async def list_by_user(self, user_id: UUID) -> list[CloudServer]:
        """All servers owned by a user."""
        ...

    async def list_by_user_paged(
        self, user_id: UUID, *, offset: int, limit: int
    ) -> tuple[list[CloudServer], int]:
        """One page of a user's servers, newest first, plus the total count."""
        ...

    async def count_active(self, user_id: UUID) -> int:
        """The user's servers in every state except DELETED (quota: concurrent)."""
        ...

    async def count_total(self, user_id: UUID) -> int:
        """The user's servers in every state, DELETED included (quota: lifetime)."""
        ...

    async def list_requested(self) -> list[CloudServer]:
        """All servers in REQUESTED state (provisioning intents)."""
        ...

    async def list_provisioning(self) -> list[CloudServer]:
        """All servers in PROVISIONING state (reconciliation candidates)."""
        ...

    async def list_running(self) -> list[CloudServer]:
        """All servers in RUNNING state (state-reconciliation candidates)."""
        ...

    async def list_stopped(self) -> list[CloudServer]:
        """All servers in STOPPED state (state-reconciliation candidates)."""
        ...

    async def list_manual_review(self) -> list[CloudServer]:
        """All servers in MANUAL_REVIEW state (the operator review queue)."""
        ...

    async def save(self, server: CloudServer) -> CloudServer:
        """Persist lifecycle/state changes to an existing server."""
        ...

    async def create(self, server: CloudServer, intent: ServerCreateIntent) -> CloudServer:
        """Persist a new REQUESTED server with its create intent.

        Raises:
            ServerCreateError: On a constraint violation (e.g. the
                idempotency key was already consumed).
            LookupError: If the provider is unknown.
        """
        ...

    async def get_by_idempotency_key(self, idempotency_key: str) -> CloudServer | None:
        """The server created under an idempotency key, or None."""
        ...

    async def get_provisioning_spec(self, server_id: UUID) -> ProvisioningSpec | None:
        """The provider plan/location the server's pinned catalog offer maps to."""
        ...


class MaintenanceSwitchRepository(Protocol):
    """Durable storage for provider/location maintenance switches (M10-005)."""

    async def list_blocks(self) -> list[MaintenanceBlock]:
        """All active switches."""
        ...

    async def save_block(self, block: MaintenanceBlock) -> MaintenanceBlock:
        """Upsert a switch for its scope (idempotent: same scope updates)."""
        ...

    async def remove_block(self, scope: MaintenanceScope) -> bool:
        """Remove a switch; True when one existed."""
        ...
