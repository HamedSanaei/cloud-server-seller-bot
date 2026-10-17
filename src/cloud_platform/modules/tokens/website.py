"""Single-use account pairing codes. Only a verified private Telegram user can issue one."""

from __future__ import annotations

import secrets
from hashlib import sha256
from uuid import UUID

from cloud_platform.core.config import get_settings
from cloud_platform.core.session_store import NS_REFERENCE
from cloud_platform.modules.tokens.domain import TokenScope
from cloud_platform.modules.users.domain import User, UserStatus
from cloud_platform.modules.users.superadmins import is_superadmin


async def issue_pairing(container: object, user: User) -> str:
    if user.id is None or user.status is not UserStatus.ACTIVE:
        raise ValueError("active persisted user required")
    code = "cpl_" + secrets.token_urlsafe(32)
    await container.session_store().put(  # type: ignore[attr-defined]
        NS_REFERENCE,
        "website:" + sha256(code.encode()).hexdigest(),
        {"user_id": str(user.id)},
        ttl_seconds=300,
    )
    return code


async def exchange_pairing(container: object, code: str, website_user_id: UUID) -> tuple[User, str]:
    record = await container.session_store().take(  # type: ignore[attr-defined]
        NS_REFERENCE, "website:" + sha256(code.encode()).hexdigest()
    )
    if not record:
        raise ValueError("pairing code expired or already used")
    user = await container.user_repository().get(UUID(record["user_id"]))  # type: ignore[attr-defined]
    if user is None or user.status is not UserStatus.ACTIVE:
        raise ValueError("active account required")
    scopes = frozenset(
        {
            TokenScope.CATALOG_READ,
            TokenScope.WALLET_READ,
            TokenScope.SERVERS_READ,
            TokenScope.SERVERS_WRITE,
        }
    )
    if is_superadmin(user, get_settings().telegram_superadmin_user_ids):
        scopes |= {TokenScope.ADMIN_READ, TokenScope.ADMIN_WRITE}
    # A fresh suffix allows replacing a revoked credential. Privileged endpoints also
    # reauthorize configured identity and persisted RBAC on EVERY request.
    _, token = await container.token_service().create(  # type: ignore[attr-defined]
        actor_user_id=user.id,
        owner_user_id=user.id,
        name=f"website-{str(website_user_id)[:8]}-{secrets.token_hex(4)}",
        scopes=scopes,
    )
    return user, token
