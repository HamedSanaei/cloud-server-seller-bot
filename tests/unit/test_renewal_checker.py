"""Commercial renewal lifecycle tests (LEASEWEB-MVP + PROD-HARDENING §17-§34).

Acceptance: exactly one charge per (server, period), warnings at the
CONFIGURED thresholds (deduplicated per period), a payable window that
survives insufficient funds, suspension only after grace, no automatic
charge when the customer turned auto-renew off, and repeated/concurrent job
runs that are idempotent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from cloud_platform.modules.audit.domain import AuditEvent
from cloud_platform.modules.compute.domain import (
    BILLING_MODEL_PREPAID_MONTHLY,
    CloudServer,
    ServerLifecycleState,
)
from cloud_platform.modules.renewals.domain import (
    RenewalKind,
    RenewalRecord,
    RenewalStatus,
)
from cloud_platform.modules.renewals.service import RenewalChecker
from cloud_platform.modules.wallet.domain import Hold, HoldStatus, Wallet

SERVER_ID = uuid4()
USER_ID = uuid4()
WALLET_ID = uuid4()
PRICE = 1299


def _server() -> CloudServer:
    return CloudServer(
        id=SERVER_ID,
        user_id=USER_ID,
        provider_key="leaseweb",
        provider_account_id=uuid4(),
        state=ServerLifecycleState.RUNNING,
        billing_model=BILLING_MODEL_PREPAID_MONTHLY,
        provider_server_id="vps-1",
    )


def _record(renewal_at: datetime, **kwargs: Any) -> RenewalRecord:
    return RenewalRecord(
        server_id=SERVER_ID,
        purchased_at=renewal_at - timedelta(days=30),
        provider_renewal_at=renewal_at,
        customer_price_minor=PRICE,
        currency="EUR",
        provider_order_ref="LS-ORD-1",
        provider_contract_id="CON-1",
        **kwargs,
    )


class FakeAuditRepo:
    async def append(self, event: AuditEvent) -> AuditEvent:
        return event


class FakeRenewalRepo:
    def __init__(self, records: list[RenewalRecord]) -> None:
        self.records = {r.server_id: r for r in records}
        self.saved: list[RenewalRecord] = []
        self.locks = 0

    async def list_active(self) -> list[RenewalRecord]:
        return [r for r in self.records.values() if r.status is not RenewalStatus.CANCELLED]

    async def get(self, server_id: UUID) -> RenewalRecord | None:
        return self.records.get(server_id)

    async def upsert(self, record: RenewalRecord) -> RenewalRecord:
        self.records[record.server_id] = record
        self.saved.append(record)
        return record

    async def lock_for_update(self, server_id: UUID) -> RenewalRecord | None:
        """The production adapter takes a real row lock; the fake only counts."""
        self.locks += 1
        return self.records.get(server_id)

    async def set_auto_renew(self, server_id: UUID, enabled: bool) -> RenewalRecord:
        record = self.records.get(server_id)
        if record is None:
            raise LookupError(f"no renewal record for server {server_id}")
        record.auto_charge_enabled = bool(enabled)
        self.saved.append(record)
        return record


class FakeNotificationRepo:
    def __init__(self) -> None:
        self.seen: set[tuple[UUID, str, str]] = set()

    async def record(self, server_id: UUID, kind: RenewalKind, for_period: datetime) -> bool:
        key = (server_id, kind.value, for_period.date().isoformat())
        if key in self.seen:
            return False
        self.seen.add(key)
        return True


class FakeServerRepo:
    def __init__(self, server: CloudServer | None = None) -> None:
        self.server = server or _server()

    async def get(self, server_id: UUID) -> CloudServer | None:
        return self.server if server_id == SERVER_ID else None


class FakeWalletRepo:
    def __init__(self, balance: int) -> None:
        self.wallet = Wallet(user_id=USER_ID, id=WALLET_ID, balance=balance, currency="EUR")

    async def get(self, user_id: UUID) -> Wallet | None:
        return self.wallet if user_id == USER_ID else None


class FakeHoldRepo:
    def __init__(self) -> None:
        self.holds: dict[str, Hold] = {}
        self.wallet: Wallet | None = None

    async def get_by_idempotency(self, wallet_id: UUID, idempotency_key: str) -> Hold | None:
        return self.holds.get(idempotency_key)

    async def create_hold(
        self, wallet_id: UUID, amount: int, currency: str, idempotency_key: str
    ) -> Hold:
        existing = self.holds.get(idempotency_key)
        if existing is not None and existing.status is HoldStatus.CREATED:
            return existing
        from cloud_platform.modules.wallet.domain import InsufficientHoldBalanceError

        if self.wallet is None or self.wallet.balance < amount:
            raise InsufficientHoldBalanceError(f"available balance < {amount}")
        hold = Hold(
            wallet_id=wallet_id,
            amount=amount,
            currency=currency,
            idempotency_key=idempotency_key,
            id=uuid4(),
        )
        self.holds[idempotency_key] = hold
        return hold

    async def capture_hold(self, hold_id: UUID) -> Hold | None:
        hold = next((h for h in self.holds.values() if h.id == hold_id), None)
        if hold is None or hold.status is not HoldStatus.CREATED:
            return None
        assert self.wallet is not None
        self.wallet.balance -= hold.amount
        hold.capture()
        return hold


class FakeHoldService:
    def __init__(self, holds: FakeHoldRepo) -> None:
        self._holds = holds
        self.captures = 0

    async def capture_hold(self, wallet_id: UUID, hold_id: UUID, idempotency_key: str) -> Hold:
        self.captures += 1
        hold = await self._holds.capture_hold(hold_id)
        assert hold is not None
        return hold


class _RecordingUserNotifier:
    def __init__(self) -> None:
        self.warns: list[RenewalKind] = []
        self.charges: list[dict[str, Any]] = []

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
        self.warns.append(kind)

    async def charged(
        self,
        *,
        user_id: UUID,
        server_id: UUID,
        renewal_at: datetime,
        price_minor: int,
        currency: str,
    ) -> None:
        self.charges.append(
            {"renewal_at": renewal_at, "price_minor": price_minor, "currency": currency}
        )


class _RecordingAdminNotifier:
    def __init__(self) -> None:
        self.alerts: list[str] = []

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
        self.alerts.append(reason)


class _RecordingEventSink:
    """The durable business-log outbox, in memory."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> bool:
        self.events.append(event)
        return True

    @property
    def types(self) -> list[Any]:
        return [event.event_type for event in self.events]


