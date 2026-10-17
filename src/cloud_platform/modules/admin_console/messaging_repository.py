"""Atomic campaign/audit enqueue and fenced PostgreSQL delivery leases."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import AdminMessageCampaign, AdminMessageDelivery, AuditEvent, User
from cloud_platform.modules.admin_console.messaging import MessageDelivery, MessageRecipient
from cloud_platform.modules.users.domain import PermissionDeniedError, UserStatus
from cloud_platform.modules.users.superadmin_repository import (
    authorize_superadmin_transaction,
    lock_superadmin_reconciliation,
)
from cloud_platform.modules.users.superadmins import validate_telegram_user_ids


class SqlAlchemyAdminMessagingRepository:
    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        *,
        configured_ids: tuple[int, ...] = (),
    ):
        self._sessions = session_factory
        self._ids = validate_telegram_user_ids(configured_ids)

    async def create_campaign(
        self,
        *,
        actor_id: UUID,
        recipients: tuple[MessageRecipient, ...],
        body: str,
        reason: str,
        idempotency_key: str,
        configured_ids: tuple[int, ...],
    ) -> UUID:
        if (
            not 1 <= len(recipients) <= 100
            or len({row.user_id for row in recipients}) != len(recipients)
            or len({row.telegram_user_id for row in recipients}) != len(recipients)
        ):
            raise ValueError("campaign requires 1 to 100 unique recipients")
        if (
            not 1 <= len(body.strip()) <= 3000
            or not 1 <= len(reason.strip()) <= 500
            or not 1 <= len(idempotency_key) <= 128
        ):
            raise ValueError("campaign content is invalid")
        facts = {
            "body": body,
            "reason": reason,
            "recipients": sorted((str(row.user_id), row.telegram_user_id) for row in recipients),
        }
        facts_hash = sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()
        identity = uuid5(
            NAMESPACE_URL, f"cloud-platform/admin-message/{actor_id}/{idempotency_key}"
        )
        advisory = int.from_bytes(sha256(identity.bytes).digest()[:8], "big", signed=True)
        async with self._sessions() as session, session.begin():
            await authorize_superadmin_transaction(session, actor_id, configured_ids)
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": advisory})
            existing = (
                await session.execute(
                    select(AdminMessageCampaign).where(
                        AdminMessageCampaign.actor_id == actor_id,
                        AdminMessageCampaign.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing.facts_hash != facts_hash:
                    raise ValueError("campaign idempotency facts differ")
                return cast(UUID, existing.id)
            targets = (
                (
                    await session.execute(
                        select(User)
                        .where(User.id.in_([row.user_id for row in recipients]))
                        .order_by(User.id)
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            expected = {row.user_id: row.telegram_user_id for row in recipients}
            if len(targets) != len(expected) or any(
                row.telegram_user_id != expected[cast(UUID, row.id)]
                or row.status != UserStatus.ACTIVE.value
                for row in targets
            ):
                raise PermissionDeniedError("frozen recipient identity or status changed")
            session.add(
                AdminMessageCampaign(
                    id=identity,
                    actor_id=actor_id,
                    body=body,
                    reason=reason,
                    idempotency_key=idempotency_key,
                    recipient_ids=sorted(str(row.user_id) for row in recipients),
                    facts_hash=facts_hash,
                )
            )
            for recipient in recipients:
                session.add(
                    AdminMessageDelivery(
                        id=uuid4(),
                        campaign_id=identity,
                        user_id=recipient.user_id,
                        telegram_user_id=recipient.telegram_user_id,
                    )
                )
            session.add(
                AuditEvent(
                    actor_type="admin",
                    actor_id=actor_id,
                    action="admin.message.campaign_queued",
                    resource_type="admin_message_campaign",
                    resource_id=str(identity),
                    reason=reason,
                    event_metadata={
                        "recipient_count": len(recipients),
                        "facts_hash": facts_hash,
                        "idempotency_key": idempotency_key,
                    },
                )
            )
            await session.flush()
        return identity

    async def claim_deliveries(
        self, *, limit: int = 20, lease_seconds: int = 60
    ) -> list[MessageDelivery]:
        if type(limit) is not int or not 1 <= limit <= 20 or not 30 <= lease_seconds <= 300:
            raise ValueError("delivery batch or lease is out of bounds")
        now = datetime.now(UTC)
        result = []
        async with self._sessions() as session, session.begin():
            await lock_superadmin_reconciliation(session)
            await session.execute(
                update(AdminMessageDelivery)
                .where(
                    AdminMessageDelivery.status == "leased", AdminMessageDelivery.lease_until <= now
                )
                .values(
                    status="ambiguous",
                    error_code="expired_send_lease",
                    lease_until=None,
                    claim_token=None,
                )
            )
            rows = (
                await session.execute(
                    select(
                        AdminMessageDelivery,
                        AdminMessageCampaign.body,
                        AdminMessageCampaign.actor_id,
                    )
                    .join(
                        AdminMessageCampaign,
                        AdminMessageDelivery.campaign_id == AdminMessageCampaign.id,
                    )
                    .where(
                        AdminMessageDelivery.status == "pending",
                        AdminMessageDelivery.next_attempt_at <= now,
                    )
                    .order_by(AdminMessageDelivery.next_attempt_at, AdminMessageDelivery.id)
                    .limit(limit)
                    .with_for_update(of=AdminMessageDelivery, skip_locked=True)
                )
            ).all()
            for row, body, actor_id in rows:
                try:
                    await authorize_superadmin_transaction(session, actor_id, self._ids)
                except PermissionDeniedError:
                    row.status, row.error_code = "failed", "actor_revoked"
                    continue
                target = (
                    await session.execute(
                        select(User).where(User.id == row.user_id).with_for_update()
                    )
                ).scalar_one_or_none()
                if (
                    target is None
                    or target.status != UserStatus.ACTIVE.value
                    or target.telegram_user_id != row.telegram_user_id
                ):
                    row.status, row.error_code = "failed", "recipient_unavailable"
                    continue
                row.status, row.claim_token = "leased", uuid4()
                row.attempts += 1
                row.lease_until = now + timedelta(seconds=lease_seconds)
                result.append(
                    MessageDelivery(
                        row.id,
                        row.campaign_id,
                        row.telegram_user_id,
                        body,
                        row.claim_token,
                        row.attempts,
                    )
                )
        return result

    async def may_deliver(self, delivery: MessageDelivery) -> bool:
        """Fence each send after queued work, fresh role/target and lease checks."""
        now = datetime.now(UTC)
        async with self._sessions() as session, session.begin():
            await lock_superadmin_reconciliation(session)
            result = (
                await session.execute(
                    select(AdminMessageDelivery, AdminMessageCampaign.actor_id)
                    .join(
                        AdminMessageCampaign,
                        AdminMessageDelivery.campaign_id == AdminMessageCampaign.id,
                    )
                    .where(AdminMessageDelivery.id == delivery.id)
                    .with_for_update(of=AdminMessageDelivery)
                )
            ).one_or_none()
            if result is None:
                return False
            row, actor_id = result
            if row.status != "leased" or row.claim_token != delivery.claim_token:
                return False
            if row.lease_until is None or row.lease_until <= now:
                row.status, row.error_code = "ambiguous", "expired_send_lease"
                return False
            try:
                await authorize_superadmin_transaction(session, actor_id, self._ids)
            except PermissionDeniedError:
                row.status, row.error_code = "failed", "actor_revoked"
                return False
            target = (
                await session.execute(select(User).where(User.id == row.user_id).with_for_update())
            ).scalar_one_or_none()
            if (
                target is None
                or target.status != UserStatus.ACTIVE.value
                or target.telegram_user_id != row.telegram_user_id
            ):
                row.status, row.error_code = "failed", "recipient_unavailable"
                return False
            return True

    async def finish(
        self,
        delivery: MessageDelivery,
        status: str,
        *,
        error_code: str | None = None,
        retry_seconds: int = 0,
    ) -> bool:
        if status not in {"pending", "sent", "failed", "ambiguous"} or error_code not in {
            None,
            "transport_rejected",
            "send_outcome_unknown",
        }:
            raise ValueError("invalid delivery outcome")
        now = datetime.now(UTC)
        async with self._sessions() as session, session.begin():
            matched = await session.scalar(
                update(AdminMessageDelivery)
                .where(
                    AdminMessageDelivery.id == delivery.id,
                    AdminMessageDelivery.status == "leased",
                    AdminMessageDelivery.claim_token == delivery.claim_token,
                )
                .returning(AdminMessageDelivery.id)
                .values(
                    status=status,
                    error_code=error_code,
                    lease_until=None,
                    claim_token=None,
                    sent_at=now if status == "sent" else None,
                    next_attempt_at=now + timedelta(seconds=min(3600, max(0, retry_seconds))),
                )
            )
            return matched is not None
