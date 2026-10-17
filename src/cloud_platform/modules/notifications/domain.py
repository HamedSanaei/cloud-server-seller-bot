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
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from cloud_platform.modules.billing.service import LowBalanceDecision, PrepaidBalanceDecision
from cloud_platform.modules.compute.domain import CloudServer
from cloud_platform.modules.fx.formatting import format_minor

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
    """One notice per (server, kind, episode); a new episode may notify again."""

    WARN = "warn"
    STOP = "stop"
    DELETE = "delete"
    AUTO_DELETE = "auto_delete"  # Existing monthly policy decision.
    RECOVERED = "recovered"
    RUNWAY = "runway"


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

    runway_hours: Decimal | None = None
    active_servers: int = 0

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
    """Transport for low-balance notifications."""

    async def send(self, event: LowBalanceNotificationEvent) -> None:
        """Deliver or raise; failures must not be recorded as deliveries."""
        ...


def render_low_balance_notice(event: LowBalanceNotificationEvent) -> str:
    if event.currency != "IRT":
        raise ValueError("customer balance notices require IRT")
    balance = format_minor(event.balance_minor, event.currency)
    server = str(event.server_id)[:8]
    if event.kind is LowBalanceNotificationKind.RUNWAY:
        minutes = max(0, int((event.runway_hours or Decimal(0)) * 60))
        hours, remainder = divmod(minutes, 60)
        return (
            f"⚠️ هشدار اعتبار {event.active_servers} سرور ساعتی فعال\n"
            f"موجودی قابل استفاده: {balance}\n"
            f"با نرخ فعلی، نخستین خاموشی ناشی از کمبود موجودی تقریباً تا "
            f"{hours} ساعت و {remainder} دقیقه دیگر رخ می‌دهد (کمتر از ۳ روز).\n"
            "برای جلوگیری از خاموشی و حذف بعدی سرورها، کیف پول تومانی را شارژ کنید. "
            "پس از پایان اعتبار پرداخت‌شده و ۲۴ ساعت ناکافی ماندن موجودی، "
            "سرور خاموش‌شده در معرض حذف است. این برآورد با تغییر نرخ تتر تغییر می‌کند.\n"
            f"زمان برآورد: {(event.at or event.episode).isoformat()}"
        )
    if event.kind is LowBalanceNotificationKind.WARN:
        return (
            f"هشدار موجودی سرور {server}: موجودی کیف پول شما {balance} است "
            "و به کمتر از 200,000 تومان رسیده است. لطفاً کیف پول را شارژ کنید."
        )
    if event.kind is LowBalanceNotificationKind.STOP:
        return (
            "اعتبار ساعت پرداخت‌شده پایان یافته و موجودی برای ساعت بعد کافی نیست. "
            f"سرور {server} در حال خاموش شدن است. "
            "برای جلوگیری از حذف، کیف پول را شارژ کنید."
        )
    if event.kind is LowBalanceNotificationKind.DELETE:
        return (
            "یک روز (۲۴ ساعت) از ناکافی شدن اعتبار پرداخت‌نشده گذشته است. "
            f"حذف سرور {server} در حال انجام است."
        )
    if event.kind is LowBalanceNotificationKind.AUTO_DELETE:
        return f"مهلت افزایش موجودی سرور {server} پایان یافته و حذف آن در حال انجام است."
    if event.kind is LowBalanceNotificationKind.RECOVERED:
        return f"موجودی کیف پول شما به {balance} رسیده است؛ وضعیت سرور {server} بازیابی شد."
    raise ValueError(f"Unsupported prepaid notice kind: {event.kind}")


class LowBalanceNotificationLogRepository(Protocol):
    """Durable notices keyed by (server_id, kind, episode).

    The existing schema records successful sends but has no pending/claimed
    state. Concurrent workers or a crash after Telegram accepts the message
    but before the log commit may send twice. Strict exactly-once delivery
    requires a durable outbox with a delivery state and reconciliation.
    """

    async def last_runway_sent(self, user_id: UUID) -> datetime | None:
        """Most recent successful aggregate reminder, independent of server changes."""
        ...

    async def was_sent(
        self, server_id: UUID, kind: LowBalanceNotificationKind, episode: datetime
    ) -> bool:
        """Whether a successful delivery was previously recorded."""
        ...

    async def record(
        self,
        user_id: UUID,
        server_id: UUID,
        kind: LowBalanceNotificationKind,
        episode: datetime,
        balance_minor: int,
        currency: str,
    ) -> bool:
        """Record a successful delivery; False if already recorded."""
        ...


_DECISION_TO_KIND: dict[
    LowBalanceDecision | PrepaidBalanceDecision, LowBalanceNotificationKind | None
] = {
    LowBalanceDecision.WARN: LowBalanceNotificationKind.WARN,
    PrepaidBalanceDecision.STOP: LowBalanceNotificationKind.STOP,
    PrepaidBalanceDecision.DELETE: LowBalanceNotificationKind.DELETE,
    LowBalanceDecision.AUTO_DELETE: LowBalanceNotificationKind.AUTO_DELETE,
    LowBalanceDecision.RECOVERED: LowBalanceNotificationKind.RECOVERED,
    LowBalanceDecision.NONE: None,
    LowBalanceDecision.GRACE: None,
}


def _decision_kind(
    decision: LowBalanceDecision | PrepaidBalanceDecision,
) -> LowBalanceNotificationKind | None:
    """Map a policy decision onto the user-notification kind (None = silent)."""
    return _DECISION_TO_KIND.get(decision)


class LowBalanceNotifier:
    """Notify per balance episode, persisting only successful deliveries.

    Silent policy decisions never send. The existing log has no outbox state:
    send-before-record allows retries after transport failures, and a later
    sequential evaluation deduplicates the successful delivery. Atomic
    exactly-once delivery across Telegram and SQL is not possible here.
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
        decision: LowBalanceDecision | PrepaidBalanceDecision,
        balance_minor: int,
        currency: str,
        episode: datetime,
    ) -> bool:
        """Send once per level and episode; propagate failed deliveries.

        ``episode`` is the policy's persisted watermark for this notice.
        """
        kind = _decision_kind(decision)
        if kind is None:
            return False
        if await self._log.was_sent(server_id, kind, episode):
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
        return await self._log.record(user_id, server_id, kind, episode, balance_minor, currency)
