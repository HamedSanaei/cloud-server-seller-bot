"""Tests for the optional monthly spend cap per price policy (M06-009).

Acceptance: an optional cap cannot overcharge the configured maximum.
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
    AccrualJob,
    AccrualPeriod,
    FinalChargeService,
    month_start_utc,
)
from cloud_platform.modules.compute.domain import CloudServer, ServerLifecycleState
from cloud_platform.modules.pricing.domain import (
    MarginRule,
    OfferCost,
    ServerPriceSnapshot,
    rule_from_dict,
    rule_to_dict,
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


def _rule(cap: int | None = None) -> MarginRule:
    return MarginRule(
        provider="hetzner",
        plan="cx22",
        location="fsn1",
        margin_factor=Decimal("1.43"),
        monthly_cap_minor=cap,
    )


def _snapshot(cap: int | None = None) -> ServerPriceSnapshot:
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
        rule=_rule(cap),
        priced_at=T0,
    )


class TestRuleCapConfig:
    def test_roundtrip_preserves_cap(self) -> None:
        rule = _rule(5000)
        assert rule_from_dict(rule_to_dict(rule)).monthly_cap_minor == 5000

    def test_roundtrip_preserves_no_cap(self) -> None:
        rule = _rule(None)
        assert rule_from_dict(rule_to_dict(rule)).monthly_cap_minor is None

    def test_old_rows_without_cap_key_deserialize_uncapped(self) -> None:
        raw = rule_to_dict(_rule(None))
        del raw["monthly_cap_minor"]  # pre-cap persisted rows lack the key
        assert rule_from_dict(raw).monthly_cap_minor is None

    def test_cap_rejects_negative(self) -> None:
        with pytest.raises(ValueError, match="monthly_cap_minor"):
            _rule(-1)

    def test_month_start_utc_boundaries(self) -> None:
        assert month_start_utc(datetime(2026, 8, 1, 0, 0, tzinfo=UTC)) == datetime(
            2026, 8, 1, tzinfo=UTC
        )
        assert month_start_utc(datetime(2026, 8, 31, 23, 59, tzinfo=UTC)) == datetime(
            2026, 8, 1, tzinfo=UTC
        )
        assert month_start_utc(datetime(2026, 9, 1, 0, 0, tzinfo=UTC)) == datetime(
            2026, 9, 1, tzinfo=UTC
        )
        # naive input is treated as UTC
        assert month_start_utc(datetime(2026, 8, 15, 6, 0)) == datetime(2026, 8, 1, tzinfo=UTC)


class Fakes:
    """Port fakes for one accrual job or final charge service instance."""

    def __init__(
        self,
        servers: list[CloudServer] | None = None,
        wallet: Wallet | None = None,
        holds: dict[str, Hold] | None = None,
        snapshot: ServerPriceSnapshot | None = None,
        seeded_rows: list[AccrualPeriod] | None = None,
    ) -> None:
        self.servers = list(servers or [])
        self.wallet = wallet or Wallet(USER_ID, id=WALLET_ID, balance=100_000)
        self.holds = dict(holds or {})
        self.snapshot = snapshot
        self.rows: list[AccrualPeriod] = list(seeded_rows or [])
        self.rows_by_key = {r.idempotency_key: r for r in self.rows}
        self.entries: dict[str, LedgerEntry] = {}
        self.debit_calls: list[tuple[int, str]] = []
        self.captured: list[str] = []
        self.released: list[str] = []
        self.saved: list[CloudServer] = []
        self.audit = AsyncMock()

    def post(self, amount: int, key: str, etype: LedgerEntryType, reference: str = "") -> None:
        if key in self.entries:
            raise AssertionError(f"duplicate ledger key {key}")
        self.entries[key] = LedgerEntry(
            id=uuid4(),
            wallet_id=WALLET_ID,
            entry_type=etype,
            amount=Money(Decimal(amount), "EUR"),
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

    async def release_hold(self, wallet_id, hold_id, key: str) -> Hold:
        hold = self.holds[key]
        hold.release()
        self.released.append(key)
        self.post(hold.amount, f"release-{key}", LedgerEntryType.RELEASE, reference=key)
        return hold

    async def ledger_get(self, wallet_id, key: str) -> LedgerEntry | None:
        return self.entries.get(key)

    async def ledger_post(
        self, wallet_id, amount: int, currency: str, etype: LedgerEntryType, key: str, **kw: object
    ) -> LedgerEntry:
        self.post(amount, key, etype, reference=str(kw.get("reference_id", "")))
        return self.entries[key]

    async def accrual_get(self, key: str) -> AccrualPeriod | None:
        return self.rows_by_key.get(key)

    async def accrual_add(self, period: AccrualPeriod) -> AccrualPeriod:
        if period.idempotency_key in self.rows_by_key:
            from cloud_platform.modules.billing.service import AccrualPeriodExistsError

            raise AccrualPeriodExistsError(period.idempotency_key)
        self.rows.append(period)
        self.rows_by_key[period.idempotency_key] = period
        return period

    async def month_total(self, wallet_id, month_start) -> int:
        return sum(r.selling_minor for r in self.rows if r.period_start >= month_start)

    async def snapshot_get(self, server_id) -> ServerPriceSnapshot | None:
        return self.snapshot

    def make_job(self) -> AccrualJob:
        h = self

        @dataclass
        class _ServerRepo:
            async def list_running(self):
                return list(h.servers)

            async def save(self, server):
                return await h.save(server)

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

            async def release_hold(self, wallet_id, hold_id, key):
                return await h.release_hold(wallet_id, hold_id, key)

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
                return [r for r in h.rows if start <= r.period_start < end]

            async def month_total(self, wallet_id, month_start):
                return await h.month_total(wallet_id, month_start)

        @dataclass
        class _SnapshotRepo:
            async def get(self, server_id):
                return await h.snapshot_get(server_id)

        return AccrualJob(
            server_repo=_ServerRepo(),
            wallet_repo=_WalletRepo(),
            hold_repo=_HoldRepo(),
            hold_service=_HoldService(),
            ledger_repo=_LedgerRepo(),
            accrual_repo=_AccrualRepo(),
            snapshot_repo=_SnapshotRepo(),
            audit_repo=self.audit,
        )

    def make_final_service(self) -> FinalChargeService:
        h = self

        @dataclass
        class _ServerRepo:
            async def save(self, server):
                return await h.save(server)

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

            async def release_hold(self, wallet_id, hold_id, key):
                return await h.release_hold(wallet_id, hold_id, key)

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
                return [r for r in h.rows if start <= r.period_start < end]

            async def month_total(self, wallet_id, month_start):
                return await h.month_total(wallet_id, month_start)

        @dataclass
        class _SnapshotRepo:
            async def get(self, server_id):
                return await h.snapshot_get(server_id)

        return FinalChargeService(
            server_repo=_ServerRepo(),
            wallet_repo=_WalletRepo(),
            hold_repo=_HoldRepo(),
            hold_service=_HoldService(),
            ledger_repo=_LedgerRepo(),
            accrual_repo=_AccrualRepo(),
            snapshot_repo=_SnapshotRepo(),
            audit_repo=self.audit,
        )


def _running_server(**kw: object) -> CloudServer:
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


def _hold(key: str, status: HoldStatus = HoldStatus.CREATED) -> Hold:
    return Hold(
        wallet_id=WALLET_ID,
        amount=SELLING,
        currency="EUR",
        idempotency_key=key,
        id=uuid4(),
        status=status,
    )


class TestAccrualCap:
    async def test_cap_settles_excess_periods_as_capped(self) -> None:
        # cap = 2 quanta per month; 5 complete periods are due
        server = _running_server()
        fakes = Fakes(servers=[server], snapshot=_snapshot(cap=2 * SELLING))

        report = await fakes.make_job().run(now=T0 + timedelta(hours=5))

        assert report.periods_posted == 2
        assert report.capped_periods == 3  # the 3rd-5th quanta of the month
        assert report.charged_minor == 2 * SELLING
        assert fakes.wallet.balance == 100_000 - 2 * SELLING
        assert len(fakes.debit_calls) == 2
        # the watermark advanced past the capped periods: they are never retried
        assert server.last_accrued_at == T0 + timedelta(hours=5)
        # exactly 2 ledger entries (2 billed periods, no hold in this fixture)
        assert len(fakes.entries) == 2
        # exactly 2 business records (the 2 billed periods)
        assert len(fakes.rows) == 2

    async def test_cap_is_per_usage_month(self) -> None:
        # created at Aug 31 23:00; three periods, all ending in September
        created = datetime(2026, 8, 31, 23, 0, tzinfo=UTC)
        server = _running_server(created_at=created)
        fakes = Fakes(servers=[server], snapshot=_snapshot(cap=3 * SELLING))

        report = await fakes.make_job().run(now=datetime(2026, 9, 1, 2, 0, tzinfo=UTC))

        assert report.periods_posted == 3  # fresh month, fresh cap
        assert report.capped_periods == 0
        assert fakes.wallet.balance == 100_000 - 3 * SELLING

    async def test_cap_boundary_exact_amount_is_allowed(self) -> None:
        # billed + this period == cap: allowed ("cannot overcharge", not "must be below")
        server = _running_server()
        fakes = Fakes(servers=[server], snapshot=_snapshot(cap=2 * SELLING))

        report = await fakes.make_job().run(now=T0 + timedelta(hours=2))

        assert report.periods_posted == 2
        assert report.capped_periods == 0
        assert report.charged_minor == 2 * SELLING

    async def test_capped_periods_are_audited(self) -> None:
        server = _running_server()
        fakes = Fakes(servers=[server], snapshot=_snapshot(cap=1 * SELLING))

        await fakes.make_job().run(now=T0 + timedelta(hours=3))

        cap_events = [
            c.args[0]
            for c in fakes.audit.append.call_args_list
            if c.args[0].action == "billing.cap_reached"
        ]
        assert len(cap_events) == 2  # quanta 2 and 3 were capped
        first = cap_events[0]
        assert first.metadata["cap"] == str(SELLING)
        assert first.metadata["billed"] == str(SELLING)
        assert first.metadata["skipped_charge"] == str(SELLING)
        assert first.metadata["month_start"] == "2026-08-01T00:00:00+00:00"


class TestFinalChargeCap:
    def _seeded_month(self, fakes: Fakes, deleted: datetime) -> None:
        """A period already billed this month (so the month has used cap room)."""
        row = AccrualPeriod(
            server_id=uuid4(),
            wallet_id=WALLET_ID,
            period_start=deleted - timedelta(hours=2),
            period_end=deleted - timedelta(hours=1),
            selling_minor=SELLING,
            cost_minor=COST,
            currency="EUR",
            idempotency_key="seed-1",
        )
        fakes.rows.append(row)
        fakes.rows_by_key[row.idempotency_key] = row

    async def test_capped_final_releases_the_hold(self) -> None:
        deleted = T0 + timedelta(minutes=30)
        fakes = Fakes(
            wallet=Wallet(USER_ID, id=WALLET_ID, balance=100_000),
            holds={HOLD_KEY: _hold(HOLD_KEY)},
            snapshot=_snapshot(cap=SELLING),
        )
        self._seeded_month(fakes, deleted)  # month already used its one quantum of cap
        server = _running_server(state=ServerLifecycleState.DELETED)

        result = await fakes.make_final_service().charge_final(server, deleted)

        # nothing may be charged: the cap protects the user
        assert result.charged_minor == 0
        assert result.capped is True
        assert result.captured_hold is False
        assert fakes.debit_calls == []
        assert fakes.wallet.balance == 100_000
        # the reserved creation hold is released, not stranded
        assert fakes.released == [HOLD_KEY]
        assert fakes.holds[HOLD_KEY].status is HoldStatus.RELEASED
        assert f"release-{HOLD_KEY}" in fakes.entries
        # the audit trail explains both skipped legs and the final charge
        actions = [c.args[0].action for c in fakes.audit.append.call_args_list]
        assert actions.count("billing.cap_reached") == 2
        final = next(
            c.args[0]
            for c in fakes.audit.append.call_args_list
            if c.args[0].action == "billing.final_charge"
        )
        assert final.metadata["capped"] == "true"

    async def test_final_within_cap_charges_normally(self) -> None:
        deleted = T0 + timedelta(minutes=90)
        fakes = Fakes(
            holds={HOLD_KEY: _hold(HOLD_KEY)},
            snapshot=_snapshot(cap=2 * SELLING),
        )
        server = _running_server(state=ServerLifecycleState.DELETED)

        result = await fakes.make_final_service().charge_final(server, deleted)

        # 1 captured quantum + 1 flat quantum == exactly the cap
        assert result.charged_minor == 2 * SELLING
        assert result.captured_hold is True
        assert result.capped is False
        assert fakes.wallet.balance == 100_000 - 2 * SELLING

    async def test_final_flat_leg_capped(self) -> None:
        deleted = T0 + timedelta(minutes=30)
        fakes = Fakes(
            snapshot=_snapshot(cap=SELLING),
        )
        self._seeded_month(fakes, deleted)  # no hold: the flat leg must be capped
        server = _running_server(state=ServerLifecycleState.DELETED)

        result = await fakes.make_final_service().charge_final(server, deleted)

        assert result.charged_minor == 0
        assert result.capped is True
        assert fakes.debit_calls == []
        assert FINAL_KEY not in fakes.entries

    async def test_no_cap_behaves_unchanged(self) -> None:
        deleted = T0 + timedelta(minutes=30)
        fakes = Fakes(
            holds={HOLD_KEY: _hold(HOLD_KEY)},
            snapshot=_snapshot(cap=None),
        )
        server = _running_server(state=ServerLifecycleState.DELETED)

        result = await fakes.make_final_service().charge_final(server, deleted)

        assert result.charged_minor == SELLING
        assert result.captured_hold is True
        assert result.capped is False
