"""Dead-letter / manual-review tooling (M11-004).

Operator tooling over the operation ledger:

- **Inspect**: list FAILED operations (the dead letter queue) and fetch one
  by key; read-only, no state change.
- **Replay safely**: reopen a FAILED operation to PENDING under the same
  operation key. The worker then re-sends the identical key; safety comes
  from the operation having FAILED DEFINITIVELY (the provider rejected the
  mutation — nothing was created) — NOT from provider-side deduplication,
  which providers like Leaseweb ordering do not offer. Replays are
  admin-gated (``admin:manage_settings``), require a non-empty reason, and
  are audited (``operation.retry``).
- **Manual review queue**: the servers in MANUAL_REVIEW with the failed
  operations recorded for each of them, so an operator sees the full picture
  before deciding (release via containment, or fix and retry the operation).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from cloud_platform.modules.audit.domain import ActorType, AuditRepository
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.compute.domain import (
    CloudServer,
    ServerRepository,
)
from cloud_platform.modules.operations.domain import (
    Operation,
    OperationRepository,
    OperationStatus,
    OperationType,
)
from cloud_platform.modules.users.domain import (
    Permission,
    PermissionChecker,
    User,
)


class OperationToolingError(Exception):
    """Base error for the operation tooling."""


class OperationNotRetryableError(OperationToolingError):
    """The operation's status does not allow a safe replay."""


@dataclass(frozen=True, slots=True)
class ManualReviewItem:
    """One server awaiting manual review plus its failed operations."""

    server: CloudServer
    failed_operations: tuple[Operation, ...]


class OperationToolingService:
    """Inspect dead-lettered operations and reopen them for safe replay."""

    def __init__(
        self,
        operation_repo: OperationRepository,
        server_repo: ServerRepository,
        audit_repo: AuditRepository,
    ) -> None:
        self._ops = operation_repo
        self._servers = server_repo
        self._audit = AuditTrail(audit_repo)

    @staticmethod
    def _actor_context(actor: User | None) -> tuple[ActorType, UUID | None]:
        if actor is None:
            return ActorType.SYSTEM, None
        return ActorType.ADMIN, actor.id

    # -- inspection --------------------------------------------------------

    async def list_failed(
        self,
        *,
        operation_types: list[OperationType] | None = None,
        limit: int = 50,
    ) -> list[Operation]:
        """The dead-letter queue: FAILED operations, newest first."""
        if limit < 1:
            raise ValueError("limit must be >= 1")
        return await self._ops.list_failed(operation_types=operation_types, limit=limit)

    async def get_operation(self, operation_key: str) -> Operation | None:
        """One operation by key, or None."""
        return await self._ops.get_by_key(operation_key)

    # -- safe replay ---------------------------------------------------------

    async def retry_failed(
        self,
        operation_key: str,
        *,
        actor: User | None,
        reason: str,
    ) -> Operation:
        """Reopen a FAILED operation so the worker re-sends it (same key).

        - PENDING: already re-queued — returns the operation unchanged
          (idempotent, no state change, no audit).
        - COMPLETED: rejected — the provider mutation already applied;
          replaying would be a double-apply, never "safe".
        - IN_FLIGHT: rejected — another worker owns the attempt.
        - FAILED: reopened to PENDING (manual-only transition), saved,
          audited as ``operation.retry``.
        """
        if actor is not None:
            PermissionChecker(actor).require(Permission.ADMIN_MANAGE_SETTINGS)
        if not reason or not reason.strip():
            raise OperationToolingError("retry commands must carry a non-empty reason")

        operation = await self._ops.get_by_key(operation_key)
        if operation is None:
            raise LookupError(f"operation {operation_key!r} not found")

        if operation.status is OperationStatus.PENDING:
            return operation
        if operation.status is OperationStatus.COMPLETED:
            raise OperationNotRetryableError(
                f"operation {operation_key!r} already completed; the provider "
                "mutation applied and a replay would double-apply"
            )
        if operation.status is OperationStatus.IN_FLIGHT:
            raise OperationNotRetryableError(
                f"operation {operation_key!r} is in flight; a worker owns it"
            )

        operation.reopen_for_retry()
        saved = await self._ops.save(operation)
        actor_type, actor_id = self._actor_context(actor)
        await self._audit.record_mutation(
            actor_type=actor_type,
            actor_id=actor_id,
            action="operation.retry",
            resource_type="operation",
            resource_id=operation_key,
            reason=reason,
            metadata={
                "operation_type": operation.operation_type.value,
                "provider": operation.provider_key,
                "attempts_before": str(operation.attempts),
            },
        )
        return saved

    # -- manual review queue -------------------------------------------------

    async def review_queue(self) -> list[ManualReviewItem]:
        """Every MANUAL_REVIEW server with its failed operations attached."""
        servers = await self._servers.list_manual_review()
        if not servers:
            return []
        failed = await self._ops.list_failed(limit=10_000)
        by_resource: dict[tuple[str, UUID], list[Operation]] = {}
        for op in failed:
            by_resource.setdefault((op.resource_type, op.resource_id), []).append(op)
        items: list[ManualReviewItem] = []
        for server in servers:
            ops = tuple(by_resource.get(("server", server.id), ()))
            items.append(ManualReviewItem(server=server, failed_operations=ops))
        return items
