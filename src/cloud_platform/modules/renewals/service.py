"""Commercial lifecycle pass: warnings, collection, grace, suspension (§17-§34).

The provider renews ITS contract with us at its own billing cycle. This job is
about the CUSTOMER's side of that: charging their wallet, warning them, running
the grace period and — only if the operator explicitly opted in — suspending.

One pass over every non-cancelled service, per service:

1. **inside ``charge_before_expiry_hours``** — try the automatic charge when
   the customer has auto-renew on and the wallet covers the LOCAL price;
   otherwise send the crossed warning thresholds (each exactly once per period,
   deduplicated in the database);
2. **at/after expiry with auto-renew on** — charge, or fall into
   ``PAYMENT_DUE`` and start the grace window (``grace_until``);
3. **inside grace** — re-try the charge on every pass (a recharge is picked up
   without any customer action);
4. **grace expired** — ``SUSPENDED``. Suspension is a COMMERCIAL state: the
   provider server is only stopped if ``stop_server_after_grace`` is on, and it
   is never terminated, reinstalled or deleted.

Exactly-once money
------------------

Every debit reuses the wallet hold/capture machinery: ``create_hold`` is
idempotent per key and ``capture_hold`` atomically debits the wallet and posts
one CHARGE ledger entry. The key is deterministic per (server, period):

    renewal-charge:<server_id>:<period-start-date>

so two workers, ten passes, or a crash-and-rerun can never double-charge.
Concurrency is settled in PostgreSQL (row lock on ``renewals`` + the hold's
unique idempotency key), never in Redis.

The renewal amount is ``record.customer_price_minor`` — the LOCAL price the
customer bought at. It is never re-derived from a current provider quote (§27).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from cloud_platform.modules.audit.domain import ActorType, AuditRepository
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.businesslog.domain import (
    BusinessEventSink,
    BusinessEventType,
    emit_safe,
)
from cloud_platform.modules.businesslog.events import service_renewal_event
from cloud_platform.modules.compute.domain import CloudServer, ServerRepository
from cloud_platform.modules.renewals.domain import (
    PAYABLE_STATUSES,
    RenewalKind,
    RenewalNotificationRepository,
    RenewalRecord,
    RenewalRepository,
    RenewalStatus,
)
from cloud_platform.modules.renewals.policy import CommerceRenewalPolicy
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

#: One monthly period (renewal advance) when the provider gives no exact date.
MONTH_PERIOD = timedelta(days=30)

#: The COMMERCIAL business event each notification kind corresponds to (§39).
#: The notification log is deduplicated per (server, kind, period), so mapping
#: the event onto the same gate keeps the operator channel exactly-once too.
_KIND_EVENT: dict[RenewalKind, BusinessEventType] = {
    RenewalKind.WARN_7D: BusinessEventType.SERVICE_RENEWAL_WARNING,
    RenewalKind.WARN_3D: BusinessEventType.SERVICE_RENEWAL_WARNING,
    RenewalKind.WARN_1D: BusinessEventType.SERVICE_RENEWAL_WARNING,
    RenewalKind.WARN_168H: BusinessEventType.SERVICE_RENEWAL_WARNING,
    RenewalKind.WARN_72H: BusinessEventType.SERVICE_RENEWAL_WARNING,
    RenewalKind.WARN_24H: BusinessEventType.SERVICE_RENEWAL_WARNING,
    RenewalKind.RENEWAL_DUE: BusinessEventType.SERVICE_RENEWAL_DUE,
    RenewalKind.GRACE_STARTED: BusinessEventType.SERVICE_GRACE_STARTED,
    RenewalKind.GRACE_EXPIRED: BusinessEventType.SERVICE_GRACE_EXPIRED,
    RenewalKind.SUSPENDED: BusinessEventType.SERVICE_SUSPENDED,
    RenewalKind.CHARGED: BusinessEventType.SERVICE_RENEWAL_SUCCEEDED,
    RenewalKind.ADMIN_INSUFFICIENT: (BusinessEventType.SERVICE_RENEWAL_FAILED_INSUFFICIENT_BALANCE),
    RenewalKind.MANUAL_CANCELLATION: BusinessEventType.SERVICE_OPERATOR_ATTENTION_REQUIRED,
}


@dataclass(frozen=True, slots=True)
class _OwnerRef:
    """The user fields a business event may carry (id only; never a secret)."""

    id: UUID


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
    #: warned_* | renewal_due | charged | payment_due | grace_started |
    #: grace_retry | suspended | cancelled_required | skipped
    action: str
    balance_minor: int | None = None


@dataclass(frozen=True, slots=True)
class RenewalNowOutcome:
    """The result of a CUSTOMER-initiated settlement of the current period.

    ``reason`` is a stable machine code the application layer maps to wording:

    - ``charged`` — this call settled the period;
    - ``already_charged`` — the period was already paid for (double click, two
      replicas, or a crash between the capture and the period advance). The
      customer is told it was already processed and is charged NOTHING more;
    - ``insufficient_funds`` — the wallet cannot cover the local price;
    - ``not_payable`` — nothing is due (still active, cancelled, expired) —
      early renewal is deliberately not offered (§37);
    - ``manual_review_required`` — the service is more than one period behind,
      so one settlement cannot clear it: the operator owns that case;
    - ``no_renewal_record`` / ``no_due_date`` — the platform cannot bill it.
    """

    server_id: UUID
    reason: str
    settled: bool = False
    status: RenewalStatus = RenewalStatus.ACTIVE
    amount_minor: int = 0
    currency: str = ""
    period_end: datetime | None = None
    grace_until: datetime | None = None
    auto_renew_enabled: bool = True


def renewal_charge_key(server_id: UUID, period: datetime) -> str:
    """Deterministic exactly-once key for one renewal debit."""
    day = period.date().isoformat()
    return f"renewal-charge:{server_id}:{day}"


def _period_of(renewal_at: datetime) -> datetime:
    """The renewal period identity: the renewal instant truncated to a day."""
    return renewal_at.replace(hour=0, minute=0, second=0, microsecond=0)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class RenewalChecker:
    """One commercial-lifecycle pass: warnings, charge, grace, suspension."""

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
        policy: CommerceRenewalPolicy | None = None,
        user_notifier: RenewalUserNotifier | None = None,
        admin_notifier: RenewalAdminNotifier | None = None,
        event_sink: BusinessEventSink | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._renewals = renewals_repo
        self._log = notification_repo
        self._servers = server_repo
        self._wallets = wallet_repo
        self._holds = hold_repo
        self._hold_service = hold_service
        self._audit = AuditTrail(audit_repo)
        self._policy = policy or CommerceRenewalPolicy()
        self._user_notifier = user_notifier or _LoggingRenewalUserNotifier()
        self._admin_notifier = admin_notifier or _LoggingRenewalAdminNotifier()
        self._events = event_sink
        self._now = clock or (lambda: datetime.now(UTC))

    @property
    def policy(self) -> CommerceRenewalPolicy:
        """The collection policy this pass applies."""
        return self._policy

    async def run(self) -> list[RenewalCheckOutcome]:
        """One full pass over every non-cancelled renewal record.

        A single failing service never stops the batch: each is checked inside
        its own guard and reported as an ``error`` outcome.
        """
        if not self._policy.enabled:
            logger.info("renewal checking disabled by [commerce.renewal] enabled=false")
            return []
        outcomes: list[RenewalCheckOutcome] = []
        for record in await self._renewals.list_active():
            try:
                outcomes.append(await self._check(record))
            except Exception:
                logger.exception("renewal check failed for server %s", record.server_id)
                outcomes.append(RenewalCheckOutcome(record.server_id, "error"))
        return outcomes

    # ------------------------------------------------------------------
    # One service
    # ------------------------------------------------------------------

    async def _check(self, record: RenewalRecord) -> RenewalCheckOutcome:
        now = self._now()
        if record.provider_renewal_at is None:
            # No known due instant: nothing to bill against. The customer price
            # is durable, but a made-up date would be worse than waiting.
            return RenewalCheckOutcome(record.server_id, "skipped")
        renewal_at = _aware(record.provider_renewal_at)
        period = _period_of(renewal_at)

        server = await self._servers.get(record.server_id)
        if server is None:
            return RenewalCheckOutcome(record.server_id, "skipped")
        balance = await self._wallet_balance(server)
        remaining = renewal_at - now
        hours_left = remaining.total_seconds() / 3600.0

        # --- Before expiry: warn, and collect early once the window opens ---
        if remaining > timedelta(0):
            await self._warn_crossed_thresholds(record, server, balance, hours_left, period)
            if (
                record.auto_charge_enabled
                and self._policy.charge_window_open(hours_left)
                and balance >= record.customer_price_minor
            ):
                return await self._settle(record, server, balance, renewal_at, period)
            if record.status is RenewalStatus.ACTIVE and balance < record.customer_price_minor:
                await self._warn_funds(record, server, balance, period, hours_left)
            return RenewalCheckOutcome(record.server_id, "warned", balance)

        # --- The period ended: the renewal is now due -----------------------
        await self._mark_due(record, server, balance, renewal_at, period)

        grace_end = _aware(record.grace_until) if record.grace_until else None
        if grace_end is None:
            grace_end = renewal_at + timedelta(hours=self._policy.grace_period_hours)

        if now >= grace_end:
            return await self._suspend(record, server, balance, grace_end, period)

        if record.auto_charge_enabled and balance >= record.customer_price_minor:
            return await self._settle(record, server, balance, renewal_at, period)

        # Payable window: the customer can still recharge, and we re-try on the
        # next pass so a top-up is picked up without any extra action (§35).
        if not record.auto_charge_enabled:
            # Auto-renew is OFF: never charge automatically, but the service is
            # still due and the customer is told exactly that.
            await self._raise_customer(record, server, balance, RenewalKind.RENEWAL_DUE, 0, period)
            return RenewalCheckOutcome(record.server_id, "renewal_due", balance)
        await self._alert_insufficient(record, server, balance, grace_end, period)
        return RenewalCheckOutcome(record.server_id, "grace_retry", balance)

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------

    async def _mark_due(
        self,
        record: RenewalRecord,
        server: CloudServer,
        balance: int,
        renewal_at: datetime,
        period: datetime,
    ) -> None:
        """Move ACTIVE -> PAYMENT_DUE once, and record the grace deadline."""
        if record.status in (RenewalStatus.PAYMENT_DUE, RenewalStatus.GRACE_PERIOD):
            if record.grace_until is None:
                record.grace_until = renewal_at + timedelta(hours=self._policy.grace_period_hours)
                record.last_checked_at = self._now()
                await self._renewals.upsert(record)
            return
        if record.status is not RenewalStatus.ACTIVE:
            return
        record.status = RenewalStatus.PAYMENT_DUE
        record.grace_until = renewal_at + timedelta(hours=self._policy.grace_period_hours)
        record.last_checked_at = self._now()
        await self._renewals.upsert(record)
        metrics.record_billing_event("renewal_due", "flagged")
        await self._raise_customer(record, server, balance, RenewalKind.RENEWAL_DUE, 0, period)

    async def _suspend(
        self,
        record: RenewalRecord,
        server: CloudServer,
        balance: int,
        grace_end: datetime,
        period: datetime,
    ) -> RenewalCheckOutcome:
        """Grace expired: SUSPENDED (commercial), never \"cancelled\" (§30-§31)."""
        first = await self._log.record(server.id, RenewalKind.GRACE_EXPIRED, period)
        if record.status is not RenewalStatus.SUSPENDED:
            record.status = RenewalStatus.SUSPENDED
            record.last_checked_at = self._now()
            await self._renewals.upsert(record)
            metrics.record_billing_event("renewal_suspended", "flagged")
        if first:
            # The customer is told the truth: the service is suspended
            # commercially; whether the machine was stopped is a separate,
            # operator-configured decision.
            await self._raise_customer(record, server, balance, RenewalKind.SUSPENDED, 0, period)
            await self._admin_alert(record, server, balance, grace_end, "grace_expired")
            await self._audit.record_mutation(
                actor_type=ActorType.SYSTEM,
                actor_id=None,
                action="service.suspended",
                resource_type="server",
                resource_id=str(server.id),
                reason=(
                    f"grace expired at {grace_end.isoformat()}: balance {balance} "
                    f"{record.currency} < {record.customer_price_minor} {record.currency}"
                ),
                metadata={
                    "price_minor": str(record.customer_price_minor),
                    "balance_minor": str(balance),
                    "currency": record.currency,
                    "stop_server_after_grace": str(self._policy.stop_server_after_grace),
                },
            )
        return RenewalCheckOutcome(record.server_id, "suspended", balance)

    async def _settle(
        self,
        record: RenewalRecord,
        server: CloudServer,
        balance: int,
        renewal_at: datetime,
        period: datetime,
    ) -> RenewalCheckOutcome:
        """Charge the current period exactly once and advance the service."""
        if not await self._charge(record, server, period):
            return RenewalCheckOutcome(record.server_id, "grace_retry", balance)
        next_renewal = renewal_at + MONTH_PERIOD
        record.provider_renewal_at = next_renewal
        record.status = RenewalStatus.ACTIVE
        record.grace_until = None
        record.last_checked_at = self._now()
        await self._renewals.upsert(record)
        first = await self._log.record(server.id, RenewalKind.CHARGED, period)
        if first:
            await self._user_notifier.charged(
                user_id=server.user_id,
                server_id=server.id,
                renewal_at=next_renewal,
                price_minor=record.customer_price_minor,
                currency=record.currency,
            )
            await self._emit(
                BusinessEventType.SERVICE_RENEWAL_SUCCEEDED,
                record,
                server,
                result="charged",
                period=period,
                period_end=next_renewal,
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

    # ------------------------------------------------------------------
    # Customer-initiated settlement (§36-§37)
    # ------------------------------------------------------------------

    async def record_for(self, server_id: UUID) -> RenewalRecord | None:
        """The commercial record of one service (None when it has none)."""
        return await self._renewals.get(server_id)

    async def set_auto_renew(self, server_id: UUID, enabled: bool) -> RenewalRecord | None:
        """Persist the customer's automatic-renewal preference durably.

        Returns ``None`` when the service has no commercial record, so the
        caller can report "not available" instead of inventing one. Callers
        MUST have verified ownership first.
        """
        try:
            return await self._renewals.set_auto_renew(server_id, enabled)
        except LookupError:
            return None

    async def settle_now(self, server_id: UUID) -> RenewalNowOutcome:
        """Settle the CURRENT period from the wallet, exactly once.

        This is the manual "renew now" path, and it reuses the very same
        idempotent machinery as the automatic pass: the debit is keyed by
        ``(server, period)`` on the wallet hold, so a double click or a second
        replica can never charge twice. It never calls a provider API — the
        provider renews its own contract with us (§28).
        """
        record = await self._renewals.lock_for_update(server_id)
        if record is None:
            return RenewalNowOutcome(server_id, "no_renewal_record", settled=False)
        if record.provider_renewal_at is None:
            return RenewalNowOutcome(
                server_id,
                "no_due_date",
                settled=False,
                status=record.status,
                amount_minor=record.customer_price_minor,
                currency=record.currency,
                auto_renew_enabled=record.auto_charge_enabled,
            )
        renewal_at = _aware(record.provider_renewal_at)
        now = self._now()
        period = _period_of(renewal_at)
        base = RenewalNowOutcome(
            server_id,
            "not_payable",
            settled=False,
            status=record.status,
            amount_minor=record.customer_price_minor,
            currency=record.currency,
            period_end=renewal_at,
            grace_until=_aware(record.grace_until) if record.grace_until else None,
            auto_renew_enabled=record.auto_charge_enabled,
        )
        if record.status not in PAYABLE_STATUSES:
            # No early renewal: only a due/grace/suspended period may be settled.
            return base
        if renewal_at > now:
            return base
        if now - renewal_at > MONTH_PERIOD:
            # More than one period behind: one settlement cannot clear the
            # exposure, so this belongs to the operator, not to a click.
            metrics.record_billing_event("renewal_manual", "overdue")
            return replace(base, reason="manual_review_required")

        server = await self._servers.get(server_id)
        if server is None:
            return replace(base, reason="no_renewal_record")
        wallet = await self._wallets.get(server.user_id)
        balance = wallet.balance if wallet is not None else 0
        already = await self._period_captured(server_id, wallet, period)

        outcome = await self._settle(record, server, balance, renewal_at, period)
        if outcome.action != "charged":
            metrics.record_billing_event("renewal_manual", "insufficient")
            return replace(base, reason="insufficient_funds")
        reason = "already_charged" if already else "charged"
        metrics.record_billing_event("renewal_manual", reason)
        return RenewalNowOutcome(
            server_id,
            reason,
            settled=True,
            status=RenewalStatus.ACTIVE,
            amount_minor=record.customer_price_minor,
            currency=record.currency,
            period_end=_aware(record.provider_renewal_at or renewal_at),
            grace_until=None,
            auto_renew_enabled=record.auto_charge_enabled,
        )

    async def _period_captured(self, server_id: UUID, wallet: object, period: datetime) -> bool:
        """Whether this period's debit has already been captured.

        Used only to choose the customer-facing wording ("charged" versus
        "already processed"); it never decides whether to charge, so a race here
        cannot cause a double debit.
        """
        wallet_id = getattr(wallet, "id", None)
        if wallet_id is None:
            return False
        key = renewal_charge_key(server_id, period)
        try:
            hold = await self._holds.get_by_idempotency(wallet_id, key)
        except Exception:  # pragma: no cover - diagnostics must never bill
            logger.warning("renewal: could not read the period hold for server %s", server_id)
            return False
        return hold is not None and hold.status is HoldStatus.CAPTURED

    # ------------------------------------------------------------------
    # Money
    # ------------------------------------------------------------------

    async def _charge(self, record: RenewalRecord, server: CloudServer, period: datetime) -> bool:
        """Exactly-once period debit. True when the renewal is paid for."""
        wallet = await self._wallets.get(server.user_id)
        if wallet is None or wallet.id is None:
            return False
        wallet_id = wallet.id
        key = renewal_charge_key(server.id, period)

        existing = await self._holds.get_by_idempotency(wallet_id, key)
        if existing is not None and existing.status is HoldStatus.CAPTURED:
            # Already charged for this period (crash/rerun/other worker).
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
                # A concurrent run created the hold first; resolve to it.
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

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    async def _warn_crossed_thresholds(
        self,
        record: RenewalRecord,
        server: CloudServer,
        balance: int,
        hours_left: float,
        period: datetime,
    ) -> None:
        """Send every configured threshold this service has reached, once."""
        for hours in self._policy.warnings_due(hours_left):
            await self._raise_customer(
                record, server, balance, RenewalKind.for_warning_hours(hours), hours, period
            )

    async def _raise_customer(
        self,
        record: RenewalRecord,
        server: CloudServer,
        balance: int,
        kind: RenewalKind,
        hours: int,
        period: datetime,
    ) -> None:
        """Deliver one deduplicated customer notification for this period."""
        first = await self._log.record(server.id, kind, period)
        if not first:
            return
        await self._user_notifier.warn(
            user_id=server.user_id,
            server_id=server.id,
            kind=kind,
            days_left=max(0, -(-hours // 24)),
            renewal_at=_aware(record.provider_renewal_at or datetime.now(UTC)),
            price_minor=record.customer_price_minor,
            currency=record.currency,
            balance_minor=balance,
        )
        event_type = _KIND_EVENT.get(kind)
        if event_type is not None:
            await self._emit(
                event_type,
                record,
                server,
                result=kind.value,
                period=period,
                days_left=max(0, -(-hours // 24)),
                grace_until=record.grace_until,
            )

    async def _warn_funds(
        self,
        record: RenewalRecord,
        server: CloudServer,
        balance: int,
        period: datetime,
        hours_left: float,
    ) -> None:
        """Tell the operator about a shortfall inside the warning window."""
        if not self._policy.warnings_due(hours_left):
            return
        await self._alert_insufficient(
            record,
            server,
            balance,
            _aware(record.provider_renewal_at or datetime.now(UTC)),
            period,
        )

    async def _alert_insufficient(
        self,
        record: RenewalRecord,
        server: CloudServer,
        balance: int,
        deadline: datetime,
        period: datetime,
    ) -> None:
        await self._admin_alert(record, server, balance, deadline, "insufficient_funds")

    async def _admin_alert(
        self,
        record: RenewalRecord,
        server: CloudServer,
        balance: int,
        deadline: datetime,
        reason: str,
    ) -> None:
        kind = (
            RenewalKind.MANUAL_CANCELLATION
            if reason == "manual_cancellation_required"
            else RenewalKind.ADMIN_INSUFFICIENT
        )
        first = await self._log.record(server.id, kind, _period_of(deadline))
        if not first:
            return
        await self._emit(
            _KIND_EVENT.get(kind, BusinessEventType.SERVICE_OPERATOR_ATTENTION_REQUIRED),
            record,
            server,
            result=reason,
            period=_period_of(deadline),
            grace_until=deadline,
        )
        await self._admin_notifier.alert(
            server_id=server.id,
            renewal_at=deadline,
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
            action=f"renewal.{reason}",
            resource_type="server",
            resource_id=str(server.id),
            reason=f"renewal {reason}: balance {balance} {record.currency} < "
            f"{record.customer_price_minor} {record.currency}",
            metadata={
                "renewal_at": deadline.isoformat(),
                "price_minor": str(record.customer_price_minor),
                "balance_minor": str(balance),
                "currency": record.currency,
            },
        )

    # ------------------------------------------------------------------
    # Business events
    # ------------------------------------------------------------------

    async def _emit(
        self,
        event_type: BusinessEventType,
        record: RenewalRecord,
        server: CloudServer,
        *,
        result: str,
        period: datetime,
        period_end: datetime | None = None,
        grace_until: datetime | None = None,
        days_left: int | None = None,
    ) -> None:
        """Emit one COMMERCIAL event; a broken sink never breaks a settlement.

        The key is deterministic in (server, event, period), so a repeated pass
        or a second worker can only post the card once — the same gate that
        makes the customer notification exactly-once. The event carries the
        LOCAL sale price (integer minor units) and no secret.
        """
        await emit_safe(
            self._events,
            service_renewal_event(
                event_type=event_type,
                event_key_parts=(server.id, event_type.value, period.date().isoformat()),
                user=_OwnerRef(id=server.user_id),
                server_id=server.id,
                provider_key=server.provider_key,
                state=server.state.value,
                result=result,
                amount_minor=record.customer_price_minor,
                currency=record.currency,
                period_end=period_end or record.provider_renewal_at,
                grace_until=grace_until,
                days_left=days_left,
                auto_renew=record.auto_charge_enabled,
                at=self._now(),
            ),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _wallet_balance(self, server: CloudServer) -> int:
        wallet = await self._wallets.get(server.user_id)
        return wallet.balance if wallet is not None else 0
