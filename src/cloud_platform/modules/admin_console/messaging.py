"""Confirmed frozen recipient campaigns and bounded durable delivery."""

from __future__ import annotations

import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from cloud_platform.core.session_store import BotSessionStore
from cloud_platform.modules.admin_console.auth import authorize
from cloud_platform.modules.users.domain import User, UserRepository, UserStatus
from cloud_platform.modules.users.superadmins import validate_telegram_user_ids


@dataclass(frozen=True, slots=True)
class MessageRecipient:
    user_id: UUID
    telegram_user_id: int


@dataclass(frozen=True, slots=True)
class MessageDelivery:
    id: UUID
    campaign_id: UUID
    telegram_user_id: int
    body: str
    claim_token: UUID
    attempts: int


class AdminMessagingRepository(Protocol):
    async def may_deliver(self, delivery: MessageDelivery) -> bool: ...
    async def create_campaign(
        self,
        *,
        actor_id: UUID,
        recipients: tuple[MessageRecipient, ...],
        body: str,
        reason: str,
        idempotency_key: str,
        configured_ids: tuple[int, ...],
    ) -> UUID: ...
    async def claim_deliveries(
        self, *, limit: int = 20, lease_seconds: int = 60
    ) -> list[MessageDelivery]: ...
    async def finish(
        self,
        delivery: MessageDelivery,
        status: str,
        *,
        error_code: str | None = None,
        retry_seconds: int = 0,
    ) -> bool: ...


class AdminMessagingService:
    def __init__(
        self,
        users: UserRepository,
        repository: AdminMessagingRepository,
        store: BotSessionStore,
        *,
        configured_ids: Iterable[int] = (),
        ttl_seconds: int = 900,
    ):
        self._users, self._repo, self._store = users, repository, store
        self._ids, self._ttl = validate_telegram_user_ids(tuple(configured_ids)), ttl_seconds

    async def clear(self, actor: User) -> None:
        admin = await authorize(self._users, actor, self._ids)
        await self._store.delete("admin-message-confirm", str(admin.id))

    async def prepare(
        self, actor: User, telegram_ids: tuple[int, ...], body: str, reason: str
    ) -> str:
        admin = await authorize(self._users, actor, self._ids)
        if not 1 <= len(telegram_ids) <= 100 or len(set(telegram_ids)) != len(telegram_ids):
            raise ValueError("select 1 to 100 unique recipients")
        tids = validate_telegram_user_ids(telegram_ids)
        if (
            not isinstance(body, str)
            or not 1 <= len(body.strip()) <= 3000
            or not isinstance(reason, str)
            or not 1 <= len(reason.strip()) <= 500
        ):
            raise ValueError("message and reason must be bounded and non-empty")
        recipients = []
        for tid in tids:
            user = await self._users.get_by_telegram_user_id(tid)
            if user is None or user.id is None or user.status is not UserStatus.ACTIVE:
                raise ValueError("recipient is unavailable")
            recipients.append({"user_id": str(user.id), "telegram_user_id": tid})
        nonce = secrets.token_urlsafe(6)
        await self._store.put(
            "admin-message-confirm",
            str(admin.id),
            {
                "nonce": nonce,
                "recipients": recipients,
                "body": body.strip(),
                "reason": reason.strip(),
            },
            ttl_seconds=self._ttl,
        )
        return nonce

    async def apply(self, actor: User, nonce: str) -> UUID:
        admin = await authorize(self._users, actor, self._ids)
        record = await self._store.get("admin-message-confirm", str(admin.id))
        if record is None or record.get("nonce") != nonce:
            raise ValueError("message confirmation expired")
        return await self._repo.create_campaign(
            actor_id=admin.id,
            recipients=tuple(
                MessageRecipient(UUID(row["user_id"]), row["telegram_user_id"])
                for row in record["recipients"]
            ),
            body=record["body"],
            reason=record["reason"],
            idempotency_key=f"admin-message:{nonce}",
            configured_ids=self._ids,
        )


class MessageRejected(Exception):
    """The transport proves no delivery happened; optional bounded retry."""

    def __init__(self, *, retry_after: int | None = None):
        self.retry_after = retry_after


class MessageSender(Protocol):
    async def send(self, telegram_user_id: int, body: str) -> None: ...


class AdminMessagingWorker:
    """Never automatically retry ambiguous sends or expired send leases."""

    def __init__(
        self, repository: AdminMessagingRepository, sender: MessageSender, *, batch_size: int = 20
    ):
        if type(batch_size) is not int or not 1 <= batch_size <= 20:
            raise ValueError("batch size must be between 1 and 20")
        self._repo, self._sender, self._batch = repository, sender, batch_size

    async def process_pending(self) -> int:
        deliveries = await self._repo.claim_deliveries(limit=self._batch, lease_seconds=60)
        for delivery in deliveries:
            if not await self._repo.may_deliver(delivery):
                continue
            try:
                await self._sender.send(delivery.telegram_user_id, delivery.body)
            except MessageRejected as exc:
                retry = exc.retry_after is not None and delivery.attempts < 5
                await self._repo.finish(
                    delivery,
                    "pending" if retry else "failed",
                    error_code="transport_rejected",
                    retry_seconds=min(3600, max(1, exc.retry_after or 1)),
                )
            except Exception:
                # No exception text or body is logged or persisted. Telegram has
                # no idempotent send key: a lost acknowledgement needs review.
                await self._repo.finish(delivery, "ambiguous", error_code="send_outcome_unknown")
            else:
                await self._repo.finish(delivery, "sent")
        return len(deliveries)
