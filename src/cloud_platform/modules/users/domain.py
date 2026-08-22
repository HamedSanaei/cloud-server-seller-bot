"""User management module: aggregate, port, and SQLAlchemy adapter."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

# ---------------------------------------------------------------------------
# RBAC — Permission & Role enums
# ---------------------------------------------------------------------------


class Permission(StrEnum):
    """Granular permissions for the application layer.

    These strings are persisted in the database and compared by name, so they
    must remain stable across releases.
    """

    SERVER_CREATE = "server:create"
    SERVER_DESTROY = "server:destroy"
    SERVER_LIST = "server:list"
    SERVER_RESTART = "server:restart"
    BILLING_VIEW = "billing:view"
    BILLING_PAY = "billing:pay"
    ADMIN_MANAGE_USERS = "admin:manage_users"
    ADMIN_MANAGE_SETTINGS = "admin:manage_settings"


class Role(StrEnum):
    """Predefined roles."""

    USER = "user"
    ADMIN = "admin"


# Mapping of role -> allowed permissions
PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.USER: frozenset(
        {
            Permission.SERVER_CREATE,
            Permission.SERVER_LIST,
            Permission.SERVER_RESTART,
            Permission.BILLING_VIEW,
            Permission.BILLING_PAY,
        }
    ),
    Role.ADMIN: frozenset(
        {
            Permission.SERVER_CREATE,
            Permission.SERVER_DESTROY,
            Permission.SERVER_LIST,
            Permission.SERVER_RESTART,
            Permission.BILLING_VIEW,
            Permission.BILLING_PAY,
            Permission.ADMIN_MANAGE_USERS,
            Permission.ADMIN_MANAGE_SETTINGS,
        }
    ),
}


# ---------------------------------------------------------------------------
# Domain exceptions
# ---------------------------------------------------------------------------


class PermissionDeniedError(RuntimeError):
    """Raised when a user lacks the required permission."""


class InvalidUserTransition(ValueError):
    """Raised when an invalid transition to a User status is attempted."""


class UserNotFound(LookupError):
    """Raised when a User is not found in the repository."""


# ---------------------------------------------------------------------------
# UserStatus
# ---------------------------------------------------------------------------


class UserStatus(StrEnum):
    ACTIVE = "active"
    FROZEN = "frozen"
    BANNED = "banned"


# ---------------------------------------------------------------------------
# User aggregate
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class User:
    """User domain aggregate.

    Attributes:
        id: Unique user id, None until persisted.
        username: Unique username.
        email: Unique email address.
        status: Current user status.
        role: User role (USER or ADMIN).
        terms_accepted_at: When the user accepted the latest terms.
        created_at: Record creation timestamp.
        updated_at: Last update timestamp.
    """

    id: uuid.UUID | None = None
    username: str = ""
    email: str = ""
    status: UserStatus = UserStatus.ACTIVE
    role: Role = Role.USER
    terms_accepted_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.username or not self.username.strip():
            raise ValueError("username must not be empty")
        if not self.email or "@" not in self.email:
            raise ValueError("email must be a valid address")

    @property
    def is_active(self) -> bool:
        return self.status is UserStatus.ACTIVE

    @property
    def can_spend(self) -> bool:
        """Whether the user is allowed to spend from their wallet."""
        return self.status is UserStatus.ACTIVE

    @property
    def can_provision(self) -> bool:
        """Whether the user is allowed to provision servers."""
        return self.status is UserStatus.ACTIVE

    def has_permission(self, permission: Permission) -> bool:
        """Check if this user has the given permission via their role."""
        allowed: frozenset[Permission] = PERMISSIONS.get(self.role, frozenset())
        return permission in allowed

    def accept_terms(self, at: datetime | None = None) -> None:
        """Record acceptance of the latest terms; defaults to now (UTC)."""
        self.terms_accepted_at = at or datetime.now(UTC)

    def freeze(self) -> None:
        """Freeze the user. Only allowed from ACTIVE."""
        if self.status is not UserStatus.ACTIVE:
            raise InvalidUserTransition(f"cannot freeze user from status {self.status.value}")
        self.status = UserStatus.FROZEN

    def unfreeze(self) -> None:
        """Unfreeze the user. Only allowed from FROZEN."""
        if self.status is not UserStatus.FROZEN:
            raise InvalidUserTransition(f"cannot unfreeze user from status {self.status.value}")
        self.status = UserStatus.ACTIVE

    def ban(self) -> None:
        """Ban the user. Allowed only from ACTIVE or FROZEN."""
        if self.status is UserStatus.BANNED:
            raise InvalidUserTransition(f"cannot ban user from status {self.status.value}")
        self.status = UserStatus.BANNED


# ---------------------------------------------------------------------------
# PermissionChecker — application-layer authorization service
# ---------------------------------------------------------------------------


class PermissionChecker:
    """Enforces permissions in the application layer.

    Usage::

        checker = PermissionChecker(user)
        checker.require(Permission.SERVER_DESTROY)  # raises PermissionDeniedError
        checker.check(Permission.BILLING_PAY)       # returns True/False
    """

    def __init__(self, user: User) -> None:
        self._user = user

    @property
    def user(self) -> User:
        return self._user

    def check(self, permission: Permission) -> bool:
        """Return True if the user has *permission*."""
        return self._user.has_permission(permission)

    def require(self, permission: Permission) -> None:
        """Raise PermissionDeniedError if the user lacks *permission*."""
        if not self._user.has_permission(permission):
            raise PermissionDeniedError(
                f"user {self._user.id} (role={self._user.role.value}) "
                f"lacks permission {permission.value}"
            )

    def require_active(self) -> None:
        """Raise PermissionDeniedError if the user is not active."""
        if not self._user.is_active:
            raise PermissionDeniedError(
                f"user {self._user.id} is {self._user.status.value}; active required"
            )

    def require_spend(self) -> None:
        """Raise if the user is not active and cannot spend."""
        if not self._user.can_spend:
            raise PermissionDeniedError(
                f"user {self._user.id} cannot spend (status={self._user.status.value})"
            )


# ---------------------------------------------------------------------------
# UserRepository port
# ---------------------------------------------------------------------------


class UserRepository(Protocol):
    """Protocol defining the repository interface for the User aggregate."""

    async def create(self, user: User) -> User: ...

    async def get(self, user_id: uuid.UUID) -> User | None: ...

    async def get_by_username(self, username: str) -> User | None: ...

    async def get_by_email(self, email: str) -> User | None: ...

    async def get_by_telegram_user_id(self, telegram_user_id: int) -> User | None: ...

    async def update_status(self, user_id: uuid.UUID, status: UserStatus) -> User: ...
