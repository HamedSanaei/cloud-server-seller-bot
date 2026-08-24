"""Multi-provider billing parity tests (M15-006).

Acceptance: different billing quantum/policy settles correctly.

Two providers with different billing shapes (Hetzner: EUR, hourly quantum,
integer-minor prices; ArvanCloud: IRR, hourly or daily quantum, IRR prices
mapped from the provider's own payload in M15-003) must settle through the
SAME provider-neutral billing pipeline:

- the catalog mapper + ingestion normalize both providers to the same
  hourly-minor offer shape (no core-domain change per provider);
- the accrual job and the final charge bill each provider in its own
  currency at its own quantum, with identical idempotency semantics;
- one provider's failure never disturbs the other's settlement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from cloud_platform.core.money import Money
from cloud_platform.modules.billing.service import (
    AccrualJob,
    AccrualPeriodExistsError,
    FinalChargeService,
)
from cloud_platform.modules.catalog.domain import (
    CatalogEntrySpec,
    PlanPricing,
    ProviderPriceEntry,
    to_minor_units,
)
from cloud_platform.modules.catalog.service import PricingIngestionService
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
from cloud_platform.providers.arvancloud.sync import (
    PROVIDER_KEY as ARVANCLOUD,
)
from cloud_platform.providers.arvancloud.sync import plan_pricing_from_arvancloud

T0 = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Catalog normalization parity: provider payloads -> one neutral offer shape
# ---------------------------------------------------------------------------


class _CatalogCapture:
    """CatalogRepository fake that captures the normalized specs."""

    def __init__(self) -> None:
        self.specs: list[CatalogEntrySpec] = []
        self._seen: set[tuple[str, str, str]] = set()

    async def upsert_entry(self, spec: CatalogEntrySpec) -> bool:
        key = (spec.provider_key, spec.plan_id, spec.location_id)
        self.specs.append(spec)
        created = key not in self._seen
        self._seen.add(key)
        return created

    async def get_offer(self, ref):  # pragma: no cover - not used here
        return None

    async def set_offer_enabled(self, ref, enabled: bool) -> None:  # pragma: no cover
        return None


def _arvancloud_plan_payload() -> dict:
    """A raw ArvanCloud ``Plan`` as returned by the adapter's API (spec 2.0).

    ``price_per_hour`` is a fractional number (major IRR units) - the
    float path the mapper must convert through str (no float money math).
    """
    return {
        "id": "123",
        "name": "General 2C/4G",
        "type": "general",
        "category": "Compute",
        "cpu_count": 2,
        "memory": 4,
        "memory_in_bytes": 4 * 1024**3,
        "disk": 40,
        "disk_in_bytes": 40 * 1024**3,
        "price_per_hour": 2500.5,
        "price_per_month": 600_000,
    }


class TestCatalogNormalizationParity:
    async def test_arvancloud_plan_maps_to_hourly_minor_irr(self) -> None:
        repo = _CatalogCapture()
        service = PricingIngestionService(repo)
        plan = plan_pricing_from_arvancloud(_arvancloud_plan_payload(), location_id="ir-thr-1")
        result = await service.ingest_plan(ARVANCLOUD, plan)
        assert len(result.prices) == 1
        price = result.prices[0]
        assert price.location_id == "ir-thr-1"
        assert price.currency == "IRR"
        # 2500.5 major IRR -> 250050 minor IRR, exact (float never in the math)
        assert price.hourly_minor == 250_050
        spec = repo.specs[0]
        assert spec.provider_key == ARVANCLOUD
        assert spec.price_per_quantum == 250_050
        assert spec.currency == "IRR"
        assert spec.memory_mb == 4096
        assert spec.disk_gb == 40

    async def test_arvancloud_monthly_only_derives_hourly(self) -> None:
        repo = _CatalogCapture()
        service = PricingIngestionService(repo)
        payload = _arvancloud_plan_payload()
        payload.pop("price_per_hour")  # monthly-only plan
        plan = plan_pricing_from_arvancloud(payload, location_id="ir-thr-1")
        result = await service.ingest_plan(ARVANCLOUD, plan)
        expected = to_minor_units(Decimal(600_000) / Decimal(720))
        assert result.prices[0].hourly_minor == expected

    async def test_arvancloud_float_hourly_is_exact_through_str(self) -> None:
        # 0.1 is inexact in binary; the float->str path must yield exactly 10 minor.
        repo = _CatalogCapture()
        service = PricingIngestionService(repo)
        payload = _arvancloud_plan_payload()
        payload["price_per_hour"] = 0.1
        plan = plan_pricing_from_arvancloud(payload, location_id="ir-thr-1")
        result = await service.ingest_plan(ARVANCLOUD, plan)
        assert result.prices[0].hourly_minor == 10

    async def test_hetzner_plan_flows_through_the_same_ingestion(self) -> None:
        """Hetzner's normalized plan uses the SAME ingest path and produces
        the same IngestedPrice shape - parity at the catalog boundary."""
        repo = _CatalogCapture()
        service = PricingIngestionService(repo)
        plan = PlanPricing(
            plan_id="cx22",
            name="CX22",
            architecture="x86_64",
            vcpu=2,
            memory_mb=4096,
            disk_gb=40,
            prices=(
                ProviderPriceEntry(location_id="fsn1", currency="EUR", hourly=Decimal("15.87")),
            ),
        )
        result = await service.ingest_plan("hetzner", plan)
        assert result.prices[0].location_id == "fsn1"
        assert result.prices[0].currency == "EUR"
        assert result.prices[0].hourly_minor == 1587
        # identical spec shape as the arvancloud path
        spec = repo.specs[0]
        assert spec.provider_key == "hetzner"
        assert spec.price_per_quantum == 1587

    async def test_both_providers_normalize_to_one_offer_shape(self) -> None:
        """The normalized offer of each provider carries the same fields the
        billing pipeline consumes (per-quantum price + quantum + currency)."""
        ac_repo = _CatalogCapture()
        service = PricingIngestionService(ac_repo)
        await service.ingest_plan(
            ARVANCLOUD,
            plan_pricing_from_arvancloud(_arvancloud_plan_payload(), location_id="ir-thr-1"),
        )
        hz_repo = _CatalogCapture()
        await PricingIngestionService(hz_repo).ingest_plan(
            "hetzner",
            PlanPricing(
                plan_id="cx22",
                name="CX22",
                architecture="x86_64",
                vcpu=2,
                memory_mb=4096,
                disk_gb=40,
                prices=(
                    ProviderPriceEntry(location_id="fsn1", currency="EUR", hourly=Decimal("15.87")),
                ),
            ),
        )
        ac_spec, hz_spec = ac_repo.specs[0], hz_repo.specs[0]
        for spec in (ac_spec, hz_spec):
            assert spec.provider_key  # identity
            assert spec.price_per_quantum > 0  # per-quantum price
            assert spec.currency in ("IRR", "EUR")
        # distinct currencies are preserved end to end (no silent conversion)
        assert (ac_spec.currency, hz_spec.currency) == ("IRR", "EUR")


# ---------------------------------------------------------------------------
# Billing engine parity: accrual job + final charge across providers
# ---------------------------------------------------------------------------

EUR_USER = uuid4()
IRR_USER = uuid4()
HZ_SERVER = uuid4()
AC_SERVER = uuid4()

HZ_COST = 700  # EUR minor per quantum
HZ_SELLING = 1_000
AC_COST = 2_000_000  # IRR minor per quantum
AC_SELLING = 2_500_000

HZ_IK = "order-hz-1"
AC_IK = "order-ac-1"


def _snapshot(
    server_id: UUID, provider: str, plan: str, location: str, cost: int, currency: str, selling: int
) -> ServerPriceSnapshot:
    return ServerPriceSnapshot(
        server_id=server_id,
        offer=OfferCost(
            provider_key=provider,
            plan_id=plan,
            location_id=location,
            cost_minor=cost,
            currency=currency,
        ),
        selling_minor=selling,
        book_name="retail",
        book_version=1,
        rule=MarginRule(
            provider=provider, plan=plan, location=location, margin_factor=Decimal("1.43")
        ),
        priced_at=T0,
    )


def _server(
    server_id: UUID,
    user_id: UUID,
    provider: str,
    ik: str,
    *,
    quantum: int,
    state=ServerLifecycleState.RUNNING,
) -> CloudServer:
    return CloudServer(
        id=server_id,
        user_id=user_id,
        provider_key=provider,
        provider_account_id=uuid4(),
        state=state,
        idempotency_key=ik,
        created_at=T0,
        quantum_seconds=quantum,
    )


class ParityHarness:
    """Two users, two currencies, two providers - one shared settlement run."""

    def __init__(
        self,
        servers: list[CloudServer],
        wallets: dict[UUID, Wallet],
        snapshots: dict[UUID, ServerPriceSnapshot],
        holds: dict[str, Hold] | None = None,
    ) -> None:
        self.servers = list(servers)
        self.wallets = dict(wallets)
        self.snapshots = dict(snapshots)
        self.holds: dict[str, Hold] = dict(holds or {})
        self.saved: list[CloudServer] = []
        self.debits: list[tuple[UUID, int, str]] = []
        self.captured: list[str] = []
        self.entries: dict[tuple[UUID, str], LedgerEntry] = {}
        self.accrual_rows: dict[str, object] = {}

    # -- shared money posting (mirrors the DB unique key: duplicates explode) --
    def post(
        self,
        wallet_id: UUID,
        amount: int,
        key: str,
        etype: LedgerEntryType,
        currency: str,
        reference: str = "",
    ) -> None:
        if (wallet_id, key) in self.entries:
            raise AssertionError(f"duplicate ledger key {key}")
        self.entries[(wallet_id, key)] = LedgerEntry(
            id=uuid4(),
            wallet_id=wallet_id,
            entry_type=etype,
            amount=Money(Decimal(amount), currency),
            reference_type="hold" if etype is LedgerEntryType.CHARGE else "server",
            reference_id=reference,
            idempotency_key=key,
        )

    # -- ServerRepository ------------------------------------------------------
    async def list_running(self) -> list[CloudServer]:
        return [s for s in self.servers if s.state is ServerLifecycleState.RUNNING]

    async def save(self, server: CloudServer) -> CloudServer:
        self.saved.append(server)
        return server

    # -- WalletRepository ------------------------------------------------------
    async def wallet_get(self, user_id: UUID) -> Wallet | None:
        return self.wallets.get(user_id)

    async def debit(self, user_id: UUID, amount: int, key: str) -> Wallet:
        wallet = self.wallets[user_id]
        if wallet.balance < amount:
            raise InsufficientBalanceError(f"balance {wallet.balance} < {amount}")
        self.debits.append((user_id, amount, key))
        wallet.balance -= amount
        return wallet

    # -- HoldRepository / HoldService ------------------------------------------
    async def hold_get(self, wallet_id: UUID, key: str) -> Hold | None:
        return self.holds.get(key)

    async def capture_hold(self, wallet_id: UUID, hold_id: UUID, key: str) -> Hold:
        hold = self.holds[key]
        if hold.status is not HoldStatus.CREATED:
            raise ValueError("cannot capture")
        hold.capture()
        self.captured.append(key)
        wallet = self.wallets_by_wallet_id[wallet_id]
        wallet.balance -= hold.amount
        self.post(
            wallet_id,
            hold.amount,
            f"capture-{key}",
            LedgerEntryType.CHARGE,
            wallet.currency,
            reference=key,
        )
        return hold

    async def release_hold(self, wallet_id: UUID, hold_id: UUID, key: str) -> Hold | None:
        hold = self.holds.get(key)
        if hold is None or hold.status is not HoldStatus.CREATED:
            return None
        hold.release()
        return hold

    @property
    def wallets_by_wallet_id(self) -> dict[UUID, Wallet]:
        return {w.id: w for w in self.wallets.values() if w.id is not None}

    # -- LedgerRepository --------------------------------------------------------
    async def ledger_get(self, wallet_id: UUID, key: str) -> LedgerEntry | None:
        return self.entries.get((wallet_id, key))

    def ledger_get_sync(self, wallet_id: UUID, key: str) -> LedgerEntry | None:
        return self.entries.get((wallet_id, key))

    async def ledger_post(
        self,
        wallet_id: UUID,
        amount: int,
        currency: str,
        etype: LedgerEntryType,
        key: str,
        **kw: object,
    ) -> LedgerEntry:
        self.post(
            wallet_id, amount, key, etype, currency, reference=str(kw.get("reference_id", ""))
        )
        return self.entries[(wallet_id, key)]

    # -- AccrualPeriodRepository ---------------------------------------------------
    async def accrual_get(self, key: str):
        return self.accrual_rows.get(key)

    async def accrual_add(self, period) -> object:
        if period.idempotency_key in self.accrual_rows:
            raise AccrualPeriodExistsError(period.idempotency_key)
        self.accrual_rows[period.idempotency_key] = period
        return period

    async def accrual_between(self, start, end):
        return [p for p in self.accrual_rows.values() if start <= p.period_start < end]

    async def month_total(self, wallet_id, month_start):
        return sum(
            p.selling_minor
            for p in self.accrual_rows.values()
            if p.wallet_id == wallet_id and p.period_start >= month_start
        )

    async def daily_cost_total(self, day_start, day_end, server_ids):
        return sum(
            p.cost_minor
            for p in self.accrual_rows.values()
            if p.server_id in server_ids and day_start <= p.period_start < day_end
        )

    # -- ServerPriceSnapshotRepository ------------------------------------------------
    async def snapshot_get(self, server_id: UUID) -> ServerPriceSnapshot | None:
        return self.snapshots.get(server_id)

    def make_accrual_job(self) -> AccrualJob:
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
                return await h.accrual_between(start, end)

            async def month_total(self, wallet_id, month_start):
                return await h.month_total(wallet_id, month_start)

            async def daily_cost_total(self, day_start, day_end, server_ids):
                return await h.daily_cost_total(day_start, day_end, server_ids)

        @dataclass
        class _SnapshotRepo:
            async def get(self, server_id):
                return await h.snapshot_get(server_id)

        return AccrualJob(
            server_repo=self,
            wallet_repo=_WalletRepo(),
            hold_repo=_HoldRepo(),
            hold_service=_HoldService(),
            ledger_repo=_LedgerRepo(),
            accrual_repo=_AccrualRepo(),
            snapshot_repo=_SnapshotRepo(),
            audit_repo=AsyncMock(),
        )

    def make_final_charge_service(self) -> FinalChargeService:
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
                return await h.accrual_between(start, end)

            async def month_total(self, wallet_id, month_start):
                return await h.month_total(wallet_id, month_start)

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
            audit_repo=AsyncMock(),
        )


def _hold(wallet_id: UUID, amount: int, currency: str, ik: str) -> tuple[str, Hold]:
    key = f"server-create:{ik}"
    return key, Hold(
        wallet_id=wallet_id,
        amount=amount,
        currency=currency,
        idempotency_key=key,
        id=uuid4(),
        status=HoldStatus.CREATED,
    )


def _parity_setup(
    *,
    ac_quantum: int = 3600,
    hz_balance: int = 100_000,
    ac_balance: int = 100_000_000,
) -> ParityHarness:
    hz_wallet = Wallet(EUR_USER, id=uuid4(), balance=hz_balance, currency="EUR")
    ac_wallet = Wallet(IRR_USER, id=uuid4(), balance=ac_balance, currency="IRR")
    servers = [
        _server(HZ_SERVER, EUR_USER, "hetzner", HZ_IK, quantum=3600),
        _server(AC_SERVER, IRR_USER, ARVANCLOUD, AC_IK, quantum=ac_quantum),
    ]
    snapshots = {
        HZ_SERVER: _snapshot(HZ_SERVER, "hetzner", "cx22", "fsn1", HZ_COST, "EUR", HZ_SELLING),
        AC_SERVER: _snapshot(AC_SERVER, ARVANCLOUD, "123", "ir-thr-1", AC_COST, "IRR", AC_SELLING),
    }
    hz_key, hz_hold = _hold(hz_wallet.id, HZ_SELLING, "EUR", HZ_IK)
    ac_key, ac_hold = _hold(ac_wallet.id, AC_SELLING, "IRR", AC_IK)
    return ParityHarness(
        servers=servers,
        wallets={EUR_USER: hz_wallet, IRR_USER: ac_wallet},
        snapshots=snapshots,
        holds={hz_key: hz_hold, ac_key: ac_hold},
    )


class TestAccrualParity:
    async def test_same_window_settles_in_each_provider_currency(self) -> None:
        h = _parity_setup()
        hz_wallet = h.wallets[EUR_USER]
        ac_wallet = h.wallets[IRR_USER]
        report = await h.make_accrual_job().run(now=T0 + timedelta(hours=2, minutes=30))

        assert report.servers_checked == 2
        assert report.errors == 0
        # 2 complete 1h periods for EACH provider (2h30m window)
        assert report.periods_posted == 4
        # Hetzner: 1000 EUR (hold capture) + 1000 EUR (debit);
        # ArvanCloud: 2_500_000 IRR (hold capture) + 2_500_000 IRR (debit)
        hz_debits = [(a, k) for (u, a, k) in h.debits if u == EUR_USER]
        ac_debits = [(a, k) for (u, a, k) in h.debits if u == IRR_USER]
        # period 1 is the creation-hold capture (no debit), period 2 a plain debit
        assert [d[0] for d in hz_debits] == [HZ_SELLING]
        assert [d[0] for d in ac_debits] == [AC_SELLING]
        assert set(h.captured) == {f"server-create:{HZ_IK}", f"server-create:{AC_IK}"}
        # wallet math per currency
        assert hz_wallet.balance == 100_000 - HZ_SELLING * 2
        assert ac_wallet.balance == 100_000_000 - AC_SELLING * 2
        # ledger entries carry the wallet's own currency
        for wallet_id, entry in ((w, e) for (w, _key), e in h.entries.items()):
            if wallet_id == hz_wallet.id:
                assert entry.amount.currency == "EUR"
            else:
                assert entry.amount.currency == "IRR"
        # accrual business records: margin consistent per provider
        hz_rows = [p for p in h.accrual_rows.values() if p.server_id == HZ_SERVER]
        ac_rows = [p for p in h.accrual_rows.values() if p.server_id == AC_SERVER]
        assert len(hz_rows) == 2
        assert len(ac_rows) == 2
        for p in hz_rows:
            assert p.selling_minor == HZ_SELLING
            assert p.cost_minor == HZ_COST
            assert p.currency == "EUR"
        for p in ac_rows:
            assert p.selling_minor == AC_SELLING
            assert p.cost_minor == AC_COST
            assert p.currency == "IRR"
        # watermarks advanced identically (2 quanta each)
        assert h.servers[0].last_accrued_at == T0 + timedelta(hours=2)
        assert h.servers[1].last_accrued_at == T0 + timedelta(hours=2)

    async def test_replay_is_idempotent_for_every_provider(self) -> None:
        h = _parity_setup()
        job = h.make_accrual_job()
        first = await job.run(now=T0 + timedelta(hours=2, minutes=30))
        assert first.periods_posted == 4
        balances = {u: w.balance for u, w in h.wallets.items()}
        # crash-replay: the watermarks never advanced (crash before save),
        # so the next run re-derives every period from the ledger
        for server in h.servers:
            server.last_accrued_at = None
        second = await job.run(now=T0 + timedelta(hours=2, minutes=30))
        assert second.periods_posted == 0
        assert second.periods_replayed == 4
        assert second.errors == 0
        # no money moved on replay, for either currency
        assert {u: w.balance for u, w in h.wallets.items()} == balances
        assert len(h.debits) == 2  # only the original second-period debit per wallet

    async def test_one_provider_failure_never_disturbs_the_other(self) -> None:
        # IRR wallet cannot cover even one period (and no hold was reserved).
        h = _parity_setup(ac_balance=100_000)
        h.holds.pop(f"server-create:{AC_IK}")
        report = await h.make_accrual_job().run(now=T0 + timedelta(hours=2, minutes=30))
        # hetzner settles fully; arvancloud is the only unsettled server
        assert report.insufficient_balance == 1
        assert report.periods_posted == 2
        assert h.wallets[EUR_USER].balance == 100_000 - HZ_SELLING * 2
        assert h.wallets[IRR_USER].balance == 100_000  # untouched
        ac_debits = [d for d in h.debits if d[0] == IRR_USER]
        assert ac_debits == []

    async def test_partial_period_unequal_across_quantums(self) -> None:
        """The trailing 30m is an incomplete quantum for BOTH hourly providers:
        neither bills it in the periodic run (the final charge owns it)."""
        h = _parity_setup()
        report = await h.make_accrual_job().run(now=T0 + timedelta(hours=1, minutes=30))
        assert report.periods_posted == 2  # one complete 1h period each
        assert h.servers[0].last_accrued_at == T0 + timedelta(hours=1)
        assert h.servers[1].last_accrued_at == T0 + timedelta(hours=1)


class TestFinalChargeParity:
    async def test_hourly_final_charge_parity(self) -> None:
        """Both hourly providers: hold-capture + ceiling remainder settle in
        their own currency, each exactly once."""
        h = _parity_setup()
        deleted_at = T0 + timedelta(hours=2, minutes=30)
        # settle the first complete period for each via the job
        await h.make_accrual_job().run(now=deleted_at)
        # now delete both (same window end)
        h.servers[0].state = ServerLifecycleState.DELETED
        h.servers[1].state = ServerLifecycleState.DELETED

        result_hz = await h.make_final_charge_service().charge_final(h.servers[0], deleted_at)
        result_ac = await h.make_final_charge_service().charge_final(h.servers[1], deleted_at)

        # hetzner: the 30m remainder bills ONE full hourly quantum
        assert result_hz.charged_minor == HZ_SELLING
        assert result_hz.captured_hold is False  # hold captured by the first period
        # arvancloud: same policy, same math, its own currency
        assert result_ac.charged_minor == AC_SELLING
        assert result_ac.captured_hold is False
        # wallet math per currency
        assert h.wallets[EUR_USER].balance == 100_000 - HZ_SELLING * 3
        assert h.wallets[IRR_USER].balance == 100_000_000 - AC_SELLING * 3
        # each final entry under its deterministic key
        assert h.ledger_get_sync(hz_wallet_id(h), f"final:{HZ_SERVER}") is not None
        assert h.ledger_get_sync(ac_wallet_id(h), f"final:{AC_SERVER}") is not None

        # replay: nothing moves again for either provider (the first charge
        # advanced the watermark to the deletion instant, so the replayed
        # window is zero-length - the ledger keys are the backstop)
        entry_count = len(h.entries)
        replay_hz = await h.make_final_charge_service().charge_final(h.servers[0], deleted_at)
        replay_ac = await h.make_final_charge_service().charge_final(h.servers[1], deleted_at)
        assert replay_hz.charged_minor == 0
        assert replay_ac.charged_minor == 0
        assert len(h.entries) == entry_count
        assert h.wallets[EUR_USER].balance == 100_000 - HZ_SELLING * 3
        assert h.wallets[IRR_USER].balance == 100_000_000 - AC_SELLING * 3

    async def test_daily_quantum_policy_settles_correctly(self) -> None:
        """An ArvanCloud offer sold at a DAILY quantum: a 2h30m life bills
        exactly ONE quantum (the hold capture), while the hourly Hetzner
        parity server bills THREE (one per hour). Both correct per policy."""
        h = _parity_setup(ac_quantum=86400)
        hz_wallet = h.wallets[EUR_USER]
        ac_wallet = h.wallets[IRR_USER]
        deleted_at = T0 + timedelta(hours=2, minutes=30)

        # periodic run: the daily-quantum server has NO complete period yet
        report = await h.make_accrual_job().run(now=deleted_at)
        assert report.periods_posted == 2  # only hetzner's two hourly periods
        assert h.servers[1].last_accrued_at is None  # arvancloud untouched
        assert ac_wallet.balance == 100_000_000  # no arvancloud debit yet

        # delete both at the same instant
        h.servers[0].state = ServerLifecycleState.DELETED
        h.servers[1].state = ServerLifecycleState.DELETED
        await h.make_final_charge_service().charge_final(h.servers[0], deleted_at)

        result_ac = await h.make_final_charge_service().charge_final(h.servers[1], deleted_at)
        # the daily hold was still CREATED (no period settled it) -> captured,
        # covering the FIRST (partial) day: exactly one quantum, no flat leg
        assert result_ac.captured_hold is True
        assert result_ac.charged_minor == AC_SELLING
        assert ac_wallet.balance == 100_000_000 - AC_SELLING
        # hetzner billed its three hourly quanta instead
        assert hz_wallet.balance == 100_000 - HZ_SELLING * 3
        # margin record for the daily window: cost + selling of ONE daily quantum
        ac_records = [p for p in h.accrual_rows.values() if p.server_id == AC_SERVER]
        assert len(ac_records) == 1
        assert ac_records[0].selling_minor == AC_SELLING
        assert ac_records[0].cost_minor == AC_COST
        assert ac_records[0].currency == "IRR"

    async def test_daily_quantum_replay_idempotent(self) -> None:
        h = _parity_setup(ac_quantum=86400)
        deleted_at = T0 + timedelta(hours=2, minutes=30)
        h.servers[1].state = ServerLifecycleState.DELETED
        first = await h.make_final_charge_service().charge_final(h.servers[1], deleted_at)
        assert first.captured_hold is True
        assert first.charged_minor == AC_SELLING
        balance = h.wallets[IRR_USER].balance
        entry_count = len(h.entries)
        second = await h.make_final_charge_service().charge_final(h.servers[1], deleted_at)
        assert second.charged_minor == 0
        assert len(h.entries) == entry_count
        assert h.wallets[IRR_USER].balance == balance


def hz_wallet_id(h: ParityHarness) -> UUID:
    return h.wallets[EUR_USER].id  # type: ignore[return-value]


def ac_wallet_id(h: ParityHarness) -> UUID:
    return h.wallets[IRR_USER].id  # type: ignore[return-value]
