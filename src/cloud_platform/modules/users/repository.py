from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from hashlib import sha256
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import AuditEvent as SQLAlchemyAuditEvent
from cloud_platform.db.base import TermsVersion as SQLAlchemyTermsVersion
from cloud_platform.db.base import User as SQLAlchemyUser
from cloud_platform.db.base import Wallet as SQLAlchemyWallet
from cloud_platform.db.timestamps import (
    from_db_utc,
    from_db_utc_or_none,
    to_db_utc,
    to_db_utc_or_none,
    utc_now,
)
from cloud_platform.modules.audit.domain import ActorType
from cloud_platform.modules.users.domain import (
    Role,
    TermsVersion,
    User,
    UserNotFound,
    UserStatus,
)


def _attr(row: Any, name: str) -> Any:
    """Read a legacy-style Column attribute; typed as Any at the boundary."""
    return getattr(row, name)


def _to_domain(row: SQLAlchemyUser) -> User:
    """Map a SQLAlchemy ORM row to a domain User aggregate."""
    return User(
        id=_attr(row, "id"),
        username=_attr(row, "username"),
        email=_attr(row, "email"),
        status=UserStatus(_attr(row, "status")),
        role=Role(_attr(row, "role")),
        terms_version=_attr(row, "terms_version"),
        # users.terms_accepted_at/created_at/updated_at are legacy naive-UTC
        # columns: rehydrate them as aware UTC for the domain.
        terms_accepted_at=from_db_utc_or_none(_attr(row, "terms_accepted_at")),
        telegram_user_id=_attr(row, "telegram_user_id"),
        phone_number=_attr(row, "phone_number"),
        phone_verified_at=from_db_utc_or_none(_attr(row, "phone_verified_at")),
        national_id=_attr(row, "national_id"),
        created_at=from_db_utc_or_none(_attr(row, "created_at")),
        updated_at=from_db_utc_or_none(_attr(row, "updated_at")),
    )


def _to_row(domain: User) -> SQLAlchemyUser:
    """Map a domain User aggregate to a SQLAlchemy ORM row."""
    if domain.id is None:
        row = SQLAlchemyUser(
            username=domain.username,
            email=domain.email,
            status=domain.status.value,
            role=domain.role.value,
            telegram_user_id=domain.telegram_user_id,
            phone_number=domain.phone_number,
            phone_verified_at=domain.phone_verified_at,
            national_id=domain.national_id,
        )
    else:
        row = SQLAlchemyUser(
            id=domain.id,
            username=domain.username,
            email=domain.email,
            status=domain.status.value,
            role=domain.role.value,
            terms_version=domain.terms_version,
            terms_accepted_at=to_db_utc_or_none(domain.terms_accepted_at),
            telegram_user_id=domain.telegram_user_id,
            phone_number=domain.phone_number,
            phone_verified_at=domain.phone_verified_at,
            national_id=domain.national_id,
            created_at=to_db_utc_or_none(domain.created_at),
            updated_at=to_db_utc(utc_now()),
        )
    return row


class UserStatusIdempotencyConflict(ValueError):
    """An actor reused a status-change key with different command facts."""


