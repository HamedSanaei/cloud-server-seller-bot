"""Admin REST surface (M14-003).

Acceptance: search/actions are RBAC/audited.

Every endpoint requires an authenticated actor whose platform User holds
the admin permission for that operation (application-layer RBAC via
:class:`PermissionChecker`); a non-admin gets the same stable ``403
forbidden`` envelope as an unknown id - no existence leaks. Every
mutation is audited through :class:`AuditTrail` as an ADMIN-actor event
WITH a non-empty reason (the audit chokepoint itself rejects admin
events without one) and requires the v1 idempotency key.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends

from cloud_platform.core.container import get_container
from cloud_platform.modules.audit.domain import ActorType, AuditRepository
from cloud_platform.modules.audit.repository import SqlAlchemyAuditRepository
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.users.domain import (
    Permission,
    PermissionChecker,
    PermissionDeniedError,
    Role,
    User,
    UserStatus,
)
from cloud_platform.modules.users.repository import SqlAlchemyUserRepository

from .dependencies import get_token_authentication, require_idempotency_key
from .errors import ApiError, ErrorCode

router = APIRouter(prefix="/v1/admin", tags=["admin"])


async def _user_repo() -> SqlAlchemyUserRepository:
    container = await get_container()
    return SqlAlchemyUserRepository(container.session_factory)


async def _audit_repo() -> AuditRepository:
    container = await get_container()
    return SqlAlchemyAuditRepository(container.session_factory)


async def _audit_trail(
    repo: Annotated[AuditRepository, Depends(_audit_repo)],
) -> AuditTrail:
    """The audit chokepoint over the request-scoped repository."""
    return AuditTrail(repo)


def _admin_auth(
    permission: Permission,
) -> Callable[[Any, SqlAlchemyUserRepository], Awaitable[User]]:
    """Dependency factory enforcing one admin permission (RBAC)."""

    async def _checker(
        auth: Annotated[Any, Depends(get_token_authentication)],
        repo: Annotated[SqlAlchemyUserRepository, Depends(_user_repo)],
    ) -> User:
        user = await repo.get(auth.user_id)
        if user is None or user.role is not Role.ADMIN:
            # identical envelope whether or not the id exists - no leak
            raise ApiError(ErrorCode.FORBIDDEN, "admin privileges required")
        checker = PermissionChecker(user)
        try:
            checker.require_active()
            checker.require(permission)
        except PermissionDeniedError as exc:
            raise ApiError(ErrorCode.FORBIDDEN, str(exc)) from None
        return user

    return _checker


# ---------------------------------------------------------------------------
# Users: search / detail / status actions
# ---------------------------------------------------------------------------


@router.get("/users")
async def search_users(
    _actor: Annotated[User, Depends(_admin_auth(Permission.ADMIN_MANAGE_USERS))],
    repo: Annotated[SqlAlchemyUserRepository, Depends(_user_repo)],
    q: str = "",
    offset: int = 0,
    limit: int = 20,
) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    users = await repo.search(q, offset=offset, limit=limit)
    return {
        "users": [
            {
                "id": str(u.id),
                "username": u.username,
                "email": u.email,
                "status": u.status.value,
                "role": u.role.value,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            }
            for u in users
        ],
        "offset": offset,
        "limit": limit,
    }


@router.get("/users/{user_id}")
async def get_user(
    user_id: UUID,
    _actor: Annotated[User, Depends(_admin_auth(Permission.ADMIN_MANAGE_USERS))],
    repo: Annotated[SqlAlchemyUserRepository, Depends(_user_repo)],
) -> dict[str, Any]:
    user = await repo.get(user_id)
    if user is None:
        raise ApiError(ErrorCode.NOT_FOUND, f"user {user_id} not found")
    return {
        "user": {
            "id": str(user.id),
            "username": user.username,
            "email": user.email,
            "status": user.status.value,
            "role": user.role.value,
            "terms_version": user.terms_version,
            "created_at": user.created_at.isoformat() if user.created_at else None,
        }
    }


@router.post("/users/{user_id}/status")
async def set_user_status(
    user_id: UUID,
    body: dict[str, Any],
    actor: Annotated[User, Depends(_admin_auth(Permission.ADMIN_MANAGE_USERS))],
    repo: Annotated[SqlAlchemyUserRepository, Depends(_user_repo)],
    audit: Annotated[AuditTrail, Depends(_audit_trail)],
    _key: Annotated[str, Depends(require_idempotency_key)],
) -> dict[str, Any]:
    status_raw = str(body.get("status") or "")
    reason = str(body.get("reason") or "").strip()
    if not reason:
        raise ApiError(ErrorCode.VALIDATION_ERROR, "a reason is required for status changes")
    try:
        status = UserStatus(status_raw)
    except ValueError:
        valid = sorted(s.value for s in UserStatus)
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, f"unknown status {status_raw!r}; valid: {valid}"
        ) from None
    target = await repo.get(user_id)
    if target is None:
        raise ApiError(ErrorCode.NOT_FOUND, f"user {user_id} not found")
    updated = await repo.update_status(user_id, status)
    await audit.record_mutation(
        actor_type=ActorType.ADMIN,
        actor_id=actor.id,
        action="admin.user.status_changed",
        resource_type="user",
        resource_id=str(user_id),
        reason=reason,
        metadata={"status": status.value},
    )
    return {"user": {"id": str(updated.id), "status": updated.status.value}}


# ---------------------------------------------------------------------------
# Audit trail query
# ---------------------------------------------------------------------------


@router.get("/audit")
async def query_audit(
    _actor: Annotated[User, Depends(_admin_auth(Permission.ADMIN_MANAGE_SETTINGS))],
    events: Annotated[AuditRepository, Depends(_audit_repo)],
    resource_type: str | None = None,
    resource_id: str | None = None,
    actor_id: UUID | None = None,
) -> dict[str, Any]:
    if actor_id is not None:
        found = await events.get_by_actor(actor_id)
    elif resource_type and resource_id:
        found = await events.get_by_resource(resource_type, resource_id)
    else:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "filter by actor_id or by resource_type+resource_id",
        )
    return {
        "events": [
            {
                "action": e.action,
                "actor_type": e.actor_type.value,
                "actor_id": str(e.actor_id) if e.actor_id else None,
                "resource_type": e.resource_type,
                "resource_id": e.resource_id,
                "reason": e.reason,
                "metadata": e.metadata,
                "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
            }
            for e in found
        ]
    }