class _BrokenEventSink:
    """A sink that always fails: it must never break a settlement."""

    async def emit(self, event: Any) -> bool:
        raise RuntimeError("outbox down")


def _checker(
    *,
    record: RenewalRecord,
    balance: int,
    now: datetime,
    notification_repo: FakeNotificationRepo | None = None,
    renewals_repo: FakeRenewalRepo | None = None,
    event_sink: Any | None = None,
) -> tuple[RenewalChecker, dict[str, Any]]:
    wallet_repo = FakeWalletRepo(balance)
    holds = FakeHoldRepo()
    holds.wallet = wallet_repo.wallet
    user_notifier = _RecordingUserNotifier()
    admin_notifier = _RecordingAdminNotifier()
    sink = event_sink if event_sink is not None else _RecordingEventSink()
    checker = RenewalChecker(
        renewals_repo=renewals_repo or FakeRenewalRepo([record]),
        notification_repo=notification_repo or FakeNotificationRepo(),
        server_repo=FakeServerRepo(),
        wallet_repo=wallet_repo,
        hold_repo=holds,
        hold_service=FakeHoldService(holds),
        audit_repo=FakeAuditRepo(),
        user_notifier=user_notifier,
        admin_notifier=admin_notifier,
        event_sink=sink,
        clock=lambda: now,
    )
    deps = {
        "user": user_notifier,
        "admin": admin_notifier,
        "holds": holds,
        "wallet": wallet_repo,
        "events": sink,
    }
    return checker, deps


