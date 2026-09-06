"""Telegram delivery notifiers (LEASEWEB-MVP).

Implements the notifier ports of the order and renewal domains on top of the
aiogram bot:

- :class:`TelegramOrderDeliveryNotifier` — delivers the provisioned server
  details (status, IPv4/IPv6, OS, location, plan, next renewal, provider
  reference) to the OWNING user's Telegram chat. Never exposes credentials,
  API keys or internal ids.
- :class:`TelegramRenewalNotifier` — the 7/3/1-day renewal reminders to the
  customer and the insufficient-funds / manual-cancellation alerts to the
  operator chat (``TELEGRAM_ADMIN_CHAT_ID``).

Delivery failures are logged and swallowed: the notifiers are best-effort
by contract (the domain's persistent dedup logs guarantee at-most-once
independent of delivery success).
"""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

from aiogram import Bot

from cloud_platform.bot.monthly_ui import format_minor
from cloud_platform.modules.compute.domain import CloudServer, ServerLifecycleState
from cloud_platform.modules.offers.domain import SellableOffer
from cloud_platform.modules.orders.service import RenewalInfo
from cloud_platform.modules.renewals.domain import RenewalKind
from cloud_platform.modules.users.domain import UserRepository

logger = logging.getLogger(__name__)

_STATE_LABELS: dict[ServerLifecycleState, str] = {
    ServerLifecycleState.RUNNING: "فعال (active)",
    ServerLifecycleState.PROVISIONING: "در حال ساخت (provisioning)",
    ServerLifecycleState.STOPPED: "خاموش (stopped)",
    ServerLifecycleState.ERROR: "خطا (error)",
    ServerLifecycleState.REQUESTED: "در انتظار ثبت (pending)",
}


class TelegramOrderDeliveryNotifier:
    """Delivers the provisioned server card to the owning user's chat."""

    def __init__(self, bot: Bot, users: UserRepository) -> None:
        self._bot = bot
        self._users = users

    async def deliver(
        self, *, server: CloudServer, offer: SellableOffer, renewal: RenewalInfo
    ) -> None:
        user = await self._users.get(server.user_id)
        if user is None or user.telegram_user_id is None:
            logger.warning(
                "no telegram identity for user %s; server %s delivered via log",
                server.user_id,
                server.id,
            )
            return
        lines = [
            "✅ سرور شما آماده شد!",
            f"🖥️ پلن: {offer.name}",
            f"📍 لوکیشن: {offer.location_id}",
        ]
        if server.os:
            lines.append(f"💿 سیستم‌عامل: {server.os}")
        if server.ipv4:
            lines.append(f"🌐 IPv4: {server.ipv4}")
        if server.ipv6:
            lines.append(f"🌐 IPv6: {server.ipv6}")
        lines.append(
            f"💰 قیمت ماهانه: {format_minor(offer.selling_price_minor, offer.selling_currency)}"
        )
        if renewal.provider_renewal_at is not None:
            next_renewal = renewal.provider_renewal_at.strftime("%Y-%m-%d")
            lines.append(
                f"📅 تمدید بعدی: {next_renewal}"
                + (" (تخمینی)" if renewal.renewal_date_estimated else "")
            )
        lines.append(f"🆔 کد مرجع پشتیبانی: {server.provider_key}-{order_ref(server, renewal)}")
        await self._send(user.telegram_user_id, "\n".join(lines))

    async def _send(self, chat_id: int, text: str) -> None:
        try:
            await self._bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            logger.exception("telegram delivery to %s failed", chat_id)


def order_ref(server: CloudServer, renewal: RenewalInfo) -> str:
    """The customer-facing provider reference (order or contract id)."""
    return renewal.provider_order_ref or renewal.provider_contract_id or str(server.id)[:8]


class TelegramRenewalNotifier:
    """Customer reminders + operator alerts over Telegram."""

    def __init__(
        self,
        bot: Bot,
        users: UserRepository,
        admin_chat_id: int | None = None,
    ) -> None:
        self._bot = bot
        self._users = users
        self._admin_chat_id = admin_chat_id

    async def warn(
        self,
        *,
        user_id: UUID,
        server_id: UUID,
        kind: RenewalKind,
        days_left: int,
        renewal_at: datetime,
        price_minor: int,
        currency: str,
        balance_minor: int,
    ) -> None:
        price = format_minor(price_minor, currency)
        balance = format_minor(balance_minor, currency)
        lines = [
            f"⚠️ یادآوری تمدید سرور (شناسه: {str(server_id)[:8]})",
            f"فقط {days_left} روز تا تمدید باقی مانده است.",
            f"قیمت تمدید ماهانه: {price}",
            f"تاریخ تمدید: {renewal_at.strftime('%Y-%m-%d')}",
            f"موجودی کیف پول: {balance}",
        ]
        if kind in (RenewalKind.WARN_3D, RenewalKind.WARN_1D) and balance_minor < price_minor:
            lines.append("⚠️ موجودی کافی نیست؛ لطفاً حساب را شارژ کنید تا سرویس قطع نشود.")
        user = await self._users.get(user_id)
        if user is not None and user.telegram_user_id is not None:
            await self._send(user.telegram_user_id, "\n".join(lines))
        else:
            logger.warning("renewal warn: no telegram identity for user %s", user_id)

    async def charged(
        self,
        *,
        user_id: UUID,
        server_id: UUID,
        renewal_at: datetime,
        price_minor: int,
        currency: str,
    ) -> None:
        user = await self._users.get(user_id)
        if user is not None and user.telegram_user_id is not None:
            await self._send(
                user.telegram_user_id,
                "✅ تمدید ماهانه سرور انجام شد!\n"
                f"مبلغ: {format_minor(price_minor, currency)}\n"
                f"تمدید بعدی: {renewal_at.strftime('%Y-%m-%d')}",
            )

    async def alert(
        self,
        *,
        server_id: UUID,
        renewal_at: datetime,
        price_minor: int,
        currency: str,
        balance_minor: int,
        provider_refs: dict[str, str],
        reason: str = "",
    ) -> None:
        if self._admin_chat_id is None:
            logger.error(
                "RENEWAL ATTENTION server=%s renewal=%s price=%d %s balance=%d %s refs=%s "
                "reason=%s (TELEGRAM_ADMIN_CHAT_ID not set)",
                server_id,
                renewal_at.isoformat(),
                price_minor,
                currency,
                balance_minor,
                currency,
                provider_refs,
                reason,
            )
            return
        lines = [
            "🚨 توجه مدیر — تمدید سرویس",
            f"سرور: {server_id}",
            f"تاریخ تمدید: {renewal_at.strftime('%Y-%m-%d')}",
            f"قیمت: {format_minor(price_minor, currency)}",
            f"موجودی: {format_minor(balance_minor, currency)}",
        ]
        if reason:
            lines.append(f"دلیل: {reason}")
        for key, value in provider_refs.items():
            if value:
                lines.append(f"{key}: {value}")
        if "manual_cancellation" in reason or balance_minor < price_minor:
            lines.append(
                "⚠️ در صورت عدم پرداخت، سرویس باید در پورتال لیزوب به‌صورت دستی لغو شود "
                "(runbook: docs/operations/RUNBOOK.md)."
            )
        await self._send(self._admin_chat_id, "\n".join(lines))

    async def _send(self, chat_id: int, text: str) -> None:
        try:
            await self._bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            logger.exception("telegram notification to %s failed", chat_id)
