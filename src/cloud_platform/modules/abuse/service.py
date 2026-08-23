"""Application service for abuse intake and case transitions.

Intake is the "quick map" in M10-006: a reported provider resource is
resolved to its responsible user, an audited ``AbuseCase`` is created
carrying that mapping, and the action is recorded in the append-only audit
log. Every mutation goes through :class:`AuditTrail`, so admin-actor cases
must carry a non-empty reason.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from cloud_platform.modules.abuse.domain import (
    AbuseCase,
    AbuseCaseRepository,
    AbuseStatus,
    ResourceNotManagedError,
    ResourceOwnershipResolver,
    ResourceRef,
)
from cloud_platform.modules.audit.domain import ActorType, AuditRepository
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.users.domain import Permission, PermissionChecker, User

logger = logging.getLogger(__name__)


class AbuseIntakeService:
    """Records abuse findings and drives their review workflow."""

    def __init__(
        self,
        resolver: ResourceOwnershipResolver,
        case_repo: AbuseCaseRepository,
        audit_repo: AuditRepository,
    ) -> None:
        self._resolver = resolver
        self._cases = case_repo
        self._audit = AuditTrail(audit_repo)

    async def intake(
        self,
        *,
        ref: ResourceRef,
        reason: str,
        reporter: str,
        actor: User | None = None,
    ) -> AbuseCase:
        """Map a reported resource to its user and record an audited case.

        ``actor`` is the acting admin (internal reporting); when None the
        report is treated as external (e.g. a provider abuse notice) and the
        audit actor is SYSTEM.

        Raises:
            PermissionDeniedError: If a non-admin user reports.
            ResourceNotManagedError: If the resource maps to no managed server.
            ValueError: On empty reason/reporter (via domain validation).
        """
        if actor is not None:
            PermissionChecker(actor).require(Permission.ADMIN_MANAGE_USERS)

        ownership = await self._resolver.resolve(ref)
        if ownership is None:
            raise ResourceNotManagedError(
                f"{ref.provider_key}/{ref.resource_type.value}/{ref.resource_id} "
                "does not map to a managed server"
            )

        case = AbuseCase(
            resource=ref,
            user_id=ownership.user_id,
            server_id=ownership.server_id,
            reason=reason,
            reporter=reporter,
        )
        created = await self._cases.create(case)
        assert created.id is not None  # persisted cases carry an id

        await self._audit.record_mutation(
            actor_type=ActorType.ADMIN if actor is not None else ActorType.SYSTEM,
            actor_id=actor.id if actor is not None else None,
            action="abuse.intake",
            resource_type="user",
            resource_id=str(ownership.user_id),
            reason=reason,
            metadata={
                "abuse_case_id": str(created.id),
                "provider_key": ref.provider_key,
                "resource": f"{ref.resource_type.value}:{ref.resource_id}",
            },
        )
        logger.info("abuse case %s opened for user %s", created.id, ownership.user_id)
        return created

    async def transition(
        self,
        *,
        case_id: UUID,
        target: AbuseStatus,
        reason: str,
        actor: User | None = None,
        at: datetime | None = None,
    ) -> AbuseCase:
        """Move a case to ``target`` status and audit the change.

        Raises:
            LookupError: If the case does not exist.
            InvalidAbuseTransition: If the move violates the lifecycle.
            PermissionDeniedError: If a non-admin user attempts the move.
        """
        if actor is not None:
            PermissionChecker(actor).require(Permission.ADMIN_MANAGE_USERS)

        case = await self._cases.get(case_id)
        if case is None:
            raise LookupError(f"abuse case {case_id} not found")

        moved = case.transition(target, at=at or datetime.now(UTC))
        saved = await self._cases.save(moved)

        await self._audit.record_mutation(
            actor_type=ActorType.ADMIN if actor is not None else ActorType.SYSTEM,
            actor_id=actor.id if actor is not None else None,
            action="abuse.transition",
            resource_type="abuse_case",
            resource_id=str(case_id),
            reason=reason,
            metadata={"from": case.status.value, "to": target.value},
        )
        return saved
