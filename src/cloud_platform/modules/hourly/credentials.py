"""One-time, owner-scoped delivery of passwords returned by server creation.

Only Fernet ciphertext is persisted. No operation, notification, audit event or
server snapshot ever receives a password. The lease prevents concurrent owner
reveals; ciphertext is deleted only after delivery is acknowledged.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import exists, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import Exists

from cloud_platform.core.secrets import SecretBox, SecretEnvelope
from cloud_platform.db.base import Server, ServerCreateCredential


class ClaimedCreatePassword:
    """Redacted one-shot value held only during a customer delivery attempt."""

    __slots__ = ("_value", "claim_id", "username")

    def __init__(self, claim_id: UUID, password: str, username: str | None) -> None:
        self.claim_id = claim_id
        self.username = username
        self._value: str | None = password

    def reveal(self) -> str | None:
        value, self._value = self._value, None
        return value

    def __repr__(self) -> str:
        return f"ClaimedCreatePassword(claim_id={self.claim_id}, password=<redacted>)"

    __str__ = __repr__


class SqlAlchemyServerCredentialStore:
    """PostgreSQL-backed encrypted create credential with owner-scoped leases."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        *,
        box: SecretBox,
    ) -> None:
        self._session_factory = session_factory
        self._box = box

    @staticmethod
    def _owner_matches(server_id: UUID, user_id: UUID) -> Exists:
        return exists(
            select(Server.id).where(
                Server.id == server_id,
                Server.user_id == user_id,
                Server.deleted_at.is_(None),
                Server.provider_server_id == ServerCreateCredential.provider_server_id,
            )
        )

    async def save_issued(
        self,
        *,
        server_id: UUID,
        provider_server_id: str,
        password: str,
        username: str | None = None,
    ) -> None:
        """Seal the provider's one-time value before any DB I/O.

        Never overwrite an existing secret: a replay cannot recreate or replace
        a credential that an owner has already received.
        """
        if not password or not provider_server_id:
            raise ValueError("a provider-issued password and server identity are required")
        envelope = self._box.encrypt(password)
        async with self._session_factory() as session:
            inserted = (
                await session.execute(
                    insert(ServerCreateCredential)
                    .values(
                        server_id=server_id,
                        provider_server_id=provider_server_id,
                        ciphertext=envelope.token,
                        username=username,
                        key_id=envelope.key_id,
                        algorithm=envelope.algorithm,
                        created_at=datetime.now(UTC),
                    )
                    .on_conflict_do_nothing(index_elements=["server_id"])
                    .returning(ServerCreateCredential.server_id)
                )
            ).scalar_one_or_none()
            if inserted is None:
                # A replay cannot replace a prior secret or consumed tombstone.
                raise RuntimeError("server create credential already exists")
            await session.commit()

    async def has_for_owner(self, *, server_id: UUID, user_id: UUID) -> bool:
        """Display reveal only when an owner can claim an unclaimed credential."""
        async with self._session_factory() as session:
            found = await session.scalar(
                select(ServerCreateCredential.server_id).where(
                    ServerCreateCredential.server_id == server_id,
                    ServerCreateCredential.ciphertext.is_not(None),
                    ServerCreateCredential.key_id == self._box.key_id,
                    self._owner_matches(server_id, user_id),
                    self._claim_available(datetime.now(UTC)),
                )
            )
            return found is not None

    @staticmethod
    def _claim_available(now: datetime) -> ColumnElement[bool]:
        return or_(
            ServerCreateCredential.claim_id.is_(None),
            ServerCreateCredential.claim_expires_at <= now,
        )

    async def claim_for_owner(
        self, *, server_id: UUID, user_id: UUID
    ) -> ClaimedCreatePassword | None:
        """Claim for one delivery attempt; expiry recovers process crashes."""
        now = datetime.now(UTC)
        claim_id = uuid4()
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    update(ServerCreateCredential)
                    .where(
                        ServerCreateCredential.server_id == server_id,
                        ServerCreateCredential.key_id == self._box.key_id,
                        ServerCreateCredential.ciphertext.is_not(None),
                        self._owner_matches(server_id, user_id),
                        self._claim_available(now),
                    )
                    .values(
                        claim_id=claim_id,
                        claim_expires_at=now + timedelta(minutes=5),
                    )
                    .returning(
                        ServerCreateCredential.ciphertext,
                        ServerCreateCredential.username,
                        ServerCreateCredential.key_id,
                        ServerCreateCredential.algorithm,
                    )
                )
            ).one_or_none()
            if row is None:
                return None
            password = self._box.decrypt(
                SecretEnvelope(token=row.ciphertext, key_id=row.key_id, algorithm=row.algorithm)
            )
            await session.commit()
            return ClaimedCreatePassword(claim_id, password, row.username)

    async def ack_for_owner(self, *, server_id: UUID, user_id: UUID, claim_id: UUID) -> bool:
        """Erase ciphertext after delivery; retain tombstone to forbid reissue."""
        async with self._session_factory() as session:
            erased = (
                await session.execute(
                    update(ServerCreateCredential)
                    .where(
                        ServerCreateCredential.server_id == server_id,
                        ServerCreateCredential.claim_id == claim_id,
                        ServerCreateCredential.ciphertext.is_not(None),
                        self._owner_matches(server_id, user_id),
                    )
                    .values(ciphertext=None, claim_id=None, claim_expires_at=None)
                    .returning(ServerCreateCredential.server_id)
                )
            ).scalar_one_or_none()
            if erased is None:
                return False
            await session.commit()
            return True

    async def release_for_owner(self, *, server_id: UUID, user_id: UUID, claim_id: UUID) -> bool:
        """Allow a retry after an explicit Telegram delivery failure."""
        async with self._session_factory() as session:
            released = (
                await session.execute(
                    update(ServerCreateCredential)
                    .where(
                        ServerCreateCredential.server_id == server_id,
                        ServerCreateCredential.claim_id == claim_id,
                        self._owner_matches(server_id, user_id),
                    )
                    .values(claim_id=None, claim_expires_at=None)
                    .returning(ServerCreateCredential.server_id)
                )
            ).scalar_one_or_none()
            if released is None:
                return False
            await session.commit()
            return True
