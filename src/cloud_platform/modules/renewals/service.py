"""Daily renewal checker (LEASEWEB-MVP).

Leaseweb renews services automatically at ITS OWN billing cycle; this job
protects us from silently paying for a customer service at our expense.

For every active :class:`RenewalRecord` the daily pass:

- sends the 7-day warning (exactly once per renewal period),
- at 3 days: warns again and flags ``INSUFFICIENT_FUNDS`` when the wallet
  cannot cover the customer price,
- at 1 day: warns the customer AND the admin when funds are still missing,
- ON the renewal date with sufficient funds and auto-charge enabled: posts
  EXACTLY ONE monthly debit using a deterministic idempotency key
  (``renewal-charge:{server_id}:{period}``) and advances the next renewal
  date by one month,
- with insufficient funds on the renewal date: after a short grace the
  record becomes ``MANUAL_CANCELLATION_REQUIRED`` and the admin is alerted
  with the provider identifiers needed to cancel in the Leaseweb portal
  (the MVP never invents a cancellation API).

The charge reuses the wallet hold/capture machinery: ``create_hold`` is
idempotent per key and ``capture_hold`` atomically debits the wallet and
posts the CHARGE ledger entry exactly once. Two overlapping job runs can
therefore never double-debit a wallet. All reminders are deduplicated in
``renewal_notifications`` keyed by (server, kind, renewal period).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from cloud_platform.modules.audit.domain import ActorType, AuditRepository
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.compute.domain import CloudServer, ServerRepository
from cloud_platform.modules.renewals.domain import (
    RenewalKind,
    RenewalNotificationRepository,
    RenewalRecord,
    RenewalRepository,
    RenewalStatus,
)
from cloud_platform.modules.wallet.domain import (
    Hold,
    HoldRepository,
    HoldStatus,
    InsufficientHoldBalanceError,
    WalletRepository,
)
from cloud_platform.modules.wallet.repository import HoldService
from cloud_platform.observability.metrics import metrics

logger = logging.getLogger(__name__)

#: How far ahead (days) of the renewal date each warning fires.
WARN_7D = timedelta(days=7)
WARN_3D = timedelta(days=3)
WARN_1D = timedelta(days=1)

#: One monthly period (renewal advance) when the provider gives no date.
MONTH_PERIOD = timedelta(days=30)

#: After the renewal date, how long an unpaid service stays in
#: ``INSUFFICIENT_FUNDS`` before it demands manual cancellation.
CANCEL_GRACE = timedelta(days=1)


class RenewalUserNotifier(Protocol):
    """Delivers renewal reminders to the owning user (bot integration later)."""

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
    ) -> None: ...

    async def charged(
        self,
        *,
        user_id: UUID,
        server_id: UUID,
        renewal_at: datetime,
        price_minor: int,
        currency: str,
    ) -> None: ...


class RenewalAdminNotifier(Protocol):
    """Delivers operator alerts (insufficient funds, manual cancellation)."""

    async def alert(
        self,
        *,
        server_id: UUID,
        renewal_at: datetime,
        price_minor: int,
        currency: str,
        balance_minor: int,
        provider_refs: dict[str, str],
        reason: str,
    ) -> None: ...


class _LoggingRenewalUserNotifier:
    """Default notifier: structured log lines (Telegram replaces it)."""

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
        logger.warning(
            "renewal %s for server %s (user %s): %d days left, renewal %s, "
            "price %d %s, balance %d %s",
            kind.value,
            server_id,
            user_id,
            days_left,
            renewal_at.isoformat(),
            price_minor,
            currency,
            balance_minor,
            currency,
        )

    async def charged(
        self,
        *,
        user_id: UUID,
        server_id: UUID,
        renewal_at: datetime,
        price_minor: int,
        currency: str,
    ) -> None:
        logger.info(
            "renewal charged for server %s (user %s): %d %s, next renewal %s",
            server_id,
            user_id,
            price_minor,
            currency,
            renewal_at.isoformat(),
        )


class _LoggingRenewalAdminNotifier:
    """Default admin notifier: structured log lines (Telegram admin chat later)."""

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
        logger.error(
            "RENEWAL ATTENTION server=%s renewal=%s price=%d %s balance=%d %s refs=%s reason=%s",
            server_id,
            renewal_at.isoformat(),
            price_minor,
            currency,
            balance_minor,
            currency,
            provider_refs,
            reason,
        )


@dataclass(frozen=True, slots=True)
class RenewalCheckOutcome:
    """Per-record outcome of one renewal pass."""

    server_id: UUID
    action: str  # warned_7d | warned_3d | warned_1d | charged | insufficient |
    #             # cancelled_required | skipped
    balance_minor: int | None = None


def renewal_charge_key(server_id: UUID, period: datetime) -> str:
    """Deterministic exactly-once key for one monthly renewal debit."""
    day = period.date().isoformat()
    return f"renewal-charge:{server_id}:{day}"


def _period_of(renewal_at: datetime) -> datetime:
    """The renewal period identity: the renewal instant truncated to a day."""
    return renewal_at.replace(hour=0, minute=0, second=0, microsecond=0)


class RenewalChecker:
    """Daily renewal pass: warnings, exactly-once charge, operator flags."""

    def __init__(
        self,
        *,
        renewals_repo: RenewalRepository,
        notification_repo: RenewalNotificationRepository,
        server_repo: ServerRepository,
        wallet_repo: WalletRepository,
        hold_repo: HoldRepository,
        hold_service: HoldService,
        audit_repo: AuditRepository,
        user_notifier: RenewalUserNotifier | None = None,
        admin_notifier: RenewalAdminNotifier | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._renewals = renewals_repo
        self._log = notification_repo
        self._servers = server_repo
        self._wallets = wallet_repo
        self._holds = hold_repo
        self._hold_service = hold_service
        self._audit = AuditTrail(audit_repo)
        self._user_notifier = user_notifier or _LoggingRenewalUserNotifier()
        self._admin_notifier = admin_notifier or _LoggingRenewalAdminNotifier()
        self._now = clock or (lambda: datetime.now(UTC))

    async def run(self) -> list[RenewalCheckOutcome]:
        """One full pass over every non-cancelled renewal record."""
        outcomes: list[RenewalCheckOutcome] = []
        for record in await self._renewals.list_active():
            try:
                outcomes.append(await self._check(record))
            except Exception:
                logger.exception("renewal check failed for server %s", record.server_id)
        return outcomes

    async def _check(self, record: RenewalRecord) -> RenewalCheckOutcome:
        now = self._now()
        if record.provider_renewal_at is None:
            return RenewalCheckOutcome(record.server_id, "skipped")
        renewal_at = record.provider_renewal_at
        if renewal_at.tzinfo is None:
            renewal_at = renewal_at.replace(tzinfo=UTC)
        period = _period_of(renewal_at)

        server = await self._servers.get(record.server_id)
        if server is None:
            return RenewalCheckOutcome(record.server_id, "skipped")
        balance = await self._wallet_balance(server)

        remaining = renewal_at - now

        # --- Notifications before the renewal date -------------------------
        if remaining > timedelta(0):
            if remaining <= WARN_7D and remaining > WARN_3D:
                return await self._warn(record, server, balance, RenewalKind.WARN_7D, 7, period)
            if remaining <= WARN_3D and remaining > WARN_1D:
                await self._flag_funds(record, server, balance)
                return await self._warn(record, server, balance, RenewalKind.WARN_3D, 3, period)
            if remaining <= WARN_1D:
                await self._flag_funds(record, server, balance)
                outcome = await self._warn(record, server, balance, RenewalKind.WARN_1D, 1, period)
                if balance < record.customer_price_minor:
                    await self._admin_alert(record, server, balance, period, "insufficient_funds")
                return outcome
            return RenewalCheckOutcome(record.server_id, "skipped")

        # --- Renewal date reached ------------------------------------------
        if not record.auto_charge_enabled:
            return await self._manual_cancellation(record, server, balance, period)
        if balance < record.customer_price_minor:
            await self._flag_funds(record, server, balance)
            await self._admin_alert(record, server, balance, period, "insufficient_funds")
            if now >= renewal_at + CANCEL_GRACE:
                return await self._manual_cancellation(record, server, balance, period)
            return RenewalCheckOutcome(record.server_id, "insufficient", balance)

        charged = await self._charge(record, server, period)
        if not charged:
            return RenewalCheckOutcome(record.server_id, "insufficient", balance)
        next_renewal = renewal_at + MONTH_PERIOD
        record.provider_renewal_at = next_renewal
        record.status = RenewalStatus.ACTIVE
        record.last_checked_at = now
        await self._renewals.upsert(record)
        first_charge_notice = await self._log.record(server.id, RenewalKind.CHARGED, period)
        if first_charge_notice:
            await self._user_notifier.charged(
                user_id=server.user_id,
                server_id=server.id,
                renewal_at=next_renewal,
                price_minor=record.customer_price_minor,
                currency=record.currency,
            )
        metrics.record_billing_event("renewal_charged", "ok")
        logger.info(
            "renewal charged for server %s: %d %s, next renewal %s",
            record.server_id,
            record.customer_price_minor,
            record.currency,
            next_renewal.isoformat(),
        )
        return RenewalCheckOutcome(record.server_id, "charged", balance)

    # -- helpers ------------------------------------------------------------

    async def _wallet_balance(self, server: CloudServer) -> int:
        wallet = await self._wallets.get(server.user_id)
        return wallet.balance if wallet is not None else 0

    async def _warn(
        self,
        record: RenewalRecord,
        server: CloudServer,
        balance: int,
        kind: RenewalKind,
        days: int,
        period: datetime,
    ) -> RenewalCheckOutcome:
        first = await self._log.record(server.id, kind, period)
        if first:
            await self._user_notifier.warn(
                user_id=server.user_id,
                server_id=server.id,
                kind=kind,
                days_left=days,
                renewal_at=record.provider_renewal_at or datetime.now(UTC),
                price_minor=record.customer_price_minor,
                currency=record.currency,
                balance_minor=balance,
            )
        record.last_checked_at = self._now()
        await self._renewals.upsert(record)
        return RenewalCheckOutcome(record.server_id, kind.value, balance)

    async def _flag_funds(self, record: RenewalRecord, server: CloudServer, balance: int) -> None:
        if balance < record.customer_price_minor and record.status is RenewalStatus.ACTIVE:
            record.status = RenewalStatus.INSUFFICIENT_FUNDS
            record.last_checked_at = self._now()
            await self._renewals.upsert(record)
            metrics.record_billing_event("renewal_insufficient", "flagged")

    async def _admin_alert(
        self,
        record: RenewalRecord,
        server: CloudServer,
        balance: int,
        period: datetime,
        reason: str,
    ) -> None:
        kind = (
            RenewalKind.MANUAL_CANCELLATION
            if reason == "manual_cancellation_required"
            else RenewalKind.ADMIN_INSUFFICIENT
        )
        first = await self._log.record(server.id, kind, period)
        if not first:
            return
        await self._admin_notifier.alert(
            server_id=server.id,
            renewal_at=record.provider_renewal_at or datetime.now(UTC),
            price_minor=record.customer_price_minor,
            currency=record.currency,
            balance_minor=balance,
            provider_refs={
                "provider": server.provider_key,
                "provider_server_id": server.provider_server_id or "",
                "provider_order_ref": record.provider_order_ref or "",
                "provider_contract_id": record.provider_contract_id or "",
            },
            reason=reason,
        )
        await self._audit.record_mutation(
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            action="renewal.admin_alert",
            resource_type="server",
            resource_id=str(server.id),
            reason=f"renewal {reason}: balance {balance} {record.currency} < "
            f"{record.customer_price_minor} {record.currency}",
            metadata={
                "renewal_at": (record.provider_renewal_at or datetime.now(UTC)).isoformat(),
                "price_minor": str(record.customer_price_minor),
                "balance_minor": str(balance),
                "currency": record.currency,
            },
        )

    async def _charge(self, record: RenewalRecord, server: CloudServer, period: datetime) -> bool:
        """Exactly-once monthly debit. True when the charge was applied."""
        wallet = await self._wallets.get(server.user_id)
        if wallet is None or wallet.id is None:
            return False
        wallet_id = wallet.id
        key = renewal_charge_key(server.id, period)

        existing = await self._holds.get_by_idempotency(wallet_id, key)
        if existing is not None and existing.status is HoldStatus.CAPTURED:
            # Already charged for this period (crash/rerun) — count as success.
            metrics.record_billing_event("renewal_charged", "replayed")
            return True

        if existing is not None and existing.status is HoldStatus.CREATED:
            hold: Hold | None = existing
        else:
            try:
                hold = await self._holds.create_hold(
                    wallet_id, record.customer_price_minor, record.currency, key
                )
            except InsufficientHoldBalanceError:
                return False
            except IntegrityError:
                # Concurrent run created the hold first; resolve to it.
                hold = await self._holds.get_by_idempotency(wallet_id, key)
        if hold is None:
            return False
        assert hold.id is not None
        try:
            await self._hold_service.capture_hold(wallet_id, hold.id, key)
        except Exception:
            logger.exception("renewal capture failed for server %s", record.server_id)
            return False
        return True

    async def _manual_cancellation(
        self, record: RenewalRecord, server: CloudServer, balance: int, period: datetime
    ) -> RenewalCheckOutcome:
        if record.status is not RenewalStatus.MANUAL_CANCELLATION_REQUIRED:
            record.status = RenewalStatus.MANUAL_CANCELLATION_REQUIRED
            record.last_checked_at = self._now()
            await self._renewals.upsert(record)
        await self._admin_alert(record, server, balance, period, "manual_cancellation_required")
        metrics.record_billing_event("renewal_cancellation_required", "flagged")
        logger.error(
            "MANUAL CANCELLATION REQUIRED for server %s (order %s, contract %s, "
            "provider server %s) — unpaid service must not renew at our expense",
            record.server_id,
            record.provider_order_ref,
            record.provider_contract_id,
            server.provider_server_id,
        )
        return RenewalCheckOutcome(record.server_id, "cancelled_required", balance)
