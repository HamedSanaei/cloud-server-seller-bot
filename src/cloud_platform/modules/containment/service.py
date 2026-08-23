"""Containment service: user freeze + resource containment command (M10-007).

The command is explicit, admin-authorized, audited and bounded:

- ``contain_user`` freezes the user (if active) and moves every containable
  server to ``MANUAL_REVIEW``, recording the pre-containment state so release
  is exact. Banned users are skipped (already more strictly contained);
  requested/deleted servers are skipped (nothing to hold). Nothing is ever
  destroyed by containment.
- ``release_user`` reverses a containment: unfreezes the user and returns
  each contained server to exactly the state it was in when contained. A
  banned user cannot be released this way (banning is a separate, harsher
  workflow).
- Every mutation is recorded through :class:`AuditTrail` with a non-empty
  reason; idempotent replays that change nothing emit no audit events.
"""

from __future__ import annotations

import logging
from uuid import UUID

from cloud_platform.modules.audit.domain import ActorType, AuditRepository
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.compute.domain import ServerLifecycleState
from cloud_platform.modules.containment.domain import (
    ContainedServer,
    ContainmentError,
    ContainmentResult,
    ServerAction,
    ServerRepository,
    UserAction,
    plan_server_containment,
    plan_user_freeze,
    plan_user_release,
)
from cloud_platform.modules.users.domain import (
    Permission,
    PermissionChecker,
    User,
    UserRepository,
    UserStatus,
)

logger = logging.getLogger(__name__)


class ContainmentService:
    """Freeze a user and contain/release their resources, with audit."""

    def __init__(
        self,
        user_repo: UserRepository,
        server_repo: ServerRepository,
        audit_repo: AuditRepository,
    ) -> None:
        self._users = user_repo
        self._servers = server_repo
        self._audit = AuditTrail(audit_repo)

    @staticmethod
    def _actor_context(actor: User | None) -> tuple[ActorType, UUID | None]:
        if actor is None:
            return ActorType.SYSTEM, None
        return ActorType.ADMIN, actor.id

    @staticmethod
    def _require_reason(reason: str) -> None:
        if not reason or not reason.strip():
            raise ContainmentError("containment commands must carry a non-empty reason")

    async def contain_user(
        self,
        *,
        user_id: UUID,
        actor: User | None,
        reason: str,
        case_id: UUID | None = None,
    ) -> ContainmentResult:
        """Freeze the user and contain all containable servers.

        Idempotent: re-running on an already-contained user changes nothing
        and records no audit events.
        """
        if actor is not None:
            PermissionChecker(actor).require(Permission.ADMIN_MANAGE_USERS)
        self._require_reason(reason)

        user = await self._users.get(user_id)
        if user is None:
            raise LookupError(f"user {user_id} not found")

        user_action = plan_user_freeze(user)
        if user_action is UserAction.FROZEN:
            user.freeze()
            user = await self._users.update_status(user_id, UserStatus.FROZEN)
            actor_type, actor_id = self._actor_context(actor)
            await self._audit.record_mutation(
                actor_type=actor_type,
                actor_id=actor_id,
                action="user.freeze",
                resource_type="user",
                resource_id=str(user_id),
                reason=reason,
                metadata={"case_id": str(case_id)} if case_id is not None else None,
            )

        outcomes: list[ContainedServer] = []
        newly_contained: list[dict[str, str]] = []
        for server in await self._servers.list_by_user(user_id):
            action = plan_server_containment(server.state)
            if action is ServerAction.CONTAINED:
                prior = server.contain()
                saved = await self._servers.save(server)
                outcomes.append(
                    ContainedServer(
                        server_id=server.id,
                        action=ServerAction.CONTAINED,
                        from_state=prior,
                        to_state=saved.state,
                    )
                )
                newly_contained.append({"server_id": str(server.id), "from": prior.value})
            elif action is ServerAction.ALREADY_CONTAINED:
                outcomes.append(
                    ContainedServer(
                        server_id=server.id,
                        action=ServerAction.ALREADY_CONTAINED,
                        from_state=server.contained_from,
                        to_state=ServerLifecycleState.MANUAL_REVIEW,
                    )
                )
            else:
                outcomes.append(
                    ContainedServer(
                        server_id=server.id,
                        action=ServerAction.SKIPPED,
                        from_state=server.state,
                    )
                )

        if newly_contained:
            actor_type, actor_id = self._actor_context(actor)
            await self._audit.record_mutation(
                actor_type=actor_type,
                actor_id=actor_id,
                action="server.contain",
                resource_type="user",
                resource_id=str(user_id),
                reason=reason,
                metadata={
                    "servers": newly_contained,
                    "case_id": str(case_id) if case_id is not None else None,
                },
            )

        result = ContainmentResult(
            user_id=user_id,
            user_action=user_action,
            user_status=user.status,
            servers=tuple(outcomes),
        )
        logger.info(
            "contained user %s: user=%s servers=%d",
            user_id,
            user_action.value,
            len(newly_contained),
        )
        return result

    async def release_user(
        self,
        *,
        user_id: UUID,
        actor: User | None,
        reason: str,
    ) -> ContainmentResult:
        """Unfreeze the user and release all contained servers.

        A banned user is refused: release reverses a freeze, never a ban.
        """
        if actor is not None:
            PermissionChecker(actor).require(Permission.ADMIN_MANAGE_USERS)
        self._require_reason(reason)

        user = await self._users.get(user_id)
        if user is None:
            raise LookupError(f"user {user_id} not found")
        if user.status is UserStatus.BANNED:
            raise ContainmentError(f"user {user_id} is banned; release refused")

        user_action = plan_user_release(user)
        if user_action is UserAction.UNFROZEN:
            user.unfreeze()
            user = await self._users.update_status(user_id, UserStatus.ACTIVE)
            actor_type, actor_id = self._actor_context(actor)
            await self._audit.record_mutation(
                actor_type=actor_type,
                actor_id=actor_id,
                action="user.unfreeze",
                resource_type="user",
                resource_id=str(user_id),
                reason=reason,
            )

        outcomes: list[ContainedServer] = []
        released: list[dict[str, str]] = []
        for server in await self._servers.list_by_user(user_id):
            if server.state is ServerLifecycleState.MANUAL_REVIEW:
                prior = server.release()
                saved = await self._servers.save(server)
                outcomes.append(
                    ContainedServer(
                        server_id=server.id,
                        action=ServerAction.RELEASED,
                        from_state=ServerLifecycleState.MANUAL_REVIEW,
                        to_state=saved.state,
                    )
                )
                released.append({"server_id": str(server.id), "to": prior.value})
            else:
                outcomes.append(
                    ContainedServer(
                        server_id=server.id,
                        action=ServerAction.SKIPPED,
                        from_state=server.state,
                    )
                )

        if released:
            actor_type, actor_id = self._actor_context(actor)
            await self._audit.record_mutation(
                actor_type=actor_type,
                actor_id=actor_id,
                action="server.release",
                resource_type="user",
                resource_id=str(user_id),
                reason=reason,
                metadata={"servers": released},
            )

        result = ContainmentResult(
            user_id=user_id,
            user_action=user_action,
            user_status=user.status,
            servers=tuple(outcomes),
        )
        logger.info(
            "released user %s: user=%s servers=%d",
            user_id,
            user_action.value,
            len(released),
        )
        return result