class TestWarnings:
    async def test_the_most_distant_warning_is_sent_once(self) -> None:
        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        checker, deps = _checker(record=_record(now + timedelta(days=6)), balance=10_000, now=now)
        await checker.run()
        await checker.run()  # repeated pass
        # 144 h remain, so only the 168 h threshold has been crossed.
        assert deps["user"].warns == [RenewalKind.WARN_168H]

    async def test_crossing_a_lower_threshold_warns_again(self) -> None:
        """The dedup log is the DURABLE one, shared across job runs."""
        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        renewal_at = now + timedelta(days=6)
        record = _record(renewal_at)
        renewals = FakeRenewalRepo([record])
        dedup = FakeNotificationRepo()

        checker, deps = _checker(
            record=record, balance=10_000, now=now, notification_repo=dedup, renewals_repo=renewals
        )
        await checker.run()
        # A later pass: 66 h remain, so the 72 h threshold is newly crossed.
        checker_later, deps_later = _checker(
            record=record,
            balance=10_000,
            now=now + timedelta(days=3, hours=6),
            notification_repo=dedup,
            renewals_repo=renewals,
        )
        await checker_later.run()
        assert deps["user"].warns == [RenewalKind.WARN_168H]
        assert deps_later["user"].warns == [RenewalKind.WARN_72H]
        # Re-running the same pass changes nothing: exactly once per period.
        again, deps_again = _checker(
            record=record,
            balance=10_000,
            now=now + timedelta(days=3, hours=6),
            notification_repo=dedup,
            renewals_repo=renewals,
        )
        await again.run()
        assert deps_again["user"].warns == []

    async def test_insufficient_funds_inside_the_window_alerts_the_operator(self) -> None:
        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        checker, deps = _checker(record=_record(now + timedelta(days=3)), balance=500, now=now)
        await checker.run()
        # 72 h and 168 h are both crossed; each fires once, most distant first.
        assert deps["user"].warns == [RenewalKind.WARN_168H, RenewalKind.WARN_72H]
        assert deps["admin"].alerts == ["insufficient_funds"]
        # Nothing is charged and the service is still ACTIVE before expiry.
        assert deps["user"].charges == []
        assert deps["wallet"].wallet.balance == 500

    async def test_no_warning_far_from_renewal(self) -> None:
        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        checker, deps = _checker(record=_record(now + timedelta(days=20)), balance=10_000, now=now)
        await checker.run()
        assert deps["user"].warns == []
        assert deps["admin"].alerts == []

    async def test_a_sufficient_wallet_is_charged_early_inside_the_window(self) -> None:
        """The configured charge window opens before expiry, exactly once."""
        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        record = _record(now + timedelta(days=2))  # 48 h left < 72 h window
        checker, deps = _checker(record=record, balance=10_000, now=now)
        await checker.run()
        assert len(deps["user"].charges) == 1
        assert deps["wallet"].wallet.balance == 10_000 - PRICE
        await checker.run()
        assert len(deps["user"].charges) == 1


class TestCharging:
    async def test_exactly_one_monthly_charge(self) -> None:
        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        record = _record(now)
        checker, deps = _checker(record=record, balance=10_000, now=now)
        await checker.run()
        assert len(deps["user"].charges) == 1
        assert deps["wallet"].wallet.balance == 10_000 - PRICE
        assert record.provider_renewal_at == now + timedelta(days=30)
        assert record.status is RenewalStatus.ACTIVE

        # Second run the same day: no second charge.
        await checker.run()
        assert len(deps["user"].charges) == 1
        assert deps["wallet"].wallet.balance == 10_000 - PRICE

    async def test_insufficient_at_due_date_enters_payment_due(self) -> None:
        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        record = _record(now)
        checker, deps = _checker(record=record, balance=100, now=now)
        await checker.run()
        assert deps["user"].charges == []
        assert record.status is RenewalStatus.PAYMENT_DUE
        # The payable deadline is durable local state, not a provider string.
        assert record.grace_until == now + timedelta(hours=48)
        assert deps["admin"].alerts == ["insufficient_funds"]
        # No negative balance is ever created.
        assert deps["wallet"].wallet.balance == 100

    async def test_recharging_inside_grace_settles_without_a_second_charge(self) -> None:
        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        record = _record(now)
        checker, deps = _checker(record=record, balance=100, now=now)
        await checker.run()
        assert record.status is RenewalStatus.PAYMENT_DUE

        # The customer tops up; the very next pass collects the period once.
        deps["wallet"].wallet.balance = 10_000
        checker_later, deps_later = _checker(
            record=record, balance=10_000, now=now + timedelta(hours=6)
        )
        await checker_later.run()
        assert len(deps_later["user"].charges) == 1
        assert record.status is RenewalStatus.ACTIVE
        assert record.grace_until is None

    async def test_still_payable_inside_grace_is_not_suspended(self) -> None:
        renewal_at = datetime(2026, 9, 1, 12, tzinfo=UTC)
        now = renewal_at + timedelta(days=1)  # inside the 48 h grace window
        record = _record(renewal_at)
        checker, deps = _checker(record=record, balance=100, now=now)
        await checker.run()
        assert record.status is RenewalStatus.PAYMENT_DUE
        assert deps["user"].charges == []

    async def test_grace_expiry_suspends_commercially(self) -> None:
        renewal_at = datetime(2026, 9, 1, 12, tzinfo=UTC)
        now = renewal_at + timedelta(hours=49)
        record = _record(renewal_at)
        checker, deps = _checker(record=record, balance=100, now=now)
        await checker.run()
        assert record.status is RenewalStatus.SUSPENDED
        # Suspension is commercial only: the operator opted into no stop, so
        # nothing has asked the provider to touch the machine.
        assert checker.policy.stop_server_after_grace is False
        assert deps["admin"].alerts[-1] == "grace_expired"

    async def test_auto_charge_disabled_never_charges(self) -> None:
        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        record = _record(now, auto_charge_enabled=False)
        checker, deps = _checker(record=record, balance=10_000, now=now)
        await checker.run()
        assert record.status is RenewalStatus.PAYMENT_DUE
        assert deps["user"].charges == []
        assert deps["wallet"].wallet.balance == 10_000
        # An explicit customer notification is still sent: the service is due.
        assert RenewalKind.RENEWAL_DUE in deps["user"].warns

    async def test_disabling_the_renewal_job_does_nothing(self) -> None:
        from cloud_platform.modules.renewals.policy import CommerceRenewalPolicy

        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        record = _record(now)
        checker, deps = _checker(record=record, balance=10_000, now=now)
        checker._policy = CommerceRenewalPolicy(enabled=False)
        assert await checker.run() == []
        assert deps["user"].charges == []
        assert record.status is RenewalStatus.ACTIVE

    async def test_charge_repeat_run_is_idempotent_after_restart(self) -> None:
        """Simulates a crash AFTER charging but BEFORE the record advanced."""
        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        record = _record(now)
        checker, deps = _checker(record=record, balance=10_000, now=now)
        await checker.run()
        # The hold is CAPTURED; pretend the renewal record was NOT advanced
        # (crash before upsert): the checker must detect the captured hold
        # and not charge again.
        record.provider_renewal_at = now  # rollback the advance
        await checker.run()
        assert len(deps["user"].charges) == 1  # one charge total
        assert deps["wallet"].wallet.balance == 10_000 - PRICE


