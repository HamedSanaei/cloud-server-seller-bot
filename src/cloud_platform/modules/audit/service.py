"""Application-layer audit facade enforcing actor/reason linkage.

All services must record mutations through :class:`AuditTrail` rather than
appending raw events. This makes the invariant "every mutation links to
actor and reason" structural: an ADMIN-actor mutation without a non-empty
reason cannot reach the append-only log.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from cloud_platform.modules.audit.domain import (
    ActorType,
    AuditError,
    AuditEvent,
    AuditRepository,
)

logger = logging.getLogger(__name__)


class AuditTrail:
    """Single chokepoint through which application services record mutations."""

    def __init__(self, repo: AuditRepository) -> None:
        self._repo = repo

    async def record_mutation(
        self,
        *,
        actor_type: ActorType,
        action: str,
        resource_type: str,
        resource_id: str = "",
        actor_id: UUID | None = None,
        reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Persist one audited mutation.

        Admin-actor mutations require a non-empty reason; system and user
        actors may omit it (automated jobs often have no human rationale).

        Raises:
            AuditError: If an admin mutation lacks a reason.
        """
        if actor_type is ActorType.ADMIN and (not reason or not reason.strip()):
            raise AuditError("admin mutations must carry a non-empty reason")
        event = AuditEvent(
            actor_type=actor_type,
            action=action,
            resource_type=resource_type,
            actor_id=actor_id,
            resource_id=resource_id,
            reason=reason,
            metadata=metadata or {},
        )
        logger.info(
            "audit %s by %s on %s/%s",
            action,
            actor_type.value,
            resource_type,
            resource_id or "-",
        )
        return await self._repo.append(event)
