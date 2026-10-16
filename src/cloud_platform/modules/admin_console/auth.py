"""Shared fresh authorization for administrator application facades."""

from cloud_platform.modules.users.domain import PermissionDeniedError, User, UserRepository
from cloud_platform.modules.users.superadmins import require_superadmin


async def authorize(users: UserRepository, actor: User, configured_ids: tuple[int, ...]) -> User:
    admin = require_superadmin(actor, configured_ids)
    if admin.id is None:
        raise PermissionDeniedError("persisted administrator required")
    fresh = require_superadmin(await users.get(admin.id), configured_ids)
    if fresh.id != admin.id or fresh.telegram_user_id != admin.telegram_user_id:
        raise PermissionDeniedError("administrator identity changed")
    return fresh
