"""Containment domain: user freeze + resource containment (M10-007).

Containment is an explicit, admin-authorized operation that is **audited and
bounded**:

- The user is frozen (spending and provisioning stop). A banned user is
  already more strictly contained and is left untouched.
- Each of the user's servers that can be held (see ``CONTAINABLE_STATES``)
  is moved to ``MANUAL_REVIEW``. ``REQUESTED`` (nothing provisioned yet) and
  ``DELETED`` (nothing left) servers are skipped, never destroyed.
- Re-containment is idempotent; release returns each server to exactly the
  state it was in when contained.
- Every mutation (freeze, contain, unfreeze, release) is recorded in the
  append-only audit trail with a non-empty reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from cloud_platform.modules.compute.domain import (
    CONTAINABLE_STATES,
    CloudServer,
    ServerLifecycleState,
)
from cloud_platform.modules.users.domain import User, UserStatus


class ContainmentError(Exception):
    """Base error for containment operations."""


class UserAction(StrEnum):
    """Outcome of the user part of a containment/release command."""

    FROZEN = "frozen"
    ALREADY_FROZEN = "already_frozen"
    SKIPPED_BANNED = "skipped_banned"
    UNFROZEN = "unfrozen"
    ALREADY_ACTIVE = "already_active"


class ServerAction(StrEnum):
    """Outcome for a single server during containment/release."""

    CONTAINED = "contained"
    ALREADY_CONTAINED = "already_contained"
    RELEASED = "released"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ContainedServer:
    """Per-server outcome of a containment or release command."""

    server_id: UUID
    action: ServerAction
    from_state: ServerLifecycleState | None = None
    to_state: ServerLifecycleState | None = None


@dataclass(frozen=True, slots=True)
class ContainmentResult:
    """Aggregate outcome of a containment or release command."""

    user_id: UUID
    user_action: UserAction
    user_status: UserStatus
    servers: tuple[ContainedServer, ...]

    @property
    def contained_server_ids(self) -> tuple[UUID, ...]:
        """Servers currently under containment after this command."""
        return tuple(
            s.server_id for s in self.servers if s.to_state is ServerLifecycleState.MANUAL_REVIEW
        )

    @property
    def changed(self) -> bool:
        """Whether this command mutated anything."""
        return self.user_action in (UserAction.FROZEN, UserAction.UNFROZEN) or any(
            s.action in (ServerAction.CONTAINED, ServerAction.RELEASED) for s in self.servers
        )


# ---------------------------------------------------------------------------
# Pure planning functions
# ---------------------------------------------------------------------------


def plan_user_freeze(user: User) -> UserAction:
    """Decide what to do to the user for a containment command."""
    if user.status is UserStatus.BANNED:
        return UserAction.SKIPPED_BANNED
    if user.status is UserStatus.FROZEN:
        return UserAction.ALREADY_FROZEN
    return UserAction.FROZEN


def plan_user_release(user: User) -> UserAction:
    """Decide what to do to the user for a release command (non-banned)."""
    if user.status is UserStatus.FROZEN:
        return UserAction.UNFROZEN
    return UserAction.ALREADY_ACTIVE


def plan_server_containment(state: ServerLifecycleState) -> ServerAction:
    """Decide what to do to a server for a containment command."""
    if state is ServerLifecycleState.MANUAL_REVIEW:
        return ServerAction.ALREADY_CONTAINED
    if state in CONTAINABLE_STATES:
        return ServerAction.CONTAINED
    return ServerAction.SKIPPED


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------


class ServerRepository(Protocol):
    """Port for the servers a containment command operates on."""

    async def list_by_user(self, user_id: UUID) -> list[CloudServer]:
        """All servers owned by the user, in stable order."""
        ...

    async def save(self, server: CloudServer) -> CloudServer:
        """Persist a server's current state."""
        ...
