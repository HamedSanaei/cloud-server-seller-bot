"""Committed prepaid charges recover independently of compute lifecycle."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_prepaid_hourly_billing import (
    FakeServers,
    Fx,
    PeriodStore,
    Wallets,
    async_value,
    engine,
    server,
)

from cloud_platform.modules.billing.service import (
    PrepaidHourlyBillingService,
    PrepaidHourlyPeriodStatus,
)
from cloud_platform.modules.compute.domain import ServerLifecycleState


@pytest.mark.asyncio
async def test_committed_charge_repairs_after_policy_stops_server():
    now = datetime.now(UTC)
    row = server(now)
    wallets, periods, fx = Wallets(row, 500_000), PeriodStore(row), Fx(100_000)
    service = engine(row, wallets, periods, fx)
    periods.fail_mark_once = True
    first = await service.run(now)
    assert first.stop_requested_server_ids == [row.id]
    assert wallets.wallet.balance == 250_000 and len(wallets.entries) == 1
    assert row.prepaid_paid_until == now
    # Persist the policy's stop before the next worker enumerates running rows.
    row.state = ServerLifecycleState.STOPPED
    fx.unavailable = True
    repaired = await service.run(now + timedelta(minutes=5))
    assert repaired.replayed == 1 and repaired.charged == 0
    assert repaired.errors == {} and repaired.stop_requested_server_ids == []
    assert row.state is ServerLifecycleState.STOPPED
    assert row.prepaid_paid_until == now + timedelta(hours=1)
    assert periods.rows[(row.id, now)].status is PrepaidHourlyPeriodStatus.PAID
    assert wallets.wallet.balance == 250_000 and wallets.debits == len(wallets.entries) == 1
    assert fx.calls == 1
    again = await service.run(now + timedelta(minutes=6))
    assert again.replayed == again.charged == 0


@pytest.mark.asyncio
async def test_corrupt_receipt_fails_closed_without_blocking_other_repair_or_renewal():
    now = datetime.now(UTC)
    rows = [server(now) for _ in range(3)]
    stores, wallets_list, rates = [], [], []
    for row in rows:
        wallets = Wallets(row, 1 if row is rows[2] else 500_000)
        periods = PeriodStore(row)
        fx = Fx(100_000)
        service = engine(row, wallets, periods, fx)
        periods.fail_mark_once = row is not rows[2]
        await service.run(now)
        fx.unavailable = True
        rates.append(fx)
        row.state = ServerLifecycleState.STOPPED
        stores.append(periods)
        wallets_list.append(wallets)
    stores_by_server = {store.server.id: store for store in stores}
    bad_period = stores[0].rows[(rows[0].id, now)]
    bad_wallet = wallets_list[0]
    bad_wallet.entries[bad_period.idempotency_key] = replace(
        bad_wallet.entries[bad_period.idempotency_key], reference_id=str(rows[1].id)
    )

    class Periods:
        async def list_charged_pending(self):
            return [period for store in stores for period in await store.list_charged_pending()]

        async def mark_paid(self, period):
            await stores_by_server[period.server_id].mark_paid(period)

    class Ledger:
        async def get_entry_by_idempotency(self, wallet_id, key):
            wallet = next(wallet for wallet in wallets_list if wallet.wallet.id == wallet_id)
            return await wallet.get_entry_by_idempotency(wallet_id, key)

    running = server(now)
    running_wallets, running_periods, running_fx = (
        Wallets(running, 500_000),
        PeriodStore(running),
        Fx(100_000),
    )
    running_periods.ledger = running_wallets

    # Share the real renewal ports while adding stopped recovery candidates.
    class CombinedPeriods(Periods):
        async def get(self, server_id, start):
            return await running_periods.get(server_id, start)

        async def bind(self, period):
            return await running_periods.bind(period)

        async def mark_paid(self, period):
            if period.server_id == running.id:
                await running_periods.mark_paid(period)
            else:
                await super().mark_paid(period)

    class CombinedLedger(Ledger):
        async def get_entry_by_idempotency(self, wallet_id, key):
            if wallet_id == running_wallets.wallet.id:
                return await running_wallets.get_entry_by_idempotency(wallet_id, key)
            return await super().get_entry_by_idempotency(wallet_id, key)

    service = PrepaidHourlyBillingService(
        server_repo=FakeServers(*rows, running),
        wallet_repo=running_wallets,
        ledger_repo=CombinedLedger(),
        period_repo=CombinedPeriods(),
        fx_resolver=running_fx,
        snapshot_repo=SimpleNamespace(
            get=lambda _: async_value(SimpleNamespace(selling_currency="USD", selling_minor=250))
        ),
    )
    report = await service.run(now)
    assert report.replayed == 1 and report.charged == 1
    assert set(report.errors) == {rows[0].id}
    assert report.stop_requested_server_ids == []
    assert rows[0].prepaid_paid_until == rows[2].prepaid_paid_until == now
    assert rows[1].prepaid_paid_until == running.prepaid_paid_until == now + timedelta(hours=1)
    assert all(row.state is ServerLifecycleState.STOPPED for row in rows)
    assert all(
        wallet.wallet.balance == 250_000 and wallet.debits == 1 for wallet in wallets_list[:2]
    )
    assert wallets_list[2].wallet.balance == 1
    assert wallets_list[2].debits == 0 and not wallets_list[2].entries
    assert all(fx.calls == 1 for fx in rates)
    assert running_fx.calls == 1
    assert running_wallets.wallet.balance == 250_000 and running_wallets.debits == 1
    assert stores[0].rows[(rows[0].id, now)].status is PrepaidHourlyPeriodStatus.PENDING
    assert stores[2].rows[(rows[2].id, now)].status is PrepaidHourlyPeriodStatus.PENDING
