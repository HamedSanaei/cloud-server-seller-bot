"""Operation ledger domain (M07-002).

An **operation** is the durable intent for one provider-mutating action.
Every external mutation flows through exactly one operation, and the
operation's unique ``operation_key`` doubles as the **IdempotencyKey** sent
to the provider: the provider applies the mutation at most once per
operation intent, and the worker only re-sends with the *same* key when a
previous attempt failed without a recorded result.

State machine:

    PENDING -> IN_FLIGHT   (claim: one worker owns the attempt)
    IN_FLIGHT -> PENDING   (retryable failure: re-queued, same key)
    IN_FLIGHT -> COMPLETED (provider result + correlation recorded)
    IN_FLIGHT -> FAILED    (permanent failure)

COMPLETED and FAILED are terminal.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from cloud_platform.modules.compute.domain import ServerLifecycleState


class OperationError(Exception):
    """Base error for operation ledger operations."""


class InvalidOperationTransition(OperationError):
    """Raised on an illegal status transition."""


class OperationType(StrEnum):
    SERVER_CREATE = "server_create"
    POWER_ON = "power_on"
    POWER_OFF = "power_off"
    REBOOT = "reboot"


class OperationStatus(StrEnum):
    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"
    FAILED = "failed"


_TERMINAL = frozenset({OperationStatus.COMPLETED, OperationStatus.FAILED})

_ALLOWED: dict[OperationStatus, frozenset[OperationStatus]] = {
    OperationStatus.PENDING: frozenset({OperationStatus.IN_FLIGHT}),
    OperationStatus.IN_FLIGHT: frozenset(
        {OperationStatus.PENDING, OperationStatus.COMPLETED, OperationStatus.FAILED}
    ),
    OperationStatus.COMPLETED: frozenset(),
    OperationStatus.FAILED: frozenset(),
}


@dataclass(slots=True)
class Operation:
    """One provider-mutating intent with its recorded correlation."""

    id: UUID
    operation_key: str
    operation_type: OperationType
    resource_type: str
    resource_id: UUID
    provider_key: str
    status: OperationStatus = OperationStatus.PENDING
    provider_response: dict[str, object] | None = None
    error: str | None = None
    attempts: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.operation_key or not self.operation_key.strip():
            raise ValueError("operation_key must not be empty")
        if not self.provider_key or not self.provider_key.strip():
            raise ValueError("provider_key must not be empty")
        if self.attempts < 0:
            raise ValueError("attempts must not be negative")

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL

    def _transition_to(self, target: OperationStatus) -> None:
        if target not in _ALLOWED[self.status]:
            raise InvalidOperationTransition(
                f"operation {self.operation_key}: {self.status.value} -> {target.value}"
            )
        self.status = target

    def mark_in_flight(self) -> None:
        """Claim for execution (PENDING -> IN_FLIGHT)."""
        self._transition_to(OperationStatus.IN_FLIGHT)
        self.attempts += 1

    def requeue(self, error: str) -> None:
        """Retryable failure: back to PENDING, error remembered (IN_FLIGHT -> PENDING)."""
        self._transition_to(OperationStatus.PENDING)
        self.error = error

    def complete(self, correlation: dict[str, object]) -> None:
        """Record the provider correlation (IN_FLIGHT -> COMPLETED)."""
        self._transition_to(OperationStatus.COMPLETED)
        self.provider_response = dict(correlation)
        self.error = None

    def fail(self, error: str) -> None:
        """Permanent failure (IN_FLIGHT -> FAILED)."""
        self._transition_to(OperationStatus.FAILED)
        self.error = error

    def reopen_for_retry(self) -> None:
        """Manual tooling only: FAILED -> PENDING with the same operation key.

        Deliberately NOT part of ``_ALLOWED``: no automated path may reopen a
        failed operation. The replay is safe because the worker re-sends the
        SAME operation key, so the provider deduplicates the mutation.
        """
        if self.status is not OperationStatus.FAILED:
            raise InvalidOperationTransition(
                f"operation {self.operation_key}: {self.status.value} -> retry "
                "(only FAILED operations may be reopened)"
            )
        self.status = OperationStatus.PENDING


# ---------------------------------------------------------------------------
# Server state reconciliation (M07-005)
# ---------------------------------------------------------------------------


class ProviderServerState(StrEnum):
    """Provider-neutral view of a remote server's lifecycle."""

    CREATING = "creating"
    RUNNING = "running"
    STOPPED = "stopped"
    DELETING = "deleting"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


# Keyword sets for the lenient normalizer. Order matters: more specific first.
_CREATING = ("creat", "build", "provision", "initial", "pending", "start", "activ")
_STOPPED = ("shut", "stop", "off", "paus", "suspend", "halt")
_DELETING = ("delet", "terminat", "destroy", "remov")
_RUNNING = ("run",)


