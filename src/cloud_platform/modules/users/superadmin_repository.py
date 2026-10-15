"""Transactional, configuration-owned Telegram Super Admin assignments.

The assignment row is the durable desired policy, including before registration.
Only trusted bot onboarding may bind an authenticated Telegram identity. Public
user creation must never call the grant helper with client-supplied identities.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager
from hashlib import sha256
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import AuditEvent, TelegramSuperadminAssignment, User
from cloud_platform.db.timestamps import to_db_utc, utc_now
from cloud_platform.modules.audit.domain import ActorType
from cloud_platform.modules.users.domain import PermissionDeniedError, Role, UserStatus
from cloud_platform.modules.users.superadmins import require_superadmin, validate_telegram_user_ids

# PostgreSQL accepts a *signed* BIGINT. A stable namespace serializes startup
# reconciliation and onboarding, including identities without a users row yet.
SUPERADMIN_ADVISORY_LOCK_KEY = int.from_bytes(
    sha256(b"cloud-platform:telegram-superadmin-policy:v1").digest()[:8],
    byteorder="big",
    signed=True,
)


async def lock_superadmin_reconciliation(session: AsyncSession) -> None:
    """Acquire the policy transaction lock before any user/assignment row lock."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": SUPERADMIN_ADVISORY_LOCK_KEY},
    )