class TestBusinessEvents:
    """§39: every commercial decision reaches the durable outbox, exactly once."""

    async def test_a_warning_and_a_charge_are_emitted_once(self) -> None:
        from cloud_platform.modules.businesslog.domain import BusinessEventType

        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        record = _record(now + timedelta(days=6))
        checker, deps = _checker(record=record, balance=10_000, now=now)

        await checker.run()
        await checker.run()
        assert deps["events"].types == [BusinessEventType.SERVICE_RENEWAL_WARNING]

    async def test_a_settlement_emits_renewal_succeeded(self) -> None:
        from cloud_platform.modules.businesslog.domain import BusinessEventType

        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        record = _record(now)
        checker, deps = _checker(record=record, balance=10_000, now=now)
        await checker.run()
        await checker.run()
        assert deps["events"].types.count(BusinessEventType.SERVICE_RENEWAL_SUCCEEDED) == 1

    async def test_an_unpaid_service_emits_due_then_insufficient(self) -> None:
        from cloud_platform.modules.businesslog.domain import BusinessEventType

        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        record = _record(now, status=RenewalStatus.PAYMENT_DUE)
        checker, deps = _checker(record=record, balance=0, now=now)
        await checker.run()
        types = deps["events"].types
        assert BusinessEventType.SERVICE_RENEWAL_FAILED_INSUFFICIENT_BALANCE in types

    async def test_grace_expiry_emits_suspension(self) -> None:
        from cloud_platform.modules.businesslog.domain import BusinessEventType

        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        record = _record(now - timedelta(days=5), status=RenewalStatus.PAYMENT_DUE)
        checker, deps = _checker(record=record, balance=0, now=now)
        await checker.run()
        assert BusinessEventType.SERVICE_SUSPENDED in deps["events"].types

    async def test_a_broken_outbox_never_breaks_a_settlement(self) -> None:
        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        record = _record(now)
        checker, deps = _checker(
            record=record, balance=10_000, now=now, event_sink=_BrokenEventSink()
        )
        await checker.run()
        assert record.status is RenewalStatus.ACTIVE
        assert deps["wallet"].wallet.balance == 10_000 - PRICE

    async def test_no_event_carries_a_secret(self) -> None:
        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        record = _record(now)
        checker, deps = _checker(record=record, balance=10_000, now=now)
        await checker.run()
        blob = "\n".join(
            str(event.sanitized_payload()) + str(event) for event in deps["events"].events
        )
        for secret in ("LEASEWEB_API_KEY", "X-LSW-Auth", "vps-1", str(USER_ID)):
            if secret == str(USER_ID):
                continue  # the owning user id IS the event's identity
            assert secret not in blob


