"""Provisioning progress notifications (M08-006).

The user is told how their server creation is going. The acceptance
property: **the user receives the final success/error exactly once.**

Design (same notifier-port pattern as the low-balance notifications):

- A :class:`ProvisioningNotifier` port receives :class:`ProvisioningEvent`s;
  the default implementation logs (the Telegram bot integration replaces it).
- Progress events (``started``) are fire-and-forget: duplicates are harmless.
- FINAL events (``success`` / ``failed``) are exactly-once: a persistent
  notification log with a unique ``(server_id, kind)`` constraint records
  each final notification, and the service delivers only when the record is
  the first one. A crash/retry/reconciler overlap can therefore never make
  the user see two "your server is ready" (or two failures) - and it can
  never swallow the outcome either, because the record is written by the
  first delivery attempt before the notifier is called.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from cloud_platform.modules.compute.domain import CloudServer

logger = logging.getLogger(__name__)


class ProvisioningEventKind(StrEnum):
    """One kind of provisioning notification.

    ``STARTED`` is a progress event; ``SUCCESS`` and ``FAILED`` are the
    final events and are the ones guaranteed exactly-once.
    """

    STARTED = "started"
    SUCCESS = "success"
    FAILED = "failed"

    @property
    def is_final(self) -> bool:
        return self is not ProvisioningEventKind.STARTED


@dataclass(frozen=True, slots=True)
class ProvisioningEvent:
    """One notification to the user about their server's provisioning."""

    user_id: UUID
    server_id: UUID
    kind: ProvisioningEventKind
    detail: str | None = None
    at: datetime | None = None


class ProvisioningNotifier(Protocol):
    """Port for delivering provisioning notifications (bot/API later)."""

    async def send(self, event: ProvisioningEvent) -> None:
        """Deliver ``event`` to the user (best effort per event)."""
        ...


class _LoggingNotifier:
    """Default notifier: structured log line (the bot integration replaces it)."""

    async def send(self, event: ProvisioningEvent) -> None:
        is_failure = event.kind is ProvisioningEventKind.FAILED
        logger.log(
            logging.WARNING if is_failure else logging.INFO,
            "provisioning %s for server %s (user %s)%s",
            event.kind.value,
            event.server_id,
            event.user_id,
            f": {event.detail}" if event.detail else "",
        )


class ProvisioningNotificationLogRepository(Protocol):
    """Persistent exactly-once log for FINAL provisioning notifications."""

    async def record_final(
        self,
        user_id: UUID,
        server_id: UUID,
        kind: ProvisioningEventKind,
        detail: str | None,
    ) -> bool:
        """Atomically record a final notification.

        Returns True when THIS call made the first record (the caller must
        deliver the notification) and False when a final notification of the
        same kind for the same server was already recorded (the caller must
        NOT deliver again).
        """
        ...


class ProvisioningProgressService:
    """Sends provisioning progress notifications with exactly-once finals."""

    def __init__(
        self,
        notifier: ProvisioningNotifier,
        log_repo: ProvisioningNotificationLogRepository,
    ) -> None:
        self._notifier = notifier
        self._log = log_repo

    @staticmethod
    def _event(
        server: CloudServer, kind: ProvisioningEventKind, detail: str | None
    ) -> ProvisioningEvent:
        return ProvisioningEvent(
            user_id=server.user_id,
            server_id=server.id,
            kind=kind,
            detail=detail,
            at=datetime.now(UTC),
        )

    async def started(self, server: CloudServer, detail: str | None = None) -> None:
        """Progress: the provider accepted/created the resource.

        Progress events are not deduplicated - a repeat is harmless.
        """
        await self._notifier.send(self._event(server, ProvisioningEventKind.STARTED, detail))

    async def succeeded(self, server: CloudServer, detail: str | None = None) -> bool:
        """FINAL: provisioning finished. Delivered exactly once.

        Returns True when this call delivered it, False when it was already
        delivered (crash/retry safety).
        """
        return await self._finish(server, ProvisioningEventKind.SUCCESS, detail)

    async def failed(self, server: CloudServer, reason: str | None = None) -> bool:
        """FINAL: provisioning failed. Delivered exactly once."""
        return await self._finish(server, ProvisioningEventKind.FAILED, reason)

    async def _finish(
        self, server: CloudServer, kind: ProvisioningEventKind, detail: str | None
    ) -> bool:
        first = await self._log.record_final(server.user_id, server.id, kind, detail)
        if not first:
            logger.info(
                "final provisioning %s for server %s already recorded; not re-notifying",
                kind.value,
                server.id,
            )
            return False
        event = self._event(server, kind, detail)
        await self._notifier.send(event)
        return True