class SqlAlchemyUserRepository:
    """SQLAlchemy-backed implementation of the User repository port."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def onboard_telegram(self, telegram_user_id: int, username: str, email: str) -> User:
        """Resolve a verified bot sender and its wallet in ONE transaction.

        This is a trusted bot-edge operation, not a public registration API.
        Never resolve identity by a mutable username/email or overwrite a wallet.
        The shared lock orders registration with concurrent policy revocations.
        """
        from cloud_platform.modules.users.superadmin_repository import (
            apply_configured_superadmin,
            lock_superadmin_reconciliation,
        )
        from cloud_platform.modules.users.superadmins import validate_telegram_user_ids
        from cloud_platform.modules.wallet.domain import DEFAULT_WALLET_CURRENCY

        validate_telegram_user_ids([telegram_user_id])
        async with self._session_factory() as session, session.begin():
            await lock_superadmin_reconciliation(session)
            row = (
                await session.execute(
                    select(SQLAlchemyUser)
                    .where(SQLAlchemyUser.telegram_user_id == telegram_user_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                # A reused/missing Telegram username is not an account link.
                # Preserve an unrelated existing account and use a fresh alias.
                alias = username.strip() or f"telegram_{telegram_user_id}"
                collision = (
                    await session.execute(
                        select(SQLAlchemyUser.id).where(SQLAlchemyUser.username == alias)
                    )
                ).scalar_one_or_none()
                if collision is not None:
                    from uuid import uuid4

                    alias = f"telegram_{telegram_user_id}_{uuid4().hex}"
                row = SQLAlchemyUser(
                    username=alias,
                    email=email,
                    telegram_user_id=telegram_user_id,
                    role=Role.USER.value,
                    status=UserStatus.ACTIVE.value,
                )
                session.add(row)
                await session.flush()
            await apply_configured_superadmin(session, row, onboarding=True)
            # DO NOTHING preserves wallet identity, balances, currency and all
            # immutable history, also after an interrupted historical signup.
            await session.execute(
                insert(SQLAlchemyWallet)
                .values(user_id=_attr(row, "id"), balance=0, currency=DEFAULT_WALLET_CURRENCY)
                .on_conflict_do_nothing(index_elements=[SQLAlchemyWallet.user_id])
            )
            await session.flush()
            return _to_domain(row)

    async def create(self, user: User) -> User:
        """Create a new user. Commits and returns the refreshed aggregate."""
        row = _to_row(user)
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def get(self, user_id: UUID) -> User | None:
        """Get a user by ID. Returns None when absent."""
        async with self._session_factory() as session:
            stmt = select(SQLAlchemyUser).where(SQLAlchemyUser.id == user_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return _to_domain(row) if row is not None else None

    async def get_by_username(self, username: str) -> User | None:
        """Get a user by username. Returns None when absent."""
        async with self._session_factory() as session:
            stmt = select(SQLAlchemyUser).where(SQLAlchemyUser.username == username)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return _to_domain(row) if row is not None else None

    async def get_by_email(self, email: str) -> User | None:
        """Get a user by email. Returns None when absent."""
        async with self._session_factory() as session:
            stmt = select(SQLAlchemyUser).where(SQLAlchemyUser.email == email)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return _to_domain(row) if row is not None else None

    async def search(self, query: str, *, offset: int = 0, limit: int = 20) -> list[User]:
        """Substring search over username/email (admin API, M14-003)."""
        pattern = f"%{(query or '').strip()}%"
        async with self._session_factory() as session:
            stmt = (
                select(SQLAlchemyUser)
                .where(
                    (SQLAlchemyUser.username.ilike(pattern)) | (SQLAlchemyUser.email.ilike(pattern))
                )
                .order_by(SQLAlchemyUser.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_domain(row) for row in rows]

    async def update_status(self, user_id: UUID, status: UserStatus) -> User:
        """Update a user's status. Raises UserNotFound if missing."""
        async with self._session_factory() as session:
            stmt = select(SQLAlchemyUser).where(SQLAlchemyUser.id == user_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()

            if row is None:
                raise UserNotFound(f"User with id {user_id} not found")

            cast(Any, row).status = status.value  # legacy Column attribute
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def change_status_idempotently(
        self,
        user_id: UUID,
        status: UserStatus,
        *,
        actor_id: UUID,
        idempotency_key: str,
        reason: str,
        admin_configured_ids: tuple[int, ...] | None = None,
    ) -> UserStatus:
        """Atomically persist status and its actor-scoped immutable audit receipt.

        Exact replays return the receipt's historical status without touching the
        current user. The transaction advisory lock serializes an actor/key even
        when conflicting requests name different targets; only the target user
        row is locked, never the actor (which may itself be another admin's target).
        """
        if not idempotency_key or not idempotency_key.strip():
            raise ValueError("an idempotency key is required for status changes")
        if not reason or not reason.strip():
            raise ValueError("admin mutations must carry a non-empty reason")
        action = "admin.user.status_changed"
        lock_key = int.from_bytes(
            sha256(f"{action}:{actor_id}:{idempotency_key}".encode()).digest()[:8],
            byteorder="big",
            signed=True,
        )
        async with self._session_factory() as session, session.begin():
            if admin_configured_ids is not None:
                from cloud_platform.modules.users.superadmin_repository import (
                    authorize_superadmin_transaction,
                )

                await authorize_superadmin_transaction(session, actor_id, admin_configured_ids)
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key}
            )
            target = (
                await session.execute(
                    select(SQLAlchemyUser).where(SQLAlchemyUser.id == user_id).with_for_update()
                )
            ).scalar_one_or_none()
            receipt = (
                await session.execute(
                    select(SQLAlchemyAuditEvent).where(
                        SQLAlchemyAuditEvent.actor_id == actor_id,
                        SQLAlchemyAuditEvent.action == action,
                        SQLAlchemyAuditEvent.event_metadata["idempotency_key"].astext
                        == idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if receipt is not None:
                metadata = _attr(receipt, "event_metadata")
                if (
                    _attr(receipt, "actor_type") != ActorType.ADMIN.value
                    or _attr(receipt, "resource_type") != "user"
                    or _attr(receipt, "resource_id") != str(user_id)
                    or _attr(receipt, "reason") != reason
                    or metadata.get("status") != status.value
                ):
                    raise UserStatusIdempotencyConflict(
                        "status-change idempotency key carries different facts"
                    )
                return UserStatus(metadata["status"])
            if target is None:
                raise UserNotFound(f"User with id {user_id} not found")
            if admin_configured_ids is not None and (
                user_id == actor_id
                or _attr(target, "role") == Role.ADMIN.value
                or _attr(target, "telegram_user_id") in admin_configured_ids
            ):
                from cloud_platform.modules.users.domain import PermissionDeniedError

                raise PermissionDeniedError(
                    "administrator identities cannot be blocked from the panel"
                )
            cast(Any, target).status = status.value
            from cloud_platform.modules.notifications.outbox import notice_row

            session.add(
                notice_row(
                    key=f"user-status:{actor_id}:{idempotency_key}",
                    user_id=user_id,
                    text=(
                        f"وضعیت حساب شما به {status.value} تغییر یافت؛ "
                        "برای بررسی با پشتیبانی تماس بگیرید."
                    ),
                )
            )
            session.add(
                SQLAlchemyAuditEvent(
                    actor_type=ActorType.ADMIN.value,
                    actor_id=actor_id,
                    action=action,
                    resource_type="user",
                    resource_id=str(user_id),
                    reason=reason,
                    event_metadata={"status": status.value, "idempotency_key": idempotency_key},
                )
            )
            # Flush both writes before returning; __aexit__ commits or rolls them
            # back together, including when an audit constraint/trigger rejects it.
            await session.flush()
            return status

    async def update_terms(self, user_id: UUID, terms_version: int) -> User:
        """Persist the user's accepted terms version. Raises UserNotFound if missing."""
        async with self._session_factory() as session:
            stmt = select(SQLAlchemyUser).where(SQLAlchemyUser.id == user_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()

            if row is None:
                raise UserNotFound(f"User with id {user_id} not found")

            cast(Any, row).terms_version = terms_version
            cast(Any, row).terms_accepted_at = to_db_utc(utc_now())
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def get_by_telegram_user_id(self, telegram_user_id: int) -> User | None:
        """Get a user by their Telegram user id. Returns None when absent."""
        async with self._session_factory() as session:
            stmt = select(SQLAlchemyUser).where(SQLAlchemyUser.telegram_user_id == telegram_user_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return _to_domain(row) if row is not None else None

    async def update_telegram_user_id(self, user_id: UUID, telegram_user_id: int) -> User:
        """Link (or re-link) a user's Telegram identity.

        Used by onboarding when the Telegram id was not recorded at creation
        time (rows created before the column existed).
        """
        async with self._session_factory() as session:
            stmt = select(SQLAlchemyUser).where(SQLAlchemyUser.id == user_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                raise UserNotFound(f"User with id {user_id} not found")
            cast(Any, row).telegram_user_id = telegram_user_id
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def update_verified_phone(
        self, user_id: UUID, phone_number: str, verified_at: datetime
    ) -> User:
        """Persist verified contact facts without exposing them in logs."""
        async with self._session_factory() as session:
            row = await session.get(SQLAlchemyUser, user_id)
            if row is None:
                raise UserNotFound(f"User with id {user_id} not found")
            cast(Any, row).phone_number = phone_number
            cast(Any, row).phone_verified_at = verified_at
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def update_national_id(self, user_id: UUID, national_id: str) -> User:
        """Store a validated national ID without claiming ownership matching."""
        async with self._session_factory() as session:
            row = await session.get(SQLAlchemyUser, user_id)
            if row is None:
                raise UserNotFound(f"User with id {user_id} not found")
            cast(Any, row).national_id = national_id
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)


def _terms_to_domain(row: SQLAlchemyTermsVersion) -> TermsVersion:
    return TermsVersion(
        version=int(_attr(row, "version")),
        body=str(_attr(row, "body")),
        # terms_versions.effective_at is a legacy naive-UTC column.
        effective_at=from_db_utc(_attr(row, "effective_at")),
        summary=str(_attr(row, "summary") or ""),
    )


class SqlAlchemyTermsVersionRepository:
    """SQLAlchemy implementation of the terms version port (immutable)."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def get(self, version: int) -> TermsVersion | None:
        async with self._session_factory() as session:
            row = await session.get(SQLAlchemyTermsVersion, version)
            return _terms_to_domain(row) if row is not None else None

    async def get_latest(self) -> TermsVersion | None:
        async with self._session_factory() as session:
            stmt = select(SQLAlchemyTermsVersion).order_by(SQLAlchemyTermsVersion.version.desc())
            result = await session.execute(stmt)
            row = result.scalars().first()
            return _terms_to_domain(row) if row is not None else None

    async def list_all(self) -> list[TermsVersion]:
        async with self._session_factory() as session:
            stmt = select(SQLAlchemyTermsVersion).order_by(SQLAlchemyTermsVersion.version.asc())
            result = await session.execute(stmt)
            return [_terms_to_domain(row) for row in result.scalars().all()]

    async def publish(self, terms: TermsVersion) -> TermsVersion:
        async with self._session_factory() as session:
            existing = await session.get(SQLAlchemyTermsVersion, terms.version)
            if existing is not None:
                raise ValueError(f"terms version {terms.version} already published")
            row = SQLAlchemyTermsVersion(
                version=terms.version,
                body=terms.body,
                effective_at=to_db_utc(terms.effective_at),
                summary=terms.summary,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _terms_to_domain(row)
