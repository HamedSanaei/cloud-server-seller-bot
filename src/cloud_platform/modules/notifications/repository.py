"""SQLAlchemy adapter for the provisioning notification log (M08-006)."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import ProvisioningNotification as _NotificationModel
from cloud_platform.modules.notifications.domain import ProvisioningEventKind


class SqlAlchemyProvisioningNotificationLogRepository:
    """Exactly-once log for final provisioning notifications.

    ``record_final`` inserts a row; the unique (server_id, kind) constraint
    makes a repeat a safe no-op returning False.
    """

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def record_final(
        self,
        user_id: object,
        server_id: object,
        kind: ProvisioningEventKind,
        detail: str | None,
    ) -> bool:
        if kind.is_final is False:
            raise ValueError("only final events may be recorded")
        async with self._session_factory() as session:
            try:
                session.add(
                    _NotificationModel(
                        user_id=user_id,
                        server_id=server_id,
                        kind=kind.value,
                        detail=detail,
                    )
                )
                await session.commit()
            except IntegrityError:
                # Already recorded: this is not the first final notification.
                await session.rollback()
                return False
            return True