def normalize_provider_status(status: str | None) -> ProviderServerState:
    """Map a provider-specific status string onto the neutral vocabulary.

    Lenient and case-insensitive; anything unrecognized is UNKNOWN (which the
    planner routes to manual review rather than guessing).
    """
    if status is None:
        return ProviderServerState.UNKNOWN
    s = status.strip().lower()
    if not s:
        return ProviderServerState.UNKNOWN
    for word in _DELETING:
        if word in s:
            return ProviderServerState.DELETING
    for word in _STOPPED:
        if word in s:
            return ProviderServerState.STOPPED
    for word in _CREATING:
        if word in s:
            return ProviderServerState.CREATING
    for word in _RUNNING:
        if word in s:
            return ProviderServerState.RUNNING
    return ProviderServerState.UNKNOWN


class StateAction(StrEnum):
    """What the planner wants the reconciler to do about one server."""

    NONE = "none"  # local state already matches the provider
    IN_PROGRESS = "in_progress"  # provider still creating; the waiter's job
    REPAIR = "repair"  # transition the local row to ``target`` to match reality
    CONTAIN = "contain"  # unexpected drift -> MANUAL_REVIEW for a human


@dataclass(frozen=True, slots=True)
class StatePlan:
    """Pure decision for one (local state, remote state) pair."""

    action: StateAction
    target: ServerLifecycleState | None = None  # set when action is REPAIR
    reason: str = ""


# Expected-transient: provider still working, no action needed.
# Repair map: local state -> {remote state: target local state}.
_REPAIR: dict[ServerLifecycleState, dict[ProviderServerState, ServerLifecycleState]] = {
    ServerLifecycleState.PROVISIONING: {
        ProviderServerState.RUNNING: ServerLifecycleState.RUNNING,
    },
    ServerLifecycleState.RUNNING: {
        ProviderServerState.STOPPED: ServerLifecycleState.STOPPED,
    },
    ServerLifecycleState.STOPPED: {
        ProviderServerState.RUNNING: ServerLifecycleState.RUNNING,
    },
}

# Remote states that are a normal, expected transient for a local state.
_IN_PROGRESS: dict[ServerLifecycleState, frozenset[ProviderServerState]] = {
    ServerLifecycleState.PROVISIONING: frozenset({ProviderServerState.CREATING}),
    ServerLifecycleState.RUNNING: frozenset(),
    ServerLifecycleState.STOPPED: frozenset(),
}


def plan_state_repair(local: ServerLifecycleState, remote: ProviderServerState) -> StatePlan:
    """Pure decision: how to bring ``local`` in line with observed ``remote``.

    Only the states where the provider is the source of truth (PROVISIONING,
    RUNNING, STOPPED) produce actions; anything else is NONE. A provider
    resource that is DELETING or UNKNOWN — or a vanished resource (NOT_FOUND) —
    is never auto-deleted or auto-destroyed: it is contained for review.
    """
    if local not in _REPAIR:
        return StatePlan(StateAction.NONE)
    if remote is ProviderServerState.NOT_FOUND:
        return StatePlan(
            StateAction.CONTAIN,
            reason="provider resource no longer found; never auto-deleted",
        )
    if remote in _IN_PROGRESS[local]:
        return StatePlan(StateAction.IN_PROGRESS, reason=f"provider still {remote.value}")
    repair = _REPAIR[local].get(remote)
    if repair is not None:
        return StatePlan(StateAction.REPAIR, target=repair, reason=f"provider is {remote.value}")
    # remote matches local exactly -> nothing to do
    _match = {
        ServerLifecycleState.RUNNING: ProviderServerState.RUNNING,
        ServerLifecycleState.STOPPED: ProviderServerState.STOPPED,
    }
    if _match.get(local) is remote:
        return StatePlan(StateAction.NONE, reason="matches provider")
    return StatePlan(
        StateAction.CONTAIN,
        reason=f"unexpected provider state {remote.value} for local {local.value}",
    )


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------


class OperationRepository(Protocol):
    """Port for the durable operation ledger."""

    async def get_or_create(
        self,
        *,
        operation_key: str,
        operation_type: OperationType,
        resource_type: str,
        resource_id: UUID,
        provider_key: str,
    ) -> Operation:
        """Return the operation for the key, creating it PENDING if absent.

        The unique key makes concurrent creates resolve to one row.
        """
        ...

    async def get(self, operation_id: UUID) -> Operation | None:
        """One operation by id, or None."""
        ...

    async def get_by_key(self, operation_key: str) -> Operation | None:
        """One operation by its unique key, or None."""
        ...

    async def list_in_flight(self, operation_type: OperationType) -> list[Operation]:
        """All IN_FLIGHT operations of a type (reconciliation candidates)."""
        ...

    async def list_pending(self, operation_types: Sequence[OperationType]) -> list[Operation]:
        """All PENDING operations of the given types (worker queue)."""
        ...

    async def claim(self, operation_id: UUID) -> Operation | None:
        """Atomically claim a PENDING operation (-> IN_FLIGHT, attempts+1).

        Returns the claimed operation, or None when another worker won the
        claim (or the operation is not PENDING).
        """
        ...

    async def save(self, operation: Operation) -> Operation:
        """Persist status/correlation changes of a claimed operation."""
        ...

    async def list_failed(
        self,
        *,
        operation_types: Sequence[OperationType] | None = None,
        limit: int = 50,
    ) -> list[Operation]:
        """FAILED operations, most recently updated first (inspection)."""
        ...
