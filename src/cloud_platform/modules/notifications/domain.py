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

from cloud_platform.modules.billing.service import LowBalanceDecision
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


# ---------------------------------------------------------------------------
# Low-balance notifications (M08-011)
# ---------------------------------------------------------------------------


class LowBalanceNotificationKind(StrEnum):
    """The warning levels the user is notified about.

    One level per (server, episode) is delivered: a repeat of the SAME level
    in the SAME episode is a no-op (the policy job may run twice, and a
    re-sent delivery must not double-notify). A NEW episode (the watermark
    was cleared and set again) may re-notify.
    """

    WARN = "warn"
    AUTO_DELETE = "auto_delete"
    RECOVERED = "recovered"


@dataclass(frozen=True, slots=True)
class LowBalanceNotificationEvent:
    """One low-balance notification to the user."""

    user_id: UUID
    server_id: UUID
    kind: LowBalanceNotificationKind
    balance_minor: int
    currency: str
    episode: datetime
    at: datetime | None = None

    def render(self) -> str:
        """ASCII rendering (integer-formatted money, no floats)."""
        major, minor = divmod(abs(self.balance_minor), 100)
        sign = "-" if self.balance_minor < 0 else ""
        return (
            f"low-balance {self.kind.value} for server {self.server_id}: "
            f"balance {sign}{major}.{minor:02d} {self.currency} "
            f"(episode {self.episode.isoformat()})"
        )


class LowBalanceNotifierPort(Protocol):
    """Port for delivering low-balance notifications (bot/API later)."""

    async def send(self, event: LowBalanceNotificationEvent) -> None:
        """Deliver ``event`` to the user (best effort per event)."""
        ...


class _LoggingLowBalanceNotifier:
    """Default notifier: structured log line (the bot integration replaces it)."""

    async def send(self, event: LowBalanceNotificationEvent) -> None:
        logger.warning("low-balance %s: %s", event.kind.value, event.render())


class LowBalanceNotificationLogRepository(Protocol):
    """Persistent dedup log for low-balance notifications.

    Unique (server_id, kind, episode): the first delivery attempt of one
    level in one episode inserts the row; repeats hit the constraint and
    must not deliver again.
    """

    async def record(
        self,
        user_id: UUID,
        server_id: UUID,
        kind: LowBalanceNotificationKind,
        episode: datetime,
        balance_minor: int,
        currency: str,
    ) -> bool:
        """Atomically record one notification; True only for the first one."""
        ...


_DECISION_TO_KIND: dict[LowBalanceDecision, LowBalanceNotificationKind | None] = {
    LowBalanceDecision.WARN: LowBalanceNotificationKind.WARN,
    LowBalanceDecision.AUTO_DELETE: LowBalanceNotificationKind.AUTO_DELETE,
    LowBalanceDecision.RECOVERED: LowBalanceNotificationKind.RECOVERED,
    LowBalanceDecision.NONE: None,
    LowBalanceDecision.GRACE: None,
}


def _decision_kind(decision: LowBalanceDecision) -> LowBalanceNotificationKind | None:
    """Map a policy decision onto the user-notification kind (None = silent)."""
    return _DECISION_TO_KIND.get(decision)


class LowBalanceNotifier:
    """User-facing low-balance notifications with per-level dedup (M08-011).

    Acceptance: **deduplicated warning levels.** The policy (M06-007)
    decides the level (WARN / GRACE / AUTO_DELETE / RECOVERED); this
    notifier turns it into exactly-once user notifications:

    - GRACE is silent by design (the user was warned when the episode
      opened) and never touches the log.
    - WARN / AUTO_DELETE / RECOVERED are recorded in the persistent log
      keyed by (server, level, episode); only the first record delivers,
      so a double-run of the policy job cannot notify twice, while a new
      episode (new watermark) notifies again.
    """

    def __init__(
        self,
        notifier: LowBalanceNotifierPort,
        log_repo: LowBalanceNotificationLogRepository,
    ) -> None:
        self._notifier = notifier
        self._log = log_repo

    async def notify(
        self,
        user_id: UUID,
        server_id: UUID,
        decision: LowBalanceDecision,
        balance_minor: int,
        currency: str,
        episode: datetime,
    ) -> bool:
        """Deliver one notification for ``decision``; True when delivered.

        ``episode`` is the server's low-balance watermark (the instant the
        episode opened): the policy passes the new watermark on WARN, the
        existing one on AUTO_DELETE, and the cleared one on RECOVERED.
        """
        kind = _decision_kind(decision)
        if kind is None:
            return False  # NONE and GRACE are not user notifications
        first = await self._log.record(user_id, server_id, kind, episode, balance_minor, currency)
        if not first:
            logger.info(
                "low-balance %s for server %s episode %s already recorded; not re-notifying",
                kind.value,
                server_id,
                episode.isoformat(),
            )
            return False
        await self._notifier.send(
            LowBalanceNotificationEvent(
                user_id=user_id,
                server_id=server_id,
                kind=kind,
                balance_minor=balance_minor,
                currency=currency,
                episode=episode,
                at=datetime.now(UTC),
            )
        )
        return True
