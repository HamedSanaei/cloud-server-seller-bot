"""SQLAlchemy adapter for the business-log outbox (release hardening).

Claiming is atomic: ``claim_due`` first selects candidate keys, then performs
a conditional ``UPDATE ... WHERE status IN (...) AND (next_attempt_at IS NULL
OR next_attempt_at <= now)`` for each — only the transaction that wins the
row sees ``rowcount == 1``. Two workers can therefore never deliver the same
business event twice, and a stale ``SENDING`` claim (worker died between the
Telegram call and the ``SENT`` write) is reclaimable after a grace period.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import BusinessLogEvent as _Model
from cloud_platform.modules.businesslog.domain import (
    STATUS_ABANDONED,
    STATUS_PENDING,
    STATUS_RETRY,
    STATUS_SENDING,
    STATUS_SENT,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BusinessLogRecord:
    """One durable business-log row (claimed for delivery)."""

    id: UUID
    event_key: str
    event_type: str
    payload: dict[str, Any]
    status: str
    created_at: datetime | None
    sent_at: datetime | None
    attempts: int
    last_error: str | None


def _to_record(row: Any) -> BusinessLogRecord:
    return BusinessLogRecord(
        id=row.id,
        event_key=str(row.event_key),
        event_type=str(row.event_type),
        payload=dict(row.payload or {}),
        status=str(row.status),
        created_at=row.created_at,
        sent_at=row.sent_at,
        attempts=int(row.attempts or 0),
        last_error=row.last_error,
    )


class SqlAlchemyBusinessLogRepository:
    """Durable business-log outbox storage."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def enqueue(
        self,
        *,
        event_key: str,
        event_type: str,
        payload: dict[str, Any],
        at: datetime | None = None,
    ) -> bool:
        """Insert the row; False when the key was already enqueued."""
        async with self._session_factory() as session:
            try:
                session.add(
                    _Model(
                        event_key=event_key,
                        event_type=event_type,
                        payload=payload,
                        status=STATUS_PENDING,
                        created_at=at or datetime.now(UTC),
                        attempts=0,
                    )
                )
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return False
            return True

    async def claim_due(
        self, *, limit: int, now: datetime, stale_after_seconds: int
    ) -> list[BusinessLogRecord]:
        """Atomically claim up to ``limit`` due (or stale) rows."""
        stale_before = now - timedelta(seconds=stale_after_seconds)
        due = _Model.next_attempt_at.is_(None) | (_Model.next_attempt_at <= now)
        claimable = (_Model.status.in_([STATUS_PENDING, STATUS_RETRY]) & due) | (
            (_Model.status == STATUS_SENDING) & (_Model.claimed_at <= stale_before)
        )
        claimed: list[BusinessLogRecord] = []
        async with self._session_factory() as session:
            candidates = (
                (
                    await session.execute(
                        select(_Model.event_key)
                        .where(claimable)
                        .order_by(_Model.created_at)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            for key in candidates:
                result = await session.execute(
                    update(_Model)
                    .where(_Model.event_key == key, claimable)
                    .values(
                        status=STATUS_SENDING,
                        claimed_at=now,
                        attempts=_Model.attempts + 1,
                    )
                )
                if getattr(result, "rowcount", 0) != 1:  # pragma: no cover - lost claim race
                    continue
                row = (
                    await session.execute(select(_Model).where(_Model.event_key == key))
                ).scalar_one()
                claimed.append(_to_record(row))
            await session.commit()
        return claimed

    async def mark_sent(self, event_key: str, *, at: datetime) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(_Model)
                .where(_Model.event_key == event_key)
                .values(status=STATUS_SENT, sent_at=at, last_error=None)
            )
            await session.commit()

    async def mark_retry(self, event_key: str, *, error: str, next_attempt_at: datetime) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(_Model)
                .where(_Model.event_key == event_key)
                .values(
                    status=STATUS_RETRY, last_error=error[:500], next_attempt_at=next_attempt_at
                )
            )
            await session.commit()

    async def mark_abandoned(self, event_key: str, *, error: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(_Model)
                .where(_Model.event_key == event_key)
                .values(status=STATUS_ABANDONED, last_error=error[:500])
            )
            await session.commit()

    async def get(self, event_key: str) -> BusinessLogRecord | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(select(_Model).where(_Model.event_key == event_key))
            ).scalar_one_or_none()
            return _to_record(row) if row is not None else None

    async def counts_by_status(self) -> dict[str, int]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(select(_Model.status, func.count()).group_by(_Model.status))
            ).all()
            return {str(status): int(count) for status, count in rows}
