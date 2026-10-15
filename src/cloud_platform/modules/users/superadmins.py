"""Configuration-backed Telegram Super Admin authorization, using existing RBAC.

The persisted ``admin`` role remains the platform's privileged RBAC role. A
Telegram management identity additionally has to be explicitly configured; a
notification chat, username, forwarded message or claimed API identity is not
an authorization source. Configuration reconciliation owns role assignment.
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from cloud_platform.modules.users.domain import (
    Permission,
    PermissionDeniedError,
    Role,
    User,
    UserStatus,
)


def validate_telegram_user_ids(values: object) -> tuple[int, ...]:
    """Canonicalize a collection of strict positive PostgreSQL BIGINT IDs."""
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise ValueError("superadmin_user_ids must be an array of integer Telegram User IDs")
    if any(type(value) is not int or not 0 < value <= 2**63 - 1 for value in values):
        raise ValueError("superadmin_user_ids must contain positive signed 64-bit integers")
    return tuple(sorted(set(values)))


def is_superadmin(user: object, configured_ids: Iterable[int]) -> bool:
    """Fail closed unless configured identity, persisted active role and RBAC agree."""
    return (
        isinstance(user, User)
        and isinstance(user.id, UUID)
        and type(user.telegram_user_id) is int
        and user.telegram_user_id > 0
        and any(type(value) is int and value == user.telegram_user_id for value in configured_ids)
        and user.role is Role.ADMIN
        and user.status is UserStatus.ACTIVE
        and user.has_permission(Permission.ADMIN_MANAGE_SETTINGS)
        and user.has_permission(Permission.WALLET_ADJUST)
    )


def require_superadmin(user: object, configured_ids: Iterable[int]) -> User:
    if not is_superadmin(user, configured_ids):
        raise PermissionDeniedError("configured active Telegram Super Admin required")
    assert isinstance(user, User)
    return user
