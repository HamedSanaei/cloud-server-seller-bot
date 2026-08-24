"""Tests for the final deletion charge (M06-006).

Acceptance: final segment posted once after confirmed deletion.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cloud_platform.core.money import Money
from cloud_platform.modules.billing.service import (
    FinalChargeService,
    MissingSnapshotError,
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
FINAL_KEY = f"final:{SERVER_ID}"


def _money(amount: int) -> Money:
    return Money(Decimal(amount), "EUR")


def _deleted_server(**kw: object) -> CloudServer:
    defaults: dict[str, object] = dict(
        id=SERVER_ID,
        user_id=USER_ID,
        provider_key="hetzner",
        provider_account_id=uuid4(),
        state=ServerLifecycleState.DELETED,
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


class Fakes:
    """Port fakes with shared state; duplicate ledger keys raise like the DB."""

    def __init__(
        self,
        wallet: Wallet | None = None,
        holds: dict[str, Hold] | None = None,
        snapshots: dict | None = None,
    ) -> None:
        self.wallet = wallet or Wallet(USER_ID, id=WALLET_ID, balance=100_000)
        self.holds = dict(holds or {})
        self.snapshots = dict(snapshots if snapshots is not None else {SERVER_ID: _snapshot()})
        self.saved: list[CloudServer] = []
        self.debit_calls: list[tuple[int, str]] = []
        self.captured: list[str] = []
        self.entries: dict[str, LedgerEntry] = {}
        self.accrual_rows: dict[str, object] = {}
        self.audit = AsyncMock()

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

    async def save(self, server: CloudServer) -> CloudServer:
        self.saved.append(server)
        return server

    async def wallet_get(self, user_id) -> Wallet | None:
        return self.wallet

    async def debit(self, user_id, amount: int, key: str) -> Wallet:
        self.debit_calls.append((amount, key))
        if self.wallet.balance < amount:
            raise InsufficientBalanceError(f"balance {self.wallet.balance} < {amount}")
        self.wallet.balance -= amount
        return self.wallet

    async def hold_get(self, wallet_id, key: str) -> Hold | None:
        return self.holds.get(key)

    async def capture_hold(self, wallet_id, hold_id, key: str) -> Hold:
        hold = self.holds[key]
        if hold.status is not HoldStatus.CREATED:
            raise ValueError("cannot capture")
        hold.capture()
        self.captured.append(key)
        self.wallet.balance -= hold.amount
        self.post(hold.amount, f"capture-{key}", LedgerEntryType.CHARGE, reference=key)
        return hold

    async def ledger_get(self, wallet_id, key: str) -> LedgerEntry | None:
        return self.entries.get(key)

    async def ledger_post(
        self, wallet_id, amount: int, currency: str, etype: LedgerEntryType, key: str, **kw: object
    ) -> LedgerEntry:
        self.post(amount, key, etype, reference=str(kw.get("reference_id", "")))
        return self.entries[key]

    async def accrual_get(self, key: str):
        return self.accrual_rows.get(key)

    async def accrual_add(self, period) -> object:
        if period.idempotency_key in self.accrual_rows:
            from cloud_platform.modules.billing.service import AccrualPeriodExistsError

            raise AccrualPeriodExistsError(period.idempotency_key)
        self.accrual_rows[period.idempotency_key] = period
        return period

    async def snapshot_get(self, server_id) -> ServerPriceSnapshot | None:
        return self.snapshots.get(server_id)

    def make_service(self) -> FinalChargeService:
        h = self

        @dataclass
        class _WalletRepo:
            async def get(self, user_id):
                return await h.wallet_get(user_id)

            async def debit(self, user_id, amount, key):
                return await h.debit(user_id, amount, key)

        @dataclass
        class _HoldRepo:
            async def get_by_idempotency(self, wallet_id, key):
                return await h.hold_get(wallet_id, key)

        @dataclass
        class _HoldService:
            async def capture_hold(self, wallet_id, hold_id, key):
                return await h.capture_hold(wallet_id, hold_id, key)

        @dataclass
        class _LedgerRepo:
            async def get_entry_by_idempotency(self, wallet_id, key):
                return await h.ledger_get(wallet_id, key)

            async def post_entry(self, wallet_id, amount, currency, etype, key, **kw):
                return await h.ledger_post(wallet_id, amount, currency, etype, key, **kw)

        @dataclass
        class _AccrualRepo:
            async def get_by_key(self, key):
                return await h.accrual_get(key)

            async def add(self, period):
                return await h.accrual_add(period)

            async def list_between(self, start, end):
                return []

        @dataclass
        class _SnapshotRepo:
            async def get(self, server_id):
                return await h.snapshot_get(server_id)

        return FinalChargeService(
            server_repo=self,
            wallet_repo=_WalletRepo(),
            hold_repo=_HoldRepo(),
            hold_service=_HoldService(),
            ledger_repo=_LedgerRepo(),
            accrual_repo=_AccrualRepo(),
            snapshot_repo=_SnapshotRepo(),
            audit_repo=self.audit,
        )


class TestFinalSegmentOnce:
    async def test_partial_first_quantum_captures_the_hold(self) -> None:
        f = Fakes(
            wallet=Wallet(USER_ID, id=WALLET_ID, balance=10_000),
            holds={HOLD_KEY: _hold(HOLD_KEY)},
        )
        server = _deleted_server()

        result = await f.make_service().charge_final(server, T0 + timedelta(minutes=30))

        assert result.charged_minor == SELLING
        assert result.captured_hold is True
        assert result.posted_entry_key == CAPTURE_KEY
        assert result.replayed is False
        assert f.captured == [HOLD_KEY]
        assert f.debit_calls == []  # the hold paid, not a fresh debit
        assert f.wallet.balance == 10_000 - SELLING
        assert server.last_accrued_at == T0 + timedelta(minutes=30)
        record = f.accrual_rows[CAPTURE_KEY]
        assert record.selling_minor == SELLING and record.cost_minor == COST

    async def test_replay_moves_nothing(self) -> None:
        f = Fakes(
            wallet=Wallet(USER_ID, id=WALLET_ID, balance=10_000),
            holds={HOLD_KEY: _hold(HOLD_KEY)},
        )
        server = _deleted_server()
        service = f.make_service()

        first = await service.charge_final(server, T0 + timedelta(minutes=30))
        assert first.charged_minor == SELLING
        balance = f.wallet.balance
        entries = len(f.entries)

        # full replay: nothing may move a second time
        second = await service.charge_final(server, T0 + timedelta(minutes=30))

        assert second.charged_minor == 0
        assert second.captured_hold is False
        assert f.wallet.balance == balance
        assert len(f.entries) == entries
        assert f.debit_calls == []
        assert f.captured == [HOLD_KEY]

    async def test_no_hold_bills_flat_under_final_key(self) -> None:
        f = Fakes()
        server = _deleted_server()

        result = await f.make_service().charge_final(server, T0 + timedelta(minutes=45))

        assert result.charged_minor == SELLING
        assert result.captured_hold is False
        assert result.posted_entry_key == FINAL_KEY
        assert f.debit_calls == [(SELLING, FINAL_KEY)]
        assert f.entries[FINAL_KEY].entry_type is LedgerEntryType.CHARGE
        assert f.wallet.balance == 100_000 - SELLING

        # replay
        second = await f.make_service().charge_final(server, T0 + timedelta(minutes=45))
        assert second.charged_minor == 0
        assert f.debit_calls == [(SELLING, FINAL_KEY)]
        assert f.wallet.balance == 100_000 - SELLING

    async def test_long_window_capture_plus_flat_remainder(self) -> None:
        f = Fakes(holds={HOLD_KEY: _hold(HOLD_KEY)})
        server = _deleted_server()

        result = await f.make_service().charge_final(server, T0 + timedelta(minutes=90))

        # 90 minutes = 1 captured quantum + 1 flat quantum
        assert result.charged_minor == 2 * SELLING
        assert result.captured_hold is True
        assert f.captured == [HOLD_KEY]
        assert f.debit_calls == [(SELLING, FINAL_KEY)]
        assert f.wallet.balance == 100_000 - 2 * SELLING
        # the business record covers the FULL window, under the final key
        record = f.accrual_rows[FINAL_KEY]
        assert record.selling_minor == 2 * SELLING
        assert record.cost_minor == 2 * COST

        # full replay AFTER the watermark was persisted: a pure no-op
        second = await f.make_service().charge_final(server, T0 + timedelta(minutes=90))
        assert second.charged_minor == 0
        assert f.debit_calls == [(SELLING, FINAL_KEY)]
        assert f.wallet.balance == 100_000 - 2 * SELLING

        # crash replay: the watermark save was lost, but the ledger absorbs it
        server.last_accrued_at = None
        third = await f.make_service().charge_final(server, T0 + timedelta(minutes=90))
        assert third.charged_minor == 0
        assert third.replayed is True
        assert f.debit_calls == [(SELLING, FINAL_KEY)]
        assert f.wallet.balance == 100_000 - 2 * SELLING

    async def test_crash_between_legs_settles_only_the_missing_leg(self) -> None:
        f = Fakes(holds={HOLD_KEY: _hold(HOLD_KEY)})
        server = _deleted_server()
        service = f.make_service()

        # simulate: the capture leg ran, then the process died before the
        # flat leg and the watermark save
        hold = f.holds[HOLD_KEY]
        hold.capture()
        f.captured.append(HOLD_KEY)
        f.wallet.balance -= hold.amount
        f.post(hold.amount, CAPTURE_KEY, LedgerEntryType.CHARGE, reference=HOLD_KEY)

        result = await service.charge_final(server, T0 + timedelta(minutes=90))

        assert result.charged_minor == SELLING  # only the missing flat quantum
        assert result.captured_hold is False
        assert f.debit_calls == [(SELLING, FINAL_KEY)]
        assert f.wallet.balance == 100_000 - 2 * SELLING  # total still exactly 2 quanta
        assert len(f.accrual_rows) == 1
        record = f.accrual_rows[FINAL_KEY]
        assert record.selling_minor == 2 * SELLING  # full window recorded


class TestWindowRules:
    async def test_grid_aligned_deletion_charges_nothing(self) -> None:
        f = Fakes(holds={HOLD_KEY: _hold(HOLD_KEY, status=HoldStatus.CAPTURED)})
        f.post(SELLING, CAPTURE_KEY, LedgerEntryType.CHARGE, reference=HOLD_KEY)
        server = _deleted_server(last_accrued_at=T0 + timedelta(hours=1))

        result = await f.make_service().charge_final(server, T0 + timedelta(hours=1))

        assert result.charged_minor == 0
        assert f.debit_calls == []
        assert f.saved == []

    async def test_zero_length_window_charges_nothing(self) -> None:
        f = Fakes()
        server = _deleted_server(last_accrued_at=T0 + timedelta(hours=2))

        result = await f.make_service().charge_final(server, T0 + timedelta(hours=2))

        assert result.charged_minor == 0
        assert f.entries == {}
        assert f.saved == []

    async def test_later_periods_accrued_then_final_partial(self) -> None:
        """Watermark at T0+2h (accrual ran twice), deleted at T0+2h30m."""
        f = Fakes(wallet=Wallet(USER_ID, id=WALLET_ID, balance=100_000 - 2 * SELLING))
        f.post(SELLING, f"accrual:{SERVER_ID}:{int(T0.timestamp())}", LedgerEntryType.CHARGE)
        f.post(
            SELLING,
            f"accrual:{SERVER_ID}:{int((T0 + timedelta(hours=1)).timestamp())}",
            LedgerEntryType.CHARGE,
        )
        server = _deleted_server(last_accrued_at=T0 + timedelta(hours=2))

        result = await f.make_service().charge_final(server, T0 + timedelta(hours=2, minutes=30))

        assert result.charged_minor == SELLING  # the partial third quantum only
        assert result.captured_hold is False
        assert f.debit_calls == [(SELLING, FINAL_KEY)]
        assert f.wallet.balance == 100_000 - 3 * SELLING  # 2 accrued + 1 final


class TestValidation:
    async def test_requires_deleted_state(self) -> None:
        f = Fakes()
        server = _deleted_server(state=ServerLifecycleState.RUNNING)
        with pytest.raises(ValueError, match="DELETED"):
            await f.make_service().charge_final(server, T0 + timedelta(hours=1))

    async def test_missing_snapshot_raises(self) -> None:
        f = Fakes(snapshots={})
        server = _deleted_server()
        with pytest.raises(MissingSnapshotError):
            await f.make_service().charge_final(server, T0 + timedelta(hours=1))

    async def test_deleted_before_created_rejected(self) -> None:
        f = Fakes()
        server = _deleted_server()
        with pytest.raises(ValueError, match="before"):
            await f.make_service().charge_final(server, T0 - timedelta(minutes=1))

    async def test_audit_event(self) -> None:
        f = Fakes()
        server = _deleted_server()
        await f.make_service().charge_final(server, T0 + timedelta(minutes=30))

        event = f.audit.append.call_args.args[0]
        assert event.action == "billing.final_charge"
        assert event.metadata["charged_minor"] == str(SELLING)
        assert event.metadata["replayed"] == "false"