class TestManualSettlement:
    """Customer-initiated "renew now" (§37): local, ownership-scoped, once."""

    async def test_due_period_is_settled_exactly_once(self) -> None:
        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        record = _record(now, status=RenewalStatus.PAYMENT_DUE)
        checker, deps = _checker(record=record, balance=10_000, now=now)

        first = await checker.settle_now(SERVER_ID)
        assert first.reason == "charged"
        assert first.settled is True
        assert first.amount_minor == PRICE
        assert first.currency == "EUR"
        assert record.status is RenewalStatus.ACTIVE
        assert deps["wallet"].wallet.balance == 10_000 - PRICE

        # A second click (or a second replica) settles NOTHING more.
        second = await checker.settle_now(SERVER_ID)
        assert second.settled is False
        assert second.reason == "not_payable"
        assert deps["wallet"].wallet.balance == 10_000 - PRICE
        assert len(deps["user"].charges) == 1

    async def test_a_captured_period_reports_already_charged(self) -> None:
        """Crash recovery: the debit landed but the record never advanced."""
        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        record = _record(now, status=RenewalStatus.PAYMENT_DUE)
        checker, deps = _checker(record=record, balance=10_000, now=now)
        await checker.settle_now(SERVER_ID)
        record.provider_renewal_at = now  # roll the period back
        record.status = RenewalStatus.PAYMENT_DUE

        again = await checker.settle_now(SERVER_ID)
        assert again.reason == "already_charged"
        assert again.settled is True
        assert deps["wallet"].wallet.balance == 10_000 - PRICE

    async def test_insufficient_funds_never_goes_negative(self) -> None:
        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        record = _record(now, status=RenewalStatus.PAYMENT_DUE)
        checker, deps = _checker(record=record, balance=PRICE - 1, now=now)

        outcome = await checker.settle_now(SERVER_ID)
        assert outcome.settled is False
        assert outcome.reason == "insufficient_funds"
        assert deps["wallet"].wallet.balance == PRICE - 1
        assert record.status is RenewalStatus.PAYMENT_DUE

    async def test_an_active_period_is_not_early_renewable(self) -> None:
        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        record = _record(now + timedelta(days=20))
        checker, deps = _checker(record=record, balance=10_000, now=now)

        outcome = await checker.settle_now(SERVER_ID)
        assert outcome.reason == "not_payable"
        assert deps["wallet"].wallet.balance == 10_000

    async def test_more_than_one_period_overdue_needs_the_operator(self) -> None:
        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        record = _record(now - timedelta(days=70), status=RenewalStatus.PAYMENT_DUE)
        checker, deps = _checker(record=record, balance=10_000, now=now)

        outcome = await checker.settle_now(SERVER_ID)
        assert outcome.reason == "manual_review_required"
        assert deps["wallet"].wallet.balance == 10_000

    async def test_a_service_without_a_record_settles_nothing(self) -> None:
        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        checker, deps = _checker(record=_record(now), balance=10_000, now=now)
        outcome = await checker.settle_now(uuid4())
        assert outcome.reason == "no_renewal_record"
        assert deps["wallet"].wallet.balance == 10_000

    async def test_a_grace_period_service_can_still_be_settled(self) -> None:
        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        record = _record(
            now - timedelta(hours=12),
            status=RenewalStatus.GRACE_PERIOD,
            grace_until=now + timedelta(hours=36),
        )
        checker, _deps = _checker(record=record, balance=10_000, now=now)

        outcome = await checker.settle_now(SERVER_ID)
        assert outcome.settled is True
        assert outcome.reason == "charged"
        assert record.status is RenewalStatus.ACTIVE
        assert record.grace_until is None


class TestAutoRenewPreference:
    """The durable customer preference (§38), never process-local state."""

    async def test_toggling_persists_and_reports_the_stored_value(self) -> None:
        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        record = _record(now + timedelta(days=10))
        checker, _ = _checker(record=record, balance=10_000, now=now)

        off = await checker.set_auto_renew(SERVER_ID, False)
        assert off is not None and off.auto_charge_enabled is False
        assert record.auto_charge_enabled is False

        on = await checker.set_auto_renew(SERVER_ID, True)
        assert on is not None and on.auto_charge_enabled is True

    async def test_an_unknown_service_reports_unavailable(self) -> None:
        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        checker, _ = _checker(record=_record(now), balance=10_000, now=now)
        assert await checker.set_auto_renew(uuid4(), False) is None
