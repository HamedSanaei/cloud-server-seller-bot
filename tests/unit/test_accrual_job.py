"""Tests for the periodic usage accrual job (M06-005).

Acceptance: no duplicate charges on retry.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cloud_platform.core.money import Money
from cloud_platform.modules.billing.service import (
    AccrualJob,
    AccrualPeriod,
    AccrualPeriodExistsError,
    AccrualRunReport,
    MissingSnapshotError,
    accrual_charge_key,
)
from cloud_platform.modules.compute.domain import CloudServer, ServerLifecycleState
from cloud_platform.modules.pricing.domain import (
    MarginRule,
    OfferCost,
    ServerPriceSnapshot,
)
from cloud_platform.modules.wallet.domain import (
    Hold,
    HoldStatus,
    InsufficientBalanceError,
    LedgerEntry,
    LedgerEntryType,
    Wallet,
)

T0 = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
QUANTUM = 3600
SELLING = 1000
COST = 700
SERVER_ID = uuid4()
USER_ID = uuid4()
WALLET_ID = uuid4()
IK = "order-1"
HOLD_KEY = f"server-create:{IK}"
CAPTURE_KEY = f"capture-server-create:{IK}"
P0_KEY = accrual_charge_key(SERVER_ID, T0)
P1_KEY = accrual_charge_key(SERVER_ID, T0 + timedelta(hours=1))
P2_KEY = accrual_charge_key(SERVER_ID, T0 + timedelta(hours=2))


def _money(amount: int) -> Money:
    return Money(Decimal(amount), "EUR")


def _server(**kw: object) -> CloudServer:
    defaults: dict[str, object] = dict(
        id=SERVER_ID,
        user_id=USER_ID,
        provider_key="hetzner",
        provider_account_id=uuid4(),
        state=ServerLifecycleState.RUNNING,
        idempotency_key=IK,
        created_at=T0,
        quantum_seconds=QUANTUM,
    )
    defaults.update(kw)
    return CloudServer(**defaults)  # type: ignore[arg-type]


def _snapshot() -> ServerPriceSnapshot:
    return ServerPriceSnapshot(
        server_id=SERVER_ID,
        offer=OfferCost(
            provider_key="hetzner",
            plan_id="cx22",
            location_id="fsn1",
            cost_minor=COST,
            currency="EUR",
        ),
        selling_minor=SELLING,
        book_name="retail-eur",
        book_version=1,
        rule=MarginRule(
            provider="hetzner",
            plan="cx22",
            location="fsn1",
            margin_factor=Decimal("1.43"),
        ),
        priced_at=T0,
    )


def _hold(key: str, status: HoldStatus = HoldStatus.CREATED, amount: int = SELLING) -> Hold:
    return Hold(
        wallet_id=WALLET_ID,
        amount=amount,
        currency="EUR",
        idempotency_key=key,
        id=uuid4(),
        status=status,
    )


class Harness:
    """All ports faked against one shared state; counters expose double charges."""

    def __init__(
        self,
        servers: list[CloudServer] | None = None,
        wallet: Wallet | None = None,
        holds: dict[str, Hold] | None = None,
        snapshots: dict | None = None,
        lock_acquired: bool = True,
    ) -> None:
        self.servers = list(servers or [])
        self.wallet = wallet or Wallet(USER_ID, id=WALLET_ID, balance=100_000)
        self.holds = dict(holds or {})
        self.snapshots = dict(snapshots if snapshots is not None else {SERVER_ID: _snapshot()})
        self.saved: list[CloudServer] = []
        self.debit_calls: list[tuple[int, str]] = []
        self.captured: list[str] = []
        self.entries: dict[str, LedgerEntry] = {}
        self.accrual_rows: dict[str, AccrualPeriod] = {}
        self.lock_acquired = lock_acquired
        self.lock_used = False
        self.audit = AsyncMock()

    # -- shared money posting (mirrors the DB's unique key: duplicates explode) --
    def post(self, amount: int, key: str, etype: LedgerEntryType, reference: str = "") -> None:
        if key in self.entries:
            raise AssertionError(f"duplicate ledger key {key}")
        self.entries[key] = LedgerEntry(
            id=uuid4(),
            wallet_id=WALLET_ID,
            entry_type=etype,
            amount=_money(amount),
            reference_type="hold" if etype is LedgerEntryType.CHARGE else "server",
            reference_id=reference,
            idempotency_key=key,
        )

    # -- ServerRepository -------------------------------------------------------
    async def list_running(self) -> list[CloudServer]:
        return list(self.servers)

    async def save(self, server: CloudServer) -> CloudServer:
        self.saved.append(server)
        return server

    # -- WalletRepository ---------------------------------------------------------
    async def wallet_get(self, user_id) -> Wallet | None:
        return self.wallet

    async def debit(self, user_id, amount: int, key: str) -> Wallet:
        self.debit_calls.append((amount, key))
        if self.wallet.balance < amount:
            raise InsufficientBalanceError(f"balance {self.wallet.balance} < {amount}")
        self.wallet.balance -= amount
        return self.wallet

    # -- HoldRepository -----------------------------------------------------------
    async def hold_get(self, wallet_id, key: str) -> Hold | None:
        return self.holds.get(key)

    # -- HoldService ----------------------------------------------------------------
    async def capture_hold(self, wallet_id, hold_id, key: str) -> Hold:
        hold = self.holds[key]
        if hold.status is not HoldStatus.CREATED:
            raise ValueError("cannot capture")
        hold.capture()
        self.captured.append(key)
        self.wallet.balance -= hold.amount
        self.post(hold.amount, f"capture-{key}", LedgerEntryType.CHARGE, reference=key)
        return hold

    # -- LedgerRepository -------------------------------------------------------------
    async def ledger_get(self, wallet_id, key: str) -> LedgerEntry | None:
        return self.entries.get(key)

    async def ledger_post(
        self, wallet_id, amount: int, currency: str, etype: LedgerEntryType, key: str, **kw: object
    ) -> LedgerEntry:
        self.post(amount, key, etype, reference=str(kw.get("reference_id", "")))
        return self.entries[key]

    # -- AccrualPeriodRepository -------------------------------------------------------
    async def accrual_get(self, key: str) -> AccrualPeriod | None:
        return self.accrual_rows.get(key)

    async def accrual_add(self, period: AccrualPeriod) -> AccrualPeriod:
        if period.idempotency_key in self.accrual_rows:
            raise AccrualPeriodExistsError(period.idempotency_key)
        self.accrual_rows[period.idempotency_key] = period
        return period

    async def accrual_between(self, start: datetime, end: datetime) -> list[AccrualPeriod]:
        return [p for p in self.accrual_rows.values() if start <= p.period_start < end]

    # -- ServerPriceSnapshotRepository ---------------------------------------------------
    async def snapshot_get(self, server_id) -> ServerPriceSnapshot | None:
        return self.snapshots.get(server_id)

    # -- JobLock ------------------------------------------------------------------------
    @asynccontextmanager
    async def lock_guard(self):
        self.lock_used = True
        yield self.lock_acquired

    # -- wiring ------------------------------------------------------------------------
    @dataclass
    class _WalletRepo:
        h: Harness

        async def get(self, user_id):
            return await self.h.wallet_get(user_id)

        async def debit(self, user_id, amount, key):
            return await self.h.debit(user_id, amount, key)

    @dataclass
    class _HoldRepo:
        h: Harness

        async def get_by_idempotency(self, wallet_id, key):
            return await self.h.hold_get(wallet_id, key)

    @dataclass
    class _HoldService:
        h: Harness

        async def capture_hold(self, wallet_id, hold_id, key):
            return await self.h.capture_hold(wallet_id, hold_id, key)

    @dataclass
    class _LedgerRepo:
        h: Harness

        async def get_entry_by_idempotency(self, wallet_id, key):
            return await self.h.ledger_get(wallet_id, key)

        async def post_entry(self, wallet_id, amount, currency, etype, key, **kw):
            return await self.h.ledger_post(wallet_id, amount, currency, etype, key, **kw)

    @dataclass
    class _AccrualRepo:
        h: Harness

        async def get_by_key(self, key):
            return await self.h.accrual_get(key)

        async def add(self, period):
            return await self.h.accrual_add(period)

        async def list_between(self, start, end):
            return await self.h.accrual_between(start, end)

    @dataclass
    class _SnapshotRepo:
        h: Harness

        async def get(self, server_id):
            return await self.h.snapshot_get(server_id)

    @dataclass
    class _Lock:
        h: Harness

        @asynccontextmanager
        async def guard(self):
            async with self.h.lock_guard() as acquired:
                yield acquired

    def make_job(self) -> AccrualJob:
        return AccrualJob(
            server_repo=self,
            wallet_repo=self._WalletRepo(self),
            hold_repo=self._HoldRepo(self),
            hold_service=self._HoldService(self),
            ledger_repo=self._LedgerRepo(self),
            accrual_repo=self._AccrualRepo(self),
            snapshot_repo=self._SnapshotRepo(self),
            audit_repo=self.audit,
            lock=self._Lock(self),
        )


class NoWalletRepo:
    async def get(self, user_id):
        return None

    async def debit(self, user_id, amount, key):
        raise AssertionError("must not debit without a wallet")


class TestFirstPeriodViaHoldCapture:
    async def test_first_period_captures_creation_hold(self) -> None:
        h = Harness(
            servers=[_server()],
            wallet=Wallet(USER_ID, id=WALLET_ID, balance=10_000),
            holds={HOLD_KEY: _hold(HOLD_KEY)},
        )
        report = await h.make_job().run(now=T0 + timedelta(hours=1))

        assert report.servers_checked == 1
        assert report.periods_posted == 1
        assert report.charged_minor == SELLING
        # the hold, not a fresh debit, consumed the reserved funds
        assert h.captured == [HOLD_KEY]
        assert h.debit_calls == []
        assert h.wallet.balance == 10_000 - SELLING
        assert CAPTURE_KEY in h.entries
        row = h.accrual_rows[CAPTURE_KEY]
        assert row.selling_minor == SELLING and row.cost_minor == COST
        assert row.period_start == T0 and row.period_end == T0 + timedelta(hours=1)
        assert h.servers[0].last_accrued_at == T0 + timedelta(hours=1)

    async def test_partial_period_not_settled(self) -> None:
        h = Harness(servers=[_server()], holds={HOLD_KEY: _hold(HOLD_KEY)})
        report = await h.make_job().run(now=T0 + timedelta(minutes=59))

        assert report.periods_posted == 0
        assert h.servers[0].last_accrued_at is None
        assert h.entries == {}
        assert h.captured == []


class TestLaterPeriods:
    async def test_second_period_is_a_plain_debit(self) -> None:
        h = Harness(servers=[_server(last_accrued_at=T0 + timedelta(hours=1))])
        report = await h.make_job().run(now=T0 + timedelta(hours=2))

        assert report.periods_posted == 1
        assert h.debit_calls == [(SELLING, P1_KEY)]
        assert h.entries[P1_KEY].entry_type is LedgerEntryType.CHARGE
        assert h.wallet.balance == 100_000 - SELLING
        assert h.servers[0].last_accrued_at == T0 + timedelta(hours=2)
        assert h.accrual_rows[P1_KEY].period_start == T0 + timedelta(hours=1)

    async def test_multiple_periods_in_one_run(self) -> None:
        h = Harness(servers=[_server(last_accrued_at=T0)])
        report = await h.make_job().run(now=T0 + timedelta(hours=3))

        assert report.periods_posted == 3
        assert h.wallet.balance == 100_000 - 3 * SELLING
        assert h.servers[0].last_accrued_at == T0 + timedelta(hours=3)
        # no hold present -> all three periods settle as plain debits
        assert h.debit_calls == [(SELLING, P0_KEY), (SELLING, P1_KEY), (SELLING, P2_KEY)]


class TestNoDuplicateChargesOnRetry:
    async def test_replay_after_watermark_reset_does_not_double_charge(self) -> None:
        """Crash before the watermark save: the ledger must absorb the retry."""
        h = Harness(
            servers=[_server()],
            wallet=Wallet(USER_ID, id=WALLET_ID, balance=10_000),
            holds={HOLD_KEY: _hold(HOLD_KEY)},
        )
        first = await h.make_job().run(now=T0 + timedelta(hours=1))
        assert first.periods_posted == 1
        balance_after_first = h.wallet.balance
        entries_after_first = len(h.entries)

        h.servers[0].last_accrued_at = None  # the crash lost the watermark

        second = await h.make_job().run(now=T0 + timedelta(hours=1))

        assert second.periods_posted == 0
        assert second.periods_replayed == 1
        assert h.wallet.balance == balance_after_first  # no second debit/capture
        assert len(h.entries) == entries_after_first  # no duplicate ledger rows
        assert h.servers[0].last_accrued_at == T0 + timedelta(hours=1)  # restored

    async def test_run_with_no_elapsed_time_is_a_noop(self) -> None:
        h = Harness(
            servers=[_server(last_accrued_at=T0 + timedelta(hours=1))],
            holds={HOLD_KEY: _hold(HOLD_KEY)},
        )
        report = await h.make_job().run(now=T0 + timedelta(hours=1, minutes=30))

        assert report.periods_posted == 0
        assert h.debit_calls == []
        assert h.captured == []
        assert h.saved == []

    async def test_replayed_period_does_not_repeat_accrual_row(self) -> None:
        h = Harness(servers=[_server(last_accrued_at=T0 + timedelta(hours=1))])
        job = h.make_job()

        await job.run(now=T0 + timedelta(hours=2))
        rows_after_first = len(h.accrual_rows)

        h.servers[0].last_accrued_at = T0 + timedelta(hours=1)  # crash rollback
        await job.run(now=T0 + timedelta(hours=2))

        assert len(h.accrual_rows) == rows_after_first  # unique key held


class TestHoldEdgeCases:
    async def test_released_hold_falls_back_to_debit(self) -> None:
        h = Harness(
            servers=[_server()],
            holds={HOLD_KEY: _hold(HOLD_KEY, status=HoldStatus.RELEASED)},
        )
        report = await h.make_job().run(now=T0 + timedelta(hours=1))

        assert report.periods_posted == 1
        assert h.captured == []
        assert h.debit_calls == [(SELLING, P0_KEY)]

    async def test_capture_replay_when_hold_already_captured(self) -> None:
        h = Harness(
            servers=[_server()],
            holds={HOLD_KEY: _hold(HOLD_KEY, status=HoldStatus.CAPTURED)},
        )
        h.post(SELLING, CAPTURE_KEY, LedgerEntryType.CHARGE, reference=HOLD_KEY)
        report = await h.make_job().run(now=T0 + timedelta(hours=1))

        assert report.periods_posted == 0
        assert report.periods_replayed == 1
        assert h.wallet.balance == 100_000  # nothing moved on replay
        assert h.servers[0].last_accrued_at == T0 + timedelta(hours=1)


class TestFailureModes:
    async def test_insufficient_balance_leaves_period_unsettled(self) -> None:
        h = Harness(
            servers=[_server()],
            wallet=Wallet(USER_ID, id=WALLET_ID, balance=500),
        )
        report = await h.make_job().run(now=T0 + timedelta(hours=1))

        assert report.insufficient_balance == 1
        assert report.periods_posted == 0
        assert h.entries == {}
        assert h.servers[0].last_accrued_at is None  # retried next run
        assert h.saved == []

    async def test_one_bad_server_does_not_break_the_run(self) -> None:
        other_id = uuid4()
        other = _server(
            id=other_id,
            user_id=uuid4(),
            idempotency_key=None,
            created_at=T0,
        )
        h = Harness(
            servers=[_server(), other],
            holds={HOLD_KEY: _hold(HOLD_KEY)},
            snapshots={SERVER_ID: _snapshot()},  # the other server has no snapshot
        )
        report = await h.make_job().run(now=T0 + timedelta(hours=1))

        assert report.servers_checked == 2
        assert report.periods_posted == 1  # the healthy server was billed
        assert report.errors == 1  # the snapshot-less server counted, not raised

    async def test_missing_wallet_counts_as_error(self) -> None:
        h = Harness(servers=[_server()])
        job = AccrualJob(
            server_repo=h,
            wallet_repo=NoWalletRepo(),
            hold_repo=h._HoldRepo(h),
            hold_service=h._HoldService(h),
            ledger_repo=h._LedgerRepo(h),
            accrual_repo=h._AccrualRepo(h),
            snapshot_repo=h._SnapshotRepo(h),
            audit_repo=h.audit,
        )
        report = await job.run(now=T0 + timedelta(hours=1))
        assert report.errors == 1
        assert report.periods_posted == 0


class TestLock:
    async def test_lock_not_acquired_skips_entirely(self) -> None:
        h = Harness(servers=[_server()], lock_acquired=False)
        report = await h.make_job().run(now=T0 + timedelta(hours=1))

        assert report == AccrualRunReport()
        assert h.lock_used is True
        assert h.debit_calls == []
        assert h.saved == []

    async def test_lock_acquired_proceeds(self) -> None:
        h = Harness(servers=[_server()], holds={HOLD_KEY: _hold(HOLD_KEY)})
        report = await h.make_job().run(now=T0 + timedelta(hours=1))

        assert h.lock_used is True
        assert report.periods_posted == 1


class TestUnitBehavior:
    def test_charge_key_is_deterministic_and_utc_based(self) -> None:
        key = accrual_charge_key(SERVER_ID, T0 + timedelta(hours=1))
        assert key == f"accrual:{SERVER_ID}:{int((T0 + timedelta(hours=1)).timestamp())}"
        # same instant, different offset representation -> same key
        tehran = timezone(timedelta(hours=3, minutes=30))
        other = (T0 + timedelta(hours=1)).astimezone(tehran)
        assert accrual_charge_key(SERVER_ID, other) == key

    async def test_missing_created_at_counts_as_error(self) -> None:
        h = Harness(servers=[_server(created_at=None)])
        h.servers[0].created_at = None
        report = await h.make_job().run(now=T0 + timedelta(hours=1))
        assert report.errors == 1
        assert report.periods_posted == 0

    def test_accrual_period_validation(self) -> None:
        with pytest.raises(ValueError):
            AccrualPeriod(
                server_id=SERVER_ID,
                wallet_id=WALLET_ID,
                period_start=T0,
                period_end=T0 + timedelta(hours=1),
                selling_minor=0,
                idempotency_key="x",
            )
        with pytest.raises(ValueError):
            AccrualPeriod(
                server_id=SERVER_ID,
                wallet_id=WALLET_ID,
                period_start=T0 + timedelta(hours=1),
                period_end=T0,
                selling_minor=1,
                idempotency_key="x",
            )

    def test_report_render_ascii(self) -> None:
        report = AccrualRunReport(
            servers_checked=3,
            periods_posted=5,
            periods_replayed=1,
            insufficient_balance=2,
            errors=0,
            charged_minor=5000,
            currency="EUR",
        )
        text = report.render()
        text.encode("ascii")  # raises when non-ASCII
        assert "charged=5000EUR" in text

    async def test_corrupted_watermark_rebases_to_start(self) -> None:
        h = Harness(servers=[_server(last_accrued_at=T0 - timedelta(hours=5))])
        report = await h.make_job().run(now=T0 + timedelta(hours=1))

        assert report.periods_posted == 1
        assert h.servers[0].last_accrued_at == T0 + timedelta(hours=1)

    async def test_audit_event_recorded_per_run(self) -> None:
        h = Harness(servers=[_server()])
        await h.make_job().run(now=T0 + timedelta(hours=1))

        event = h.audit.append.call_args.args[0]
        assert event.action == "billing.accrual"
        assert event.metadata["periods_posted"] == "1"

    async def test_missing_snapshot_raises_typed_error(self) -> None:
        h = Harness(servers=[_server()])
        h.snapshots = {}
        job = h.make_job()
        with pytest.raises(MissingSnapshotError):
            await job._accrue_server(h.servers[0], T0 + timedelta(hours=1), AccrualRunReport())
