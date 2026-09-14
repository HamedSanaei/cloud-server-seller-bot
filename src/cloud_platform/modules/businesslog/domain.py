"""Private Telegram business-logger channel (release hardening).

The operator needs a business feed — purchases, provider acceptances,
provisioning results, recharges, recharges failures, admin wallet
adjustments — in a private Telegram channel. That feed is an integration
concern, so it is modelled as one:

- **Structured events** (:class:`BusinessEventType` + :class:`BusinessEvent`)
  with a stable, deterministic ``event_key``. The key makes emission and
  delivery idempotent, so a retried checkout, a re-run worker or a retried
  delivery can never post the same business event twice.
- **A durable outbox**. :class:`BusinessEventSink` only *enqueues* (one DB
  insert, unique key). Nothing is sent inline. A Telegram outage therefore
  cannot fail a checkout, roll back a wallet charge, re-POST a provider
  order or delay reconciliation — the worst case is a delayed log line.
- **Bounded, claimed delivery**. :class:`BusinessLogDispatcher` claims
  events atomically (``PENDING``/``RETRY`` -> ``SENDING``), sends one
  message, then marks ``SENT``. Failures back off exponentially up to
  ``max_attempts`` and then become ``ABANDONED`` so a permanently broken
  channel can never flood it or spin forever.

Security: every payload is passed through
:func:`cloud_platform.core.logging.sanitize_payload` before it is stored or
rendered — structured redaction of tokens, passwords, Authorization /
``X-LSW-Auth`` headers, cloud-init and root credentials, plus value
truncation. Root passwords and provider credentials never reach the channel.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from cloud_platform.core.logging import sanitize_payload

logger = logging.getLogger(__name__)

#: Statuses of one outbox row (durable delivery state machine).
STATUS_PENDING = "PENDING"
STATUS_SENDING = "SENDING"
STATUS_SENT = "SENT"
STATUS_RETRY = "RETRY"
STATUS_ABANDONED = "ABANDONED"


class BusinessEventType(StrEnum):
    """Every business event the operator channel can carry."""

    PURCHASE_REQUESTED = "purchase.requested"
    PROVIDER_ACCEPTED = "purchase.provider_accepted"
    VPS_PROVISIONED = "purchase.vps_provisioned"
    PURCHASE_FAILED = "purchase.failed"
    RECHARGE_CREATED = "recharge.created"
    RECHARGE_SUCCEEDED = "recharge.succeeded"
    RECHARGE_FAILED = "recharge.failed"
    ADMIN_WALLET_ADJUSTMENT = "admin.wallet_adjustment"
    # Customer server management (Telegram My Servers). Read-only page views
    # are deliberately NOT events: only state-changing operations are logged.
    SERVER_STARTED = "server.started"
    SERVER_STOPPED = "server.stopped"
    SERVER_REBOOT_REQUESTED = "server.reboot_requested"
    SERVER_CONSOLE_REQUESTED = "server.console_requested"
    SERVER_REINSTALL_REQUESTED = "server.reinstall_requested"
    SERVER_SNAPSHOT_CREATED = "server.snapshot_created"
    SERVER_SNAPSHOT_RESTORED = "server.snapshot_restored"
    SERVER_SNAPSHOT_DELETED = "server.snapshot_deleted"
    SERVER_PASSWORD_RESET_REQUESTED = "server.password_reset_requested"
    SERVER_IP_NULL_ROUTED = "server.ip_null_routed"
    SERVER_IP_UNNULL_ROUTED = "server.ip_unnull_routed"
    SERVER_OPERATION_FAILED = "server.operation_failed"
    # Commercial service lifecycle (PROD-HARDENING §39). These describe the
    # CUSTOMER's billing state — never the provider's infrastructure state —
    # and never carry a wallet secret, credential or provider key.
    SERVICE_RENEWAL_WARNING = "service.renewal_warning"
    SERVICE_RENEWAL_DUE = "service.renewal_due"
    SERVICE_RENEWAL_SUCCEEDED = "service.renewal_succeeded"
    SERVICE_RENEWAL_FAILED_INSUFFICIENT_BALANCE = "service.renewal_failed_insufficient_balance"
    SERVICE_GRACE_STARTED = "service.grace_started"
    SERVICE_GRACE_EXPIRED = "service.grace_expired"
    SERVICE_SUSPENDED = "service.suspended"
    SERVICE_OPERATOR_ATTENTION_REQUIRED = "service.operator_attention_required"
    SERVICE_AUTO_RENEW_ENABLED = "service.auto_renew_enabled"
    SERVICE_AUTO_RENEW_DISABLED = "service.auto_renew_disabled"

    @property
    def log_flag(self) -> str:
        """The configuration flag that enables this event type."""
        return _FLAGS[self]


_FLAGS: Mapping[BusinessEventType, str] = {
    BusinessEventType.PURCHASE_REQUESTED: "log_purchases",
    BusinessEventType.PROVIDER_ACCEPTED: "log_purchases",
    BusinessEventType.VPS_PROVISIONED: "log_purchases",
    BusinessEventType.PURCHASE_FAILED: "log_order_failures",
    BusinessEventType.RECHARGE_CREATED: "log_recharges",
    BusinessEventType.RECHARGE_SUCCEEDED: "log_recharges",
    BusinessEventType.RECHARGE_FAILED: "log_payment_failures",
    BusinessEventType.ADMIN_WALLET_ADJUSTMENT: "log_admin_wallet_adjustments",
    BusinessEventType.SERVER_STARTED: "log_server_management",
    BusinessEventType.SERVER_STOPPED: "log_server_management",
    BusinessEventType.SERVER_REBOOT_REQUESTED: "log_server_management",
    BusinessEventType.SERVER_CONSOLE_REQUESTED: "log_server_management",
    BusinessEventType.SERVER_REINSTALL_REQUESTED: "log_server_management",
    BusinessEventType.SERVER_SNAPSHOT_CREATED: "log_server_management",
    BusinessEventType.SERVER_SNAPSHOT_RESTORED: "log_server_management",
    BusinessEventType.SERVER_SNAPSHOT_DELETED: "log_server_management",
    BusinessEventType.SERVER_PASSWORD_RESET_REQUESTED: "log_server_management",
    BusinessEventType.SERVER_IP_NULL_ROUTED: "log_server_management",
    BusinessEventType.SERVER_IP_UNNULL_ROUTED: "log_server_management",
    BusinessEventType.SERVER_OPERATION_FAILED: "log_server_management",
    # The commercial lifecycle rides on the same operator toggle: it is one
    # per-server notice feed, so an operator who wants server notices wants
    # these too (and can still silence the whole channel with ``enabled``).
    BusinessEventType.SERVICE_RENEWAL_WARNING: "log_server_management",
    BusinessEventType.SERVICE_RENEWAL_DUE: "log_server_management",
    BusinessEventType.SERVICE_RENEWAL_SUCCEEDED: "log_server_management",
    BusinessEventType.SERVICE_RENEWAL_FAILED_INSUFFICIENT_BALANCE: "log_server_management",
    BusinessEventType.SERVICE_GRACE_STARTED: "log_server_management",
    BusinessEventType.SERVICE_GRACE_EXPIRED: "log_server_management",
    BusinessEventType.SERVICE_SUSPENDED: "log_server_management",
    BusinessEventType.SERVICE_OPERATOR_ATTENTION_REQUIRED: "log_server_management",
    BusinessEventType.SERVICE_AUTO_RENEW_ENABLED: "log_server_management",
    BusinessEventType.SERVICE_AUTO_RENEW_DISABLED: "log_server_management",
}

#: Persian-first titles for the channel cards.
_TITLES: Mapping[BusinessEventType, str] = {
    BusinessEventType.PURCHASE_REQUESTED: "🛒 درخواست خرید سرور",
    BusinessEventType.PROVIDER_ACCEPTED: "✅ پذیرش سفارش توسط پروایدر",
    BusinessEventType.VPS_PROVISIONED: "🚀 سرور ساخته و تحویل شد",
    BusinessEventType.PURCHASE_FAILED: "❌ خطا در خرید / نیازمند بررسی",
    BusinessEventType.RECHARGE_CREATED: "🧾 درخواست شارژ کیف پول",
    BusinessEventType.RECHARGE_SUCCEEDED: "💰 شارژ کیف پول موفق",
    BusinessEventType.RECHARGE_FAILED: "⚠️ شارژ کیف پول ناموفق",
    BusinessEventType.ADMIN_WALLET_ADJUSTMENT: "🛠️ اصلاح موجودی توسط مدیر",
    BusinessEventType.SERVER_STARTED: "🟢 روشن کردن سرور",
    BusinessEventType.SERVER_STOPPED: "🔴 خاموش کردن سرور",
    BusinessEventType.SERVER_REBOOT_REQUESTED: "🔄 درخواست ریبوت سرور",
    BusinessEventType.SERVER_CONSOLE_REQUESTED: "🖥 درخواست کنسول سرور",
    BusinessEventType.SERVER_REINSTALL_REQUESTED: "💿 درخواست نصب مجدد",
    BusinessEventType.SERVER_SNAPSHOT_CREATED: "📸 ساخت Snapshot",
    BusinessEventType.SERVER_SNAPSHOT_RESTORED: "♻️ بازیابی Snapshot",
    BusinessEventType.SERVER_SNAPSHOT_DELETED: "🗑 حذف Snapshot",
    BusinessEventType.SERVER_PASSWORD_RESET_REQUESTED: "🔑 درخواست بازنشانی رمز",
    BusinessEventType.SERVER_IP_NULL_ROUTED: "🚫 Null route روی IP",
    BusinessEventType.SERVER_IP_UNNULL_ROUTED: "✅ برداشتن Null route",
    BusinessEventType.SERVER_OPERATION_FAILED: "⚠️ خطا در عملیات سرور",
    BusinessEventType.SERVICE_RENEWAL_WARNING: "⏰ یادآوری تمدید سرویس",
    BusinessEventType.SERVICE_RENEWAL_DUE: "📅 سررسید تمدید سرویس",
    BusinessEventType.SERVICE_RENEWAL_SUCCEEDED: "✅ تمدید سرویس انجام شد",
    BusinessEventType.SERVICE_RENEWAL_FAILED_INSUFFICIENT_BALANCE: (
        "⚠️ تمدید سرویس: موجودی کیف پول کافی نیست"
    ),
    BusinessEventType.SERVICE_GRACE_STARTED: "🕒 شروع مهلت پرداخت",
    BusinessEventType.SERVICE_GRACE_EXPIRED: "⌛ پایان مهلت پرداخت",
    BusinessEventType.SERVICE_SUSPENDED: "⛔ تعلیق سرویس (تجاری)",
    BusinessEventType.SERVICE_OPERATOR_ATTENTION_REQUIRED: "🔔 سرویس نیازمند بررسی مدیر",
    BusinessEventType.SERVICE_AUTO_RENEW_ENABLED: "🔄 فعال‌سازی تمدید خودکار",
    BusinessEventType.SERVICE_AUTO_RENEW_DISABLED: "🔕 غیرفعال‌سازی تمدید خودکار",
}

#: Field labels for the rendered card (Persian-first, English hints).
_LABELS: Mapping[str, str] = {
    "at": "زمان",
    "user_id": "کاربر داخلی",
    "telegram_user_id": "Telegram ID",
    "username": "نام کاربری",
    "market": "بازار",
    "provider": "پروایدر",
    "location": "لوکیشن",
    "plan": "پلن",
    "product_id": "کد محصول پروایدر",
    "os": "سیستم‌عامل",
    "amount": "مبلغ",
    "currency": "ارز",
    "selling_price": "قیمت فروش",
    "provider_cost": "هزینه پروایدر",
    "server_id": "شناسه سرور",
    "order_id": "شناسه سفارش",
    "provider_order_id": "شناسه سفارش پروایدر",
    "operation_key": "کلید عملیات",
    "failed_event": "کلید رویداد ناموفق",
    "kind": "نوع رویداد",
    "state": "وضعیت نهایی",
    "operation": "عملیات",
    "result": "نتیجه",
    "snapshot": "Snapshot",
    "image": "ایمیج",
    "ip": "IP",
    "ipv4": "IPv4",
    "ipv6": "IPv6",
    "category": "دسته خطا",
    "reason": "دلیل",
    "gateway": "درگاه",
    "payment_session_id": "شناسه پرداخت",
    "gateway_reference": "کد مرجع درگاه",
    "balance_after": "موجودی بعد از تراکنش",
    "actor": "مدیر عامل",
    "admin_id": "شناسه مدیر",
    "entry_type": "نوع تراکنش",
    "renewal_at": "تاریخ تمدید",
    "period_end": "پایان دوره",
    "grace_until": "مهلت پرداخت",
    "days_left": "روز باقی‌مانده",
    "auto_renew": "تمدید خودکار",
}

#: Order the fields are rendered in (stable, operator-friendly).
_FIELD_ORDER: tuple[str, ...] = (
    "at",
    "kind",
    "market",
    "provider",
    "location",
    "plan",
    "product_id",
    "os",
    "amount",
    "currency",
    "selling_price",
    "provider_cost",
    "balance_after",
    "user_id",
    "telegram_user_id",
    "username",
    "admin_id",
    "actor",
    "entry_type",
    "server_id",
    "order_id",
    "provider_order_id",
    "operation_key",
    "renewal_at",
    "period_end",
    "grace_until",
    "days_left",
    "auto_renew",
    "payment_session_id",
    "gateway",
    "gateway_reference",
    "ipv4",
    "ipv6",
    "ip",
    "state",
    "operation",
    "result",
    "image",
    "snapshot",
    "renewal_at",
    "category",
    "reason",
)


class BusinessLogError(Exception):
    """Base error for business-log operations."""


@dataclass(frozen=True, slots=True)
class BusinessLogPolicy:
    """Which business events the operator channel receives.

    ``enabled=False`` (or an unset ``chat_id``) turns the whole feed off
    without touching call sites: the sink becomes a no-op.
    """

    enabled: bool = False
    chat_id: int = 0
    log_purchases: bool = True
    log_recharges: bool = True
    log_payment_failures: bool = True
    log_order_failures: bool = True
    log_admin_wallet_adjustments: bool = True
    log_server_management: bool = True
    max_attempts: int = 5
    batch_size: int = 50
    backoff_base_seconds: int = 30
    stale_claim_seconds: int = 300

    @property
    def active(self) -> bool:
        """Whether the channel can actually receive anything."""
        return bool(self.enabled and self.chat_id)

    def allows(self, event_type: BusinessEventType) -> bool:
        """Whether ``event_type`` is enabled by the per-category flags."""
        if not self.active:
            return False
        return bool(getattr(self, event_type.log_flag, True))

    @classmethod
    def from_settings(cls, settings: Any) -> BusinessLogPolicy:
        """Build the policy from the application settings (TOML section)."""
        return cls(
            enabled=bool(getattr(settings, "telegram_logger_enabled", False)),
            chat_id=int(getattr(settings, "telegram_logger_chat_id", 0) or 0),
            log_purchases=bool(getattr(settings, "telegram_logger_log_purchases", True)),
            log_recharges=bool(getattr(settings, "telegram_logger_log_recharges", True)),
            log_payment_failures=bool(
                getattr(settings, "telegram_logger_log_payment_failures", True)
            ),
            log_order_failures=bool(getattr(settings, "telegram_logger_log_order_failures", True)),
            log_admin_wallet_adjustments=bool(
                getattr(settings, "telegram_logger_log_admin_wallet_adjustments", True)
            ),
            log_server_management=bool(
                getattr(settings, "telegram_logger_log_server_management", True)
            ),
        )


@dataclass(frozen=True, slots=True)
class BusinessEvent:
    """One operator-channel business event.

    ``event_key`` is the durable identity: enqueueing the same key twice is a
    no-op, so repeating the same operation (Telegram double tap, worker
    retry, reconciler pass) never duplicates a channel message.
    """

    event_key: str
    event_type: BusinessEventType
    payload: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.event_key or not self.event_key.strip():
            raise ValueError("event_key must not be empty")
        if len(self.event_key) > 200:
            raise ValueError("event_key must be at most 200 characters")

    def sanitized_payload(self) -> dict[str, Any]:
        """Redacted, JSON-safe payload (never raises on odd value types)."""
        try:
            return sanitize_payload(self.payload)
        except Exception:  # pragma: no cover - defensive: logging must not break callers
            logger.warning("business event payload could not be sanitized", exc_info=True)
            return {}


def compact(**fields: Any) -> dict[str, Any]:
    """Build a payload, dropping unset (``None``/empty) values."""
    return {key: value for key, value in fields.items() if value is not None and value != ""}


def format_minor(minor: int | None, currency: str | None) -> str | None:
    """Integer money rendering (never float).

    Returns None for anything that is not an integer: money only ever enters
    a business event as integer minor units, and an oddly-typed value must
    degrade to "field omitted" rather than break the renderer.
    """
    if not isinstance(minor, int):
        return None
    # divmod already carries the sign for negatives (-450 -> (-5, 50)).
    major, rem = divmod(minor, 100)
    return f"{major}.{rem:02d} {currency or ''}".strip()


def render_event(event_type: BusinessEventType, payload: Mapping[str, Any]) -> str:
    """Render one event as the operator card sent to the channel."""
    lines = [_TITLES.get(event_type, event_type.value)]
    remaining = dict(payload)
    for key in _FIELD_ORDER:
        if key not in remaining:
            continue
        value = remaining.pop(key)
        label = _LABELS.get(key, key)
        lines.append(f"{label}: {value}")
    for key in sorted(remaining):
        lines.append(f"{_LABELS.get(key, key)}: {remaining[key]}")
    return "\n".join(str(line) for line in lines)


def event_key(*parts: object) -> str:
    """Deterministic event key from parts (``a:b:c``)."""
    return ":".join(str(part) for part in parts if part not in (None, ""))


def uuid_text(value: UUID | str | None) -> str | None:
    """Stable textual form of a uuid (or None)."""
    if value is None:
        return None
    return str(value)


class BusinessEventSink(Protocol):
    """Port: durably record a business event (never sends inline)."""

    async def emit(self, event: BusinessEvent) -> bool:
        """Persist ``event``; True when it was newly recorded."""
        ...


async def emit_safe(sink: BusinessEventSink | None, event: BusinessEvent) -> bool:
    """Emit ``event`` through ``sink``, never raising into the caller.

    Call sites sit on the financial path (checkout, wallet capture, provider
    acceptance). Business logging is observational: a broken sink must not
    be able to fail a purchase, and the failure is visible in the app logs.
    """
    if sink is None:
        return False
    try:
        return await sink.emit(event)
    except Exception:
        logger.warning(
            "business event %s (%s) could not be emitted",
            event.event_key,
            event.event_type.value,
            exc_info=True,
        )
        return False


class BusinessLogRepository(Protocol):
    """Port: the durable business-log outbox."""

    async def enqueue(
        self,
        *,
        event_key: str,
        event_type: str,
        payload: Mapping[str, Any],
        at: datetime | None = None,
    ) -> bool:
        """Insert the row; False when ``event_key`` already exists."""
        ...

    async def claim_due(self, *, limit: int, now: datetime, stale_after_seconds: int) -> list[Any]:
        """Atomically claim due (or stale) rows for delivery."""
        ...

    async def mark_sent(self, event_key: str, *, at: datetime) -> None: ...

    async def mark_retry(
        self, event_key: str, *, error: str, next_attempt_at: datetime
    ) -> None: ...

    async def mark_abandoned(self, event_key: str, *, error: str) -> None: ...

    async def get(self, event_key: str) -> Any | None: ...

    async def counts_by_status(self) -> dict[str, int]: ...


class NullBusinessEventSink:
    """No-op sink used when the operator channel is disabled."""

    async def emit(self, event: BusinessEvent) -> bool:
        return False


class OutboxBusinessEventSink:
    """Records business events in the durable outbox.

    Emission is **best effort by contract**: the sink never raises into the
    caller, because the caller may be the financial path (checkout, wallet
    capture, provider acceptance). A failed enqueue is logged and dropped —
    an operator notification is worth less than a wallet transaction.
    """

    def __init__(self, repository: BusinessLogRepository, policy: BusinessLogPolicy) -> None:
        self._repo = repository
        self._policy = policy

    @property
    def policy(self) -> BusinessLogPolicy:
        return self._policy

    async def emit(self, event: BusinessEvent) -> bool:
        if not self._policy.allows(event.event_type):
            return False
        try:
            return await self._repo.enqueue(
                event_key=event.event_key,
                event_type=event.event_type.value,
                payload=event.sanitized_payload(),
                at=event.created_at,
            )
        except Exception:
            logger.warning(
                "business event %s (%s) could not be enqueued",
                event.event_key,
                event.event_type.value,
                exc_info=True,
            )
            return False


class BusinessLogChannel(Protocol):
    """Port: delivery transport for one rendered business event."""

    async def send(self, text: str) -> None:
        """Deliver ``text`` to the operator channel; raising means retry."""
        ...


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    """Outcome of one dispatcher pass."""

    sent: int = 0
    retried: int = 0
    abandoned: int = 0
    pending: int = 0


def _safe_error(exc: BaseException) -> str:
    """A short, redacted error description for the durable retry record."""
    safe = sanitize_payload({"error": str(exc), "type": type(exc).__name__})
    return str(safe.get("error", "delivery failed"))[:500]


class BusinessLogDispatcher:
    """Delivers claimed business events with bounded retry/backoff.

    Idempotency: rows are claimed atomically before sending, so two workers
    cannot both deliver the same event. A row whose ``SENDING`` claim went
    stale (worker crash *after* the Telegram call) is reclaimed — that rare
    case may duplicate one message, which is strictly better than silently
    losing it, and the attempt cap keeps a broken channel from flooding.
    """

    def __init__(
        self,
        repository: BusinessLogRepository,
        channel: BusinessLogChannel,
        policy: BusinessLogPolicy,
    ) -> None:
        self._repo = repository
        self._channel = channel
        self._policy = policy

    async def deliver(
        self, *, now: datetime | None = None, limit: int | None = None
    ) -> DeliveryReport:
        """One delivery pass over the outbox."""
        if not self._policy.active:
            return DeliveryReport()
        moment = now or datetime.now(UTC)
        claimed = await self._repo.claim_due(
            limit=limit or self._policy.batch_size,
            now=moment,
            stale_after_seconds=self._policy.stale_claim_seconds,
        )
        sent = retried = abandoned = 0
        for record in claimed:
            key = str(record.event_key)
            attempts = int(getattr(record, "attempts", 1) or 1)
            if attempts > self._policy.max_attempts:
                await self._repo.mark_abandoned(
                    key, error=f"exceeded {self._policy.max_attempts} delivery attempts"
                )
                abandoned += 1
                continue
            try:
                await self._channel.send(self.render(record))
            except Exception as exc:
                delay = self._policy.backoff_base_seconds * (2 ** (attempts - 1))
                await self._repo.mark_retry(
                    key,
                    error=_safe_error(exc),
                    next_attempt_at=moment + timedelta(seconds=delay),
                )
                retried += 1
                continue
            await self._repo.mark_sent(key, at=moment)
            sent += 1
        return DeliveryReport(sent=sent, retried=retried, abandoned=abandoned)

    @staticmethod
    def render(record: Any) -> str:
        """Render one claimed outbox row (unknown types render verbatim)."""
        raw_type = str(getattr(record, "event_type", ""))
        try:
            event_type = BusinessEventType(raw_type)
        except ValueError:
            payload = dict(getattr(record, "payload", {}) or {})
            lines = [f"رویداد نامشخص: {raw_type}"]
            lines.extend(f"{key}: {value}" for key, value in sorted(payload.items()))
            return "\n".join(lines)
        payload = dict(getattr(record, "payload", {}) or {})
        created = getattr(record, "created_at", None)
        if created is not None and "at" not in payload:
            payload["at"] = str(created)
        return render_event(event_type, payload)


__all__ = [
    "STATUS_ABANDONED",
    "STATUS_PENDING",
    "STATUS_RETRY",
    "STATUS_SENDING",
    "STATUS_SENT",
    "BusinessEvent",
    "BusinessEventSink",
    "BusinessEventType",
    "BusinessLogChannel",
    "BusinessLogDispatcher",
    "BusinessLogError",
    "BusinessLogPolicy",
    "BusinessLogRepository",
    "DeliveryReport",
    "NullBusinessEventSink",
    "OutboxBusinessEventSink",
    "compact",
    "emit_safe",
    "event_key",
    "format_minor",
    "render_event",
    "uuid_text",
]
