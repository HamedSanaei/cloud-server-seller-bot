"""Customer-safe website inbox and Telegram fan-out using the durable outbox."""

from __future__ import annotations

from hashlib import sha256
from typing import Any
from uuid import UUID

from cloud_platform.modules.businesslog.domain import (
    BusinessEvent,
    BusinessEventType,
    OutboxBusinessEventSink,
    format_minor,
)
from cloud_platform.modules.notifications.domain import (
    LowBalanceNotificationEvent,
    LowBalanceNotificationKind,
    render_low_balance_notice,
)

# Allowlisted titles/fields only: operator diagnostics and credentials never go to users.
TITLES = {
    "purchase.requested": "درخواست ساخت سرور ثبت شد",
    "purchase.provider_accepted": "ارائه‌دهنده درخواست ساخت سرور را پذیرفت",
    "purchase.failed": "ساخت سرور ناموفق است یا نیاز به بررسی دارد",
    "recharge.created": "درخواست شارژ کیف پول ثبت شد",
    "recharge.succeeded": "شارژ کیف پول انجام شد",
    "recharge.failed": "شارژ کیف پول ناموفق بود",
    "admin.wallet_adjustment": "موجودی کیف پول توسط مدیر اصلاح شد",
    "server.started": "درخواست روشن کردن سرور پذیرفته شد",
    "server.stopped": "درخواست خاموش کردن سرور پذیرفته شد",
    "server.reboot_requested": "درخواست راه‌اندازی مجدد سرور پذیرفته شد",
    "server.reinstall_requested": "درخواست نصب مجدد سیستم‌عامل پذیرفته شد",
    "server.password_reset_requested": "درخواست بازنشانی رمز سرور پذیرفته شد",
    "server.operation_failed": "عملیات سرور ناموفق است یا نیاز به بررسی دارد",
    "server.snapshot_created": "درخواست ساخت اسنپ‌شات ثبت شد",
    "server.snapshot_restored": "درخواست بازیابی اسنپ‌شات ثبت شد",
    "server.snapshot_deleted": "درخواست حذف اسنپ‌شات ثبت شد",
    "server.ip_null_routed": "مسیر دسترسی آی‌پی سرور مسدود شد",
    "server.ip_unnull_routed": "مسیر دسترسی آی‌پی سرور باز شد",
    "service.renewal_warning": "هشدار نزدیک شدن زمان تمدید سرور",
    "service.renewal_failed_insufficient_balance": "موجودی برای تمدید کافی نیست",
    "service.suspended": "سرویس تعلیق شد",
    "service.renewal_succeeded": "سرویس تمدید شد",
    "service.renewal_due": "زمان تمدید سرویس رسیده است",
    "service.grace_started": "مهلت پرداخت تمدید سرویس آغاز شد",
    "service.grace_expired": "مهلت پرداخت تمدید سرویس پایان یافت",
    "service.auto_renew_enabled": "تمدید خودکار سرویس فعال شد",
    "service.auto_renew_disabled": "تمدید خودکار سرویس غیرفعال شد",
}


async def enqueue_notice(
    repository: Any, *, key: str, user_id: UUID, title: str, server_id: str | None = None
) -> bool:
    payload = {"user_id": str(user_id), "text": title[:1800]}
    if server_id:
        payload["server_id"] = server_id
    return bool(
        await repository.enqueue(
            event_key="notice:" + sha256(key.encode()).hexdigest(),
            event_type=BusinessEventType.CUSTOMER_NOTICE.value,
            payload=payload,
        )
    )


class LowBalanceOutboxNotifier:
    """One durable event supplies the website inbox and Telegram delivery."""

    def __init__(self, repository: Any) -> None:
        self.repository = repository

    async def send(self, event: LowBalanceNotificationEvent) -> None:
        resource = (
            "aggregate" if event.kind is LowBalanceNotificationKind.RUNWAY else str(event.server_id)
        )
        await enqueue_notice(
            self.repository,
            key=f"balance:{event.user_id}:{resource}:{event.kind.value}:{event.episode.isoformat()}",
            user_id=event.user_id,
            title=render_low_balance_notice(event),
            server_id=str(event.server_id),
        )


class CustomerEventSink:
    """Publish customer notices even when the optional operator logger is disabled."""

    def __init__(self, repository: Any, policy: Any) -> None:
        self.repository = repository
        self.operator = OutboxBusinessEventSink(repository, policy)
        self.policy = policy

    async def emit(self, event: BusinessEvent) -> bool:
        operator = await self.operator.emit(event)
        title = TITLES.get(event.event_type.value)
        if not title:
            return operator
        payload = event.sanitized_payload()
        try:
            user_id = UUID(str(payload["user_id"]))
        except (KeyError, ValueError, TypeError):
            return operator
        server_id = payload.get("server_id")
        if server_id:
            title += f"\nسرور: {str(server_id)[:8]}"
        amount = format_minor(payload.get("amount_minor"), payload.get("currency"))
        if amount:
            title += f"\nمبلغ: {amount}"
        sent = await enqueue_notice(
            self.repository,
            key=event.event_key,
            user_id=user_id,
            title=title,
            server_id=str(server_id) if server_id else None,
        )
        return sent or operator
