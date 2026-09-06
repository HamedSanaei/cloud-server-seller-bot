"""Renewal checker tests (LEASEWEB-MVP).

Acceptance: exactly one monthly charge per (server, period), warnings at
7/3/1 days (deduplicated), insufficient funds flagged + admin-alerted,
unpaid services eventually demand manual cancellation, and repeated job
runs are idempotent.
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

    async def list_active(self) -> list[RenewalRecord]:
        return [r for r in self.records.values() if r.status is not RenewalStatus.CANCELLED]

    async def get(self, server_id: UUID) -> RenewalRecord | None:
        return self.records.get(server_id)

    async def upsert(self, record: RenewalRecord) -> RenewalRecord:
        self.records[record.server_id] = record
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


def _checker(
    *,
    record: RenewalRecord,
    balance: int,
    now: datetime,
) -> tuple[RenewalChecker, dict[str, Any]]:
    wallet_repo = FakeWalletRepo(balance)
    holds = FakeHoldRepo()
    holds.wallet = wallet_repo.wallet
    user_notifier = _RecordingUserNotifier()
    admin_notifier = _RecordingAdminNotifier()
    checker = RenewalChecker(
        renewals_repo=FakeRenewalRepo([record]),
        notification_repo=FakeNotificationRepo(),
        server_repo=FakeServerRepo(),
        wallet_repo=wallet_repo,
        hold_repo=holds,
        hold_service=FakeHoldService(holds),
        audit_repo=FakeAuditRepo(),
        user_notifier=user_notifier,
        admin_notifier=admin_notifier,
        clock=lambda: now,
    )
    deps = {
        "user": user_notifier,
        "admin": admin_notifier,
        "holds": holds,
        "wallet": wallet_repo,
    }
    return checker, deps


class TestWarnings:
    async def test_7_day_warning_sent_once(self) -> None:
        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        checker, deps = _checker(record=_record(now + timedelta(days=6)), balance=10_000, now=now)
        await checker.run()
        await checker.run()  # repeated pass
        assert deps["user"].warns == [RenewalKind.WARN_7D]

    async def test_3_day_warning_flags_insufficient_funds(self) -> None:
        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        checker, deps = _checker(record=_record(now + timedelta(days=3)), balance=500, now=now)
        await checker.run()
        assert deps["user"].warns == [RenewalKind.WARN_3D]
        assert deps["admin"].alerts == []  # admin alert only at 1 day

    async def test_1_day_warning_alerts_admin_when_insufficient(self) -> None:
        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        checker, deps = _checker(record=_record(now + timedelta(days=1)), balance=500, now=now)
        await checker.run()
        assert deps["user"].warns == [RenewalKind.WARN_1D]
        assert deps["admin"].alerts == ["insufficient_funds"]

    async def test_no_warning_far_from_renewal(self) -> None:
        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        checker, deps = _checker(record=_record(now + timedelta(days=20)), balance=10_000, now=now)
        await checker.run()
        assert deps["user"].warns == []


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

    async def test_insufficient_at_due_date_flags_and_alerts(self) -> None:
        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        record = _record(now)
        checker, deps = _checker(record=record, balance=100, now=now)
        await checker.run()
        assert deps["user"].charges == []
        assert record.status is RenewalStatus.INSUFFICIENT_FUNDS
        assert deps["admin"].alerts == ["insufficient_funds"]

    async def test_unpaid_after_grace_demands_manual_cancellation(self) -> None:
        now = datetime(2026, 9, 2, 12, tzinfo=UTC)  # renewal + 1 day (grace passed)
        record = _record(datetime(2026, 9, 1, 12, tzinfo=UTC))
        checker, deps = _checker(record=record, balance=100, now=now)
        await checker.run()
        assert record.status is RenewalStatus.MANUAL_CANCELLATION_REQUIRED
        assert "manual_cancellation_required" in deps["admin"].alerts

    async def test_auto_charge_disabled_demands_manual_cancellation(self) -> None:
        now = datetime(2026, 9, 1, 12, tzinfo=UTC)
        record = _record(now, auto_charge_enabled=False)
        checker, deps = _checker(record=record, balance=10_000, now=now)
        await checker.run()
        assert record.status is RenewalStatus.MANUAL_CANCELLATION_REQUIRED
        assert deps["user"].charges == []

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
