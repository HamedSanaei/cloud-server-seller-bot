"""USD-offer/IRT-wallet checkout: binding, durable reservation and POST gate."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from test_hourly_cloud_flow import (
    USER,
    FakeAccountRepo,
    FakeAuditRepo,
    FakeHourlyProvider,
    FakeOffersRepo,
    FakeOpsRepo,
    FakeServerRepo,
    _usd_offer,
)

from cloud_platform.modules.billing.service import (
    PrepaidHourlyBillingService,
    PrepaidHourlyPeriod,
)
from cloud_platform.modules.compute.domain import BILLING_MODEL_PREPAID_HOURLY_IRT
from cloud_platform.modules.fx.domain import ConversionSnapshot, FxPurpose
from cloud_platform.modules.hourly.service import HourlyCloudService, HourlyError
from cloud_platform.modules.pricing.domain import snapshot_from_selling_price
from cloud_platform.modules.wallet.domain import Hold, HoldStatus, InsufficientHoldBalanceError


class Servers(FakeServerRepo):
    async def create(self, server, intent):
        server.created_at = datetime.now(UTC)
        server.idempotency_key = intent.idempotency_key
        return await super().create(server, intent)


class Snapshots:
    def __init__(self):
        self.rows = {}

    async def create_snapshot(self, *, server_id, price, actor, reason):
        snapshot = snapshot_from_selling_price(server_id, price)
        self.rows[server_id] = snapshot
        return snapshot

    async def get_snapshot(self, server_id):
        return self.rows.get(server_id)

    async def require_snapshot(self, server_id):
        return self.rows[server_id]

    async def get(self, server_id):
        return self.rows.get(server_id)


class Wallets:
    def __init__(self, currency="IRT", balance=100_000):
        self.wallet = SimpleNamespace(
            id=uuid4(), currency=currency, balance=balance, status=SimpleNamespace(value="active")
        )
        # The application compares the real enum by identity.
        from cloud_platform.modules.wallet.domain import WalletStatus

        self.wallet.status = WalletStatus.ACTIVE

    async def get(self, user_id):
        return self.wallet


class Quotes:
    def __init__(self):
        self.calls = 0

    async def snapshot(self, amount_minor, source_currency, target_currency, purpose):
        assert (source_currency, target_currency, purpose) == ("USD", "IRT", FxPurpose.CHARGE)
        self.calls += 1
        now = datetime.now(UTC)
        return ConversionSnapshot(
            source_amount_minor=amount_minor,
            source_currency="USD",
            target_amount_minor=amount_minor * 10_000,
            target_currency="IRT",
            rate=Decimal("1000000"),
            purpose=FxPurpose.CHARGE,
            source="abantether",
            path="USDTIRT.buy USD/USDT=1",
            observed_at=now,
            expires_at=now + timedelta(minutes=10),
            proxy=True,
            proxy_asset="USDT",
        )


class Periods:
    def __init__(self):
        self.rows: dict[tuple[UUID, datetime], PrepaidHourlyPeriod] = {}

    async def get(self, server_id, start):
        return self.rows.get((server_id, start))

    async def bind(self, period):
        key = (period.server_id, period.period_start)
        return self.rows.setdefault(key, period)


class Holds:
    def __init__(self):
        self.rows: dict[tuple[UUID, str], Hold] = {}
        self.available = 100_000

    async def get_by_idempotency(self, wallet_id, idempotency_key):
        return self.rows.get((wallet_id, idempotency_key))

    async def create_hold(self, wallet_id, amount, currency, idempotency_key):
        key = (wallet_id, idempotency_key)
        if key in self.rows:
            return self.rows[key]
        if amount > self.available:
            raise InsufficientHoldBalanceError("first-hour balance insufficient")
        self.available -= amount
        hold = Hold(wallet_id, amount, currency, idempotency_key, id=uuid4())
        self.rows[key] = hold
        return hold


class HoldService:
    def __init__(self, repo):
        self.repo = repo

    async def create_hold(self, wallet_id, amount, currency, idempotency_key):
        return await self.repo.create_hold(wallet_id, amount, currency, idempotency_key)


async def checkout(*, currency="IRT", balance=100_000):
    offer = await _usd_offer()
    wallet = Wallets(currency, balance)
    servers = Servers()
    snapshots = Snapshots()
    periods = Periods()
    holds = Holds()
    holds.available = balance
    quotes = Quotes()
    cloud = FakeHourlyProvider()
    billing = PrepaidHourlyBillingService(
        server_repo=servers,
        wallet_repo=wallet,
        ledger_repo=SimpleNamespace(),
        snapshot_repo=snapshots,
        period_repo=periods,
        fx_resolver=quotes,
    )
    service = HourlyCloudService(
        server_repo=servers,
        offers_repo=FakeOffersRepo([offer]),
        account_repo=FakeAccountRepo(),
        wallet_repo=wallet,
        snapshot_service=snapshots,
        operation_repo=FakeOpsRepo(),
        audit_repo=FakeAuditRepo(),
        cloud_providers={offer.provider_key: cloud},
        prepaid_billing=billing,
        period_repo=periods,
        hold_repo=holds,
        hold_service=HoldService(holds),
    )
    return SimpleNamespace(
        service=service,
        offer=offer,
        wallet=wallet,
        servers=servers,
        snapshots=snapshots,
        periods=periods,
        holds=holds,
        quotes=quotes,
        cloud=cloud,
    )


async def buy(setup, key="first-hour"):
    return await setup.service.create_instance(
        user=USER,
        offer_id=setup.offer.id,
        image_id="UBUNTU_24_04",
        image_label="Ubuntu 24.04",
        idempotency_key=key,
        expected_selling_price_minor=setup.offer.selling_price_minor,
        expected_selling_currency="USD",
    )


async def test_reserves_bound_irt_without_changing_usd_offer_snapshot_before_provider_post():
    setup = await checkout()
    result = await buy(setup)
    server = result.server
    period = await setup.periods.get(server.id, server.created_at)
    hold = await setup.holds.get_by_idempotency(setup.wallet.wallet.id, "server-create:first-hour")
    assert server.billing_model == BILLING_MODEL_PREPAID_HOURLY_IRT
    assert (await setup.snapshots.require_snapshot(server.id)).selling_currency == "USD"
    assert (await setup.snapshots.require_snapshot(server.id)).selling_minor == period.usd_minor
    assert period.fx_snapshot.purpose is FxPurpose.CHARGE
    assert (hold.amount, hold.currency, hold.status) == (
        period.irt_minor,
        "IRT",
        HoldStatus.CREATED,
    )
    assert setup.cloud.posts == []
    assert await setup.service.process_server(server.id) == "provisioned"
    assert len(setup.cloud.posts) == 1


async def test_missing_or_mismatched_reservation_never_reaches_provider():
    setup = await checkout()
    server = (await buy(setup)).server
    key = (setup.wallet.wallet.id, "server-create:first-hour")
    hold = setup.holds.rows.pop(key)
    assert await setup.service.process_server(server.id) == "awaiting-first-hour"
    assert setup.cloud.posts == []
    setup.holds.rows[key] = replace(hold, amount=hold.amount + 1)
    assert await setup.service.process_server(server.id) == "awaiting-first-hour"
    assert setup.cloud.posts == []
    setup.holds.rows[key] = replace(hold, status=HoldStatus.RELEASED)
    assert await setup.service.process_server(server.id) == "awaiting-first-hour"
    assert setup.cloud.posts == []


async def test_same_key_replay_repairs_missing_hold_without_new_quote_or_new_server():
    setup = await checkout()
    initial = (await buy(setup)).server
    key = (setup.wallet.wallet.id, "server-create:first-hour")
    first_hold = setup.holds.rows.pop(key)
    setup.holds.available += first_hold.amount
    replay = await buy(setup)
    assert replay.replayed and replay.server.id == initial.id
    assert setup.quotes.calls == 1
    assert setup.holds.rows[key].amount == first_hold.amount
    assert await setup.service.process_server(initial.id) == "provisioned"
    assert len(setup.cloud.posts) == 1


async def test_first_hour_insufficient_balance_prevents_billable_create():
    setup = await checkout(balance=1)
    with pytest.raises(InsufficientHoldBalanceError):
        await buy(setup)
    assert setup.cloud.posts == []
    assert (
        await setup.service.process_server(next(iter(setup.servers.servers)))
        == "awaiting-first-hour"
    )
    assert setup.cloud.posts == []


async def test_usd_wallet_retains_legacy_hourly_without_fx_or_hold():
    setup = await checkout(currency="USD")
    server = (await buy(setup)).server
    assert server.billing_model == "hourly"
    assert setup.quotes.calls == 0
    assert setup.holds.rows == {}
    assert await setup.service.process_server(server.id) == "provisioned"
    assert len(setup.cloud.posts) == 1


async def test_missing_prepaid_dependencies_refuses_checkout_before_persisting_server():
    setup = await checkout()
    setup.service._periods = None
    with pytest.raises(HourlyError, match="period"):
        await buy(setup)
    assert setup.servers.servers == {}
    assert setup.cloud.posts == []