async def authorize_superadmin_transaction(
    session: AsyncSession, actor_id: UUID, configured_ids: Iterable[int]
) -> None:
    """Authorize fresh persisted RBAC while excluding concurrent revocation.

    Call within the privileged mutation's transaction, before wallet/gateway
    row locks. The policy lock remains held until that same transaction ends.
    Thus commands linearize wholly before a removal, or see its revoked role.
    """
    from cloud_platform.modules.users.repository import _to_domain

    configured = validate_telegram_user_ids(configured_ids)
    await lock_superadmin_reconciliation(session)
    user = (
        await session.execute(
            select(User)
            .where(User.id == actor_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if user is None:
        raise PermissionDeniedError("configured active Telegram Super Admin required")
    require_superadmin(_to_domain(user), configured)
    assignment = (
        await session.execute(
            select(TelegramSuperadminAssignment)
            .where(
                TelegramSuperadminAssignment.telegram_user_id == cast(Any, user).telegram_user_id
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if (
        assignment is None
        or not cast(Any, assignment).enabled
        or cast(Any, assignment).user_id != actor_id
    ):
        raise PermissionDeniedError("enabled bound Telegram Super Admin assignment required")


def _audit(
    session: AsyncSession,
    action: str,
    telegram_user_id: int,
    *,
    user: User | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditEvent(
            actor_type=ActorType.SYSTEM.value,
            actor_id=None,
            action=f"telegram.superadmin.{action}",
            resource_type="telegram_superadmin_assignment",
            resource_id=str(telegram_user_id),
            reason="Reconcile configuration-owned Telegram Super Admin policy",
            event_metadata={
                "telegram_user_id": telegram_user_id,
                "user_id": str(cast(Any, user).id) if user is not None else None,
                **(metadata or {}),
            },
        )
    )


async def apply_configured_superadmin(
    session: AsyncSession, user: User, *, onboarding: bool = False
) -> bool:
    """Grant a persisted policy to an authenticated bot user's existing row.

    Caller owns the transaction and must acquire the shared lock *before*
    looking up/creating the user. This helper also takes the reentrant lock as a
    defensive boundary. It never reconciles a process's potentially stale config.
    Missing/revoked assignments are deliberately not grants. No wallet is read
    or written. Returns whether privileges or the assignment binding changed.
    Onboarding only binds previously pending assignments; it never undoes a
    later account freeze/role revocation on an ordinary message or callback.
    """
    telegram_user_id = cast(Any, user).telegram_user_id
    if telegram_user_id is None:
        return False
    validate_telegram_user_ids([telegram_user_id])
    if cast(Any, user).id is None:
        raise ValueError("Super Admin binding requires a persisted user identity")
    await lock_superadmin_reconciliation(session)
    assignment = (
        await session.execute(
            select(TelegramSuperadminAssignment)
            .where(TelegramSuperadminAssignment.telegram_user_id == telegram_user_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if assignment is None or not cast(Any, assignment).enabled:
        return False
    bound_user_id = cast(Any, assignment).user_id
    if bound_user_id is not None and bound_user_id != cast(Any, user).id:
        raise ValueError("Telegram Super Admin assignment is bound to a different user")
    if onboarding and bound_user_id == cast(Any, user).id:
        return False
    old_role, old_status = cast(Any, user).role, cast(Any, user).status
    binding_changed = bound_user_id != cast(Any, user).id
    privilege_changed = old_role != Role.ADMIN.value or old_status != UserStatus.ACTIVE.value
    if not binding_changed and not privilege_changed:
        return False
    now = to_db_utc(utc_now())
    cast(Any, assignment).user_id = cast(Any, user).id
    cast(Any, assignment).updated_at = now
    if privilege_changed:
        cast(Any, user).role = Role.ADMIN.value
        cast(Any, user).status = UserStatus.ACTIVE.value
        cast(Any, user).updated_at = now
    _audit(
        session,
        "granted",
        telegram_user_id,
        user=user,
        metadata={
            "previous_role": old_role,
            "previous_status": old_status,
            "role": Role.ADMIN.value,
            "status": UserStatus.ACTIVE.value,
            "binding_changed": binding_changed,
        },
    )
    return True


class SqlAlchemySuperadminRepository:
    """Idempotent startup policy synchronization, atomically audited."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def reconcile(self, configured_ids: Iterable[int]) -> None:
        desired = set(validate_telegram_user_ids(configured_ids))
        async with self._session_factory() as session, session.begin():
            await lock_superadmin_reconciliation(session)
            assignments = (
                (await session.execute(select(TelegramSuperadminAssignment).with_for_update()))
                .scalars()
                .all()
            )
            existing = {cast(Any, row).telegram_user_id: row for row in assignments}
            for telegram_id in sorted(desired):
                assignment = existing.get(telegram_id)
                if assignment is None:
                    assignment = TelegramSuperadminAssignment(
                        telegram_user_id=telegram_id, enabled=True
                    )
                    session.add(assignment)
                    _audit(session, "configured", telegram_id)
                elif not cast(Any, assignment).enabled:
                    cast(Any, assignment).enabled = True
                    cast(Any, assignment).updated_at = to_db_utc(utc_now())
                    _audit(session, "configured", telegram_id)
                # Flush before the helper queries the assignment, including
                # session factories that disable SQLAlchemy's autoflush.
                await session.flush()
                user = (
                    await session.execute(
                        select(User).where(User.telegram_user_id == telegram_id).with_for_update()
                    )
                ).scalar_one_or_none()
                if user is not None:
                    await apply_configured_superadmin(session, user)
            for telegram_id in sorted(existing.keys() - desired):
                assignment = existing[telegram_id]
                if not cast(Any, assignment).enabled:
                    continue
                cast(Any, assignment).enabled = False
                cast(Any, assignment).updated_at = to_db_utc(utc_now())
                _audit(session, "removed", telegram_id)
                user_id = cast(Any, assignment).user_id
                if user_id is None:
                    continue
                user = (
                    await session.execute(select(User).where(User.id == user_id).with_for_update())
                ).scalar_one_or_none()
                if user is not None and cast(Any, user).role == Role.ADMIN.value:
                    cast(Any, user).role = Role.USER.value
                    cast(Any, user).updated_at = to_db_utc(utc_now())
                    _audit(
                        session,
                        "revoked",
                        telegram_id,
                        user=user,
                        metadata={"previous_role": Role.ADMIN.value, "role": Role.USER.value},
                    )
            # A rejected audit insert/commit rolls back every assignment and
            # user mutation, rather than leaving an unaudited privilege grant.
            await session.flush()
