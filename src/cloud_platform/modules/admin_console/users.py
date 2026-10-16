"""Application facade for user management in the Telegram admin console.

Every method re-authenticates a configured active Telegram Super Admin before
doing anything else, so the UI layer is never the authorization boundary. All
reads are paginated and bounded. The only mutation delegates to the existing
transactional ``change_status_idempotently`` operation, so fresh authorization
and the immutable audit receipt stay inside that one transaction. This facade
never creates accounts or wallets, promotes roles, moves money, or touches a
provider.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from cloud_platform.modules.compute.domain import CloudServer, ServerRepository
from cloud_platform.modules.users.domain import (
    PermissionDeniedError,
    User,
    UserNotFound,
    UserRepository,
    UserStatus,
)
from cloud_platform.modules.users.superadmins import require_superadmin, validate_telegram_user_ids
from cloud_platform.modules.wallet.domain import Wallet, WalletRepository

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
MAX_QUERY_LENGTH = 100
MAX_REASON_LENGTH = 500
MAX_IDEMPOTENCY_KEY_LENGTH = 128
SERVER_PAGE_LIMIT = 20
_INT64_MAX = 2**63 - 1


class AdminUserRepository(UserRepository, Protocol):
    """The user port the admin console needs, extending the base repository."""

    async def search(self, query: str, *, offset: int = 0, limit: int = 20) -> list[User]: ...

    async def change_status_idempotently(
        self,
        user_id: UUID,
        status: UserStatus,
        *,
        actor_id: UUID,
        idempotency_key: str,
        reason: str,
        admin_configured_ids: tuple[int, ...] | None = None,
    ) -> UserStatus: ...


@dataclass(frozen=True, slots=True)
class AdminServerSummary:
    """A redacted server row: no provider identifiers or secrets are exposed."""

    server_id: UUID
    provider_key: str
    state: str
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AdminUserSearchPage:
    """One page of an admin user search plus its pagination context."""

    items: tuple[User, ...]
    offset: int
    limit: int


@dataclass(frozen=True, slots=True)
class AdminUserProfile:
    """An admin user profile: user, read-only wallet and a server page."""

    user: User
    wallet: Wallet | None
    servers: tuple[AdminServerSummary, ...]
    server_total: int
    server_offset: int
    server_limit: int


def _validate_pagination(offset: object, limit: object) -> tuple[int, int]:
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if type(limit) is not int or limit < 1:
        raise ValueError("limit must be a positive integer")
    if offset > _INT64_MAX:
        offset = _INT64_MAX
    if limit > MAX_PAGE_SIZE:
        limit = MAX_PAGE_SIZE
    return offset, limit


def _clean_query(query: object) -> str:
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    return query.strip()[:MAX_QUERY_LENGTH]


def _telegram_id_query(clean_query: str) -> int | None:
    """Return the exact Telegram id a numeric query names, or None for text.

    A purely ASCII-decimal query is a Telegram User ID, never a username
    substring. A numeric query that is zero or overflows signed int64 is
    rejected instead of being silently searched as text.
    """
    if not clean_query.isascii() or not clean_query.isdigit():
        return None
    value = int(clean_query)
    if value <= 0 or value > _INT64_MAX:
        raise ValueError("numeric query must be a positive signed 64-bit Telegram id")
    return value


def _validate_target(telegram_user_id: object) -> int:
    return validate_telegram_user_ids([telegram_user_id])[0]


def _server_summary(server: CloudServer) -> AdminServerSummary:
    return AdminServerSummary(
        server_id=server.id,
        provider_key=server.provider_key,
        state=server.state.value,
        created_at=server.created_at,
    )


class AdminUserService:
    """Re-authorized, bounded user-management operations for the admin console."""

    def __init__(
        self,
        user_repo: AdminUserRepository,
        wallet_repo: WalletRepository,
        server_repo: ServerRepository,
        *,
        configured_ids: Iterable[int] = (),
    ) -> None:
        self._users = user_repo
        self._wallets = wallet_repo
        self._servers = server_repo
        self._configured_ids = validate_telegram_user_ids(tuple(configured_ids))

    async def _authorize(self, actor: object) -> User:
        admin = require_superadmin(actor, self._configured_ids)
        if admin.id is None:
            raise PermissionDeniedError("a persisted administrator is required")
        fresh = await self._users.get(admin.id)
        fresh = require_superadmin(fresh, self._configured_ids)
        if fresh.id != admin.id or fresh.telegram_user_id != admin.telegram_user_id:
            raise PermissionDeniedError("persisted Telegram identity does not match actor")
        return fresh

    async def search(
        self, actor: User, query: str, offset: int = 0, limit: int = 20
    ) -> AdminUserSearchPage:
        """Search users by Telegram id or by bounded username/email substring."""
        await self._authorize(actor)
        offset, limit = _validate_pagination(offset, limit)
        clean_query = _clean_query(query)
        telegram_id = _telegram_id_query(clean_query)
        if telegram_id is not None:
            items = await self._match_telegram_id(telegram_id, offset=offset, limit=limit)
            return AdminUserSearchPage(items=items, offset=offset, limit=limit)
        users = await self._users.search(clean_query, offset=offset, limit=limit)
        return AdminUserSearchPage(items=tuple(users), offset=offset, limit=limit)

    async def _match_telegram_id(
        self, telegram_id: int, *, offset: int, limit: int
    ) -> tuple[User, ...]:
        """Exact Telegram-id search: an id matches at most one user at offset 0."""
        if offset > 0:
            return ()
        user = await self._users.get_by_telegram_user_id(telegram_id)
        if user is None:
            return ()
        return (user,)[:limit]

    async def profile(
        self,
        actor: User,
        telegram_user_id: int,
        *,
        server_offset: int = 0,
        server_limit: int = SERVER_PAGE_LIMIT,
    ) -> AdminUserProfile:
        """Read a user's profile and a first page of their servers, read-only."""
        await self._authorize(actor)
        server_offset, server_limit = _validate_pagination(server_offset, server_limit)
        server_limit = min(server_limit, SERVER_PAGE_LIMIT)
        target_id = _validate_target(telegram_user_id)
        user = await self._users.get_by_telegram_user_id(target_id)
        if user is None or user.id is None:
            raise UserNotFound(f"Telegram user {target_id} not found")
        wallet = await self._wallets.get(user.id)
        servers, total = await self._servers.list_by_user_paged(
            user.id, offset=server_offset, limit=server_limit
        )
        return AdminUserProfile(
            user=user,
            wallet=wallet,
            servers=tuple(_server_summary(server) for server in servers),
            server_total=total,
            server_offset=server_offset,
            server_limit=server_limit,
        )

    async def set_status(
        self,
        actor: User,
        telegram_user_id: int,
        status: UserStatus,
        *,
        reason: str,
        idempotency_key: str,
    ) -> UserStatus:
        """Change a user's status through the existing transactional operation."""
        admin = await self._authorize(actor)
        assert admin.id is not None
        target_id = _validate_target(telegram_user_id)
        if target_id == admin.telegram_user_id:
            raise PermissionDeniedError("a Super Admin cannot change their own status here")
        if target_id in self._configured_ids:
            raise PermissionDeniedError("configured Super Admin status cannot be changed here")
        if isinstance(status, UserStatus):
            resolved = status
        else:
            try:
                resolved = UserStatus(status)
            except (ValueError, TypeError):
                raise ValueError(f"unknown user status: {status!r}") from None
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("a reason is required for status changes")
        clean_reason = reason.strip()
        if len(clean_reason) > MAX_REASON_LENGTH:
            raise ValueError("reason is too long")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("an idempotency key is required for status changes")
        key = idempotency_key.strip()
        if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
            raise ValueError("idempotency key is too long")
        target = await self._users.get_by_telegram_user_id(target_id)
        if target is None or target.id is None:
            raise UserNotFound(f"Telegram user {target_id} not found")
        return await self._users.change_status_idempotently(
            target.id,
            resolved,
            actor_id=admin.id,
            idempotency_key=key,
            reason=clean_reason,
            admin_configured_ids=self._configured_ids,
        )


__all__ = [
    "AdminServerSummary",
    "AdminUserProfile",
    "AdminUserRepository",
    "AdminUserSearchPage",
    "AdminUserService",
]
