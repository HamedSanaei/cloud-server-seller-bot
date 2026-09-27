"""Prepaid hourly money and lifecycle invariants (no external provider calls)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cloud_platform.core.money import Money
from cloud_platform.modules.billing.service import (
    PrepaidBalanceDecision,
    PrepaidBalancePolicyService,
    PrepaidHourlyBillingService,
    PrepaidHourlyPeriodStatus,
    decide_prepaid_balance,
)
from cloud_platform.modules.compute.domain import CloudServer, ServerLifecycleState
from cloud_platform.modules.fx.domain import ConversionSnapshot, FxPurpose
from cloud_platform.modules.wallet.domain import (
    Hold,
    HoldStatus,
    LedgerEntry,
    LedgerEntryType,
    Wallet,
)


def server(
    now: datetime, *, state: ServerLifecycleState = ServerLifecycleState.RUNNING
) -> CloudServer:
    return CloudServer(
        id=uuid4(),
        user_id=uuid4(),
        provider_key="hetzner",
        provider_account_id=uuid4(),
        state=state,
        idempotency_key="purchase-key",
        billing_model="hourly_prepaid_irt",
        created_at=now - timedelta(hours=1),
        prepaid_paid_until=now,
    )


class PeriodStore:
    def __init__(self, server_row: CloudServer):
        self.server = server_row
        self.rows = {}
        self.fail_mark_once = False

    async def get(self, server_id, start):
        return self.rows.get((server_id, start))

    async def bind(self, period):
        key = (period.server_id, period.period_start)
        if key in self.rows:
            prior = self.rows[key]
            assert prior.usd_minor == period.usd_minor and prior.wallet_id == period.wallet_id
            return prior
        self.rows[key] = period
        return period

    async def mark_paid(self, period):
        if self.fail_mark_once:
            self.fail_mark_once = False
            raise RuntimeError("database write interrupted after committed ledger")
        assert self.rows[(period.server_id, period.period_start)].fx_snapshot == period.fx_snapshot
        assert self.server.prepaid_paid_until in (None, period.period_start)
        self.rows[(period.server_id, period.period_start)] = replace(
            period, status=PrepaidHourlyPeriodStatus.PAID
        )
        self.server.prepaid_paid_until = period.period_end


class Wallets:
    def __init__(self, row: CloudServer, balance: int):
        self.wallet = Wallet(user_id=row.user_id, id=uuid4(), balance=balance, currency="IRT")
        self.entries = {}
        self.debits = 0

    async def get(self, user_id):
        assert user_id == self.wallet.user_id
        return self.wallet

    async def get_entry_by_idempotency(self, wallet_id, key):
        assert wallet_id == self.wallet.id
        return self.entries.get(key)

    async def adjust(
        self,
        user_id,
        delta,
        key,
        *,
        entry_type,
        reference_type,
        reference_id,
        description,
        expected_currency,
    ):
        assert user_id == self.wallet.user_id and expected_currency == "IRT"
        if key in self.entries:
            return self.wallet, False
        if self.wallet.balance + delta < 0:
            raise ValueError("insufficient IRT for upcoming hour")
        self.wallet.balance += delta
        self.debits += 1
        self.entries[key] = LedgerEntry(
            id=uuid4(),
            wallet_id=self.wallet.id,
            entry_type=entry_type,
            amount=Money(Decimal(-delta), "IRT"),
            reference_type=reference_type,
            reference_id=reference_id,
            idempotency_key=key,
            description=description,
        )
        return self.wallet, True


class Fx:
    def __init__(self, rate: int, *, unavailable: bool = False):
        self.rate = rate
        self.unavailable = unavailable
        self.calls = 0

    async def snapshot(self, amount, source, target, purpose):
        self.calls += 1
        assert (source, target, purpose) == ("USD", "IRT", FxPurpose.CHARGE)
        if self.unavailable:
            raise RuntimeError("AbanTether offline")
        at = datetime.now(UTC)
        return ConversionSnapshot(
            source_amount_minor=amount,
            source_currency="USD",
            target_amount_minor=amount * self.rate // 100,
            target_currency="IRT",
            rate=Decimal(self.rate),
            purpose=purpose,
            source="abantether",
            path="USDTIRT.buy (proxy USDT for USD)",
            observed_at=at,
            expires_at=at + timedelta(minutes=5),
            proxy=True,
            proxy_asset="USDT",
        )


def engine(row, wallets, periods, fx):
    return PrepaidHourlyBillingService(
        server_repo=SimpleNamespace(list_running=lambda: async_value([row])),
        wallet_repo=wallets,
        ledger_repo=wallets,
        snapshot_repo=SimpleNamespace(
            get=lambda _: async_value(
                SimpleNamespace(
                    selling_currency="USD",
                    selling_minor=250,
                )
            )
        ),
        period_repo=periods,
        fx_resolver=fx,
    )


async def async_value(value):
    return value


@pytest.mark.asyncio
async def test_next_hour_charge_recovers_after_ledger_commits_without_requoting():
    now = datetime.now(UTC)
    row = server(now)
    wallets, periods, fx = Wallets(row, 500_000), PeriodStore(row), Fx(100_000)
    service = engine(row, wallets, periods, fx)
    periods.fail_mark_once = True
    first = await service.run(now)
    assert first.stop_requested_server_ids == [row.id]
    assert wallets.wallet.balance == 250_000 and wallets.debits == 1
    assert row.prepaid_paid_until == now
    fx.unavailable = True
    replay = await service.run(now)
    assert replay.replayed == 1 and replay.stop_requested_server_ids == []
    assert wallets.wallet.balance == 250_000 and wallets.debits == 1
    assert row.prepaid_paid_until == now + timedelta(hours=1)
    assert fx.calls == 1
    period = periods.rows[(row.id, now)]
    assert period.fx_snapshot.to_dict()["proxy_asset"] == "USDT"
    assert period.status is PrepaidHourlyPeriodStatus.PAID


@pytest.mark.asyncio
async def test_outage_or_insufficient_funds_requests_stop_without_unpaid_coverage():
    now = datetime.now(UTC)
    row = server(now)
    wallets, periods, fx = Wallets(row, 1), PeriodStore(row), Fx(100_000, unavailable=True)
    service = engine(row, wallets, periods, fx)
    outage = await service.run(now)
    assert outage.stop_requested_server_ids == [row.id]
    assert not periods.rows and wallets.debits == 0 and row.prepaid_paid_until == now
    fx.unavailable = False
    insufficient = await service.run(now)
    assert insufficient.stop_requested_server_ids == [row.id]
    assert wallets.debits == 0 and row.prepaid_paid_until == now
    assert periods.rows[(row.id, now)].irt_minor == 250_000


@pytest.mark.asyncio
async def test_renewal_failure_before_expiry_preserves_paid_interval_until_due():
    expiry = datetime.now(UTC) + timedelta(seconds=30)
    row = server(expiry)
    wallets, periods = Wallets(row, 500_000), PeriodStore(row)
    service = engine(row, wallets, periods, Fx(100_000, unavailable=True))
    early = await service.run(expiry - timedelta(seconds=30))
    assert row.id in early.errors and not early.stop_requested_server_ids
    due = await service.run(expiry)
    assert due.stop_requested_server_ids == [row.id]
    assert wallets.debits == 0 and row.prepaid_paid_until == expiry


@pytest.mark.asyncio
async def test_checkout_capture_requires_matching_hold_and_replays_once():
    now = datetime.now(UTC)
    row = server(now)
    row.created_at = now
    row.prepaid_paid_until = None
    wallets, periods = Wallets(row, 300_000), PeriodStore(row)
    service = engine(row, wallets, periods, Fx(100_000))
    first = await service.prepare_first_hour(row, wallets.wallet.id, row.created_at, 250)
    hold = Hold(
        wallet_id=wallets.wallet.id,
        amount=first.irt_minor,
        currency="IRT",
        idempotency_key="server-create:purchase-key",
        id=uuid4(),
    )

    class Holds:
        async def get_by_idempotency(self, wallet_id, key):
            assert wallet_id == wallets.wallet.id and key == hold.idempotency_key
            return hold

        async def capture_hold(self, wallet_id, hold_id, key):
            assert wallet_id == wallets.wallet.id and hold_id == hold.id
            if hold.status is HoldStatus.CREATED:
                hold.capture()
                wallets.wallet.balance -= hold.amount
            wallets.entries[f"capture-{key}"] = LedgerEntry(
                id=uuid4(),
                wallet_id=wallet_id,
                entry_type=LedgerEntryType.CHARGE,
                amount=Money(Decimal(hold.amount), "IRT"),
                reference_type="hold",
                reference_id=str(hold.id),
                idempotency_key=f"capture-{key}",
                description=f"hold captured for {key}",
            )
            return hold

    holds = Holds()
    hold.amount += 1
    with pytest.raises(ValueError, match="reservation"):
        await service.capture_first_hour(row, holds, holds)
    assert wallets.wallet.balance == 300_000 and row.prepaid_paid_until is None
    hold.amount -= 1
    await service.capture_first_hour(row, holds, holds)
    assert wallets.wallet.balance == 50_000
    assert row.prepaid_paid_until == row.created_at + timedelta(hours=1)
    assert periods.rows[(row.id, row.created_at)].status is PrepaidHourlyPeriodStatus.PAID


def test_zero_episode_is_independent_of_warning_and_resets_only_on_replenishment():
    now = datetime.now(UTC)
    warning = decide_prepaid_balance(199_999, None, None, now)
    assert warning.decision is PrepaidBalanceDecision.WARN
    assert decide_prepaid_balance(200_000, None, None, now).decision is PrepaidBalanceDecision.NONE
    zero = decide_prepaid_balance(0, warning.warning_since, None, now + timedelta(hours=30))
    assert zero.decision is PrepaidBalanceDecision.STOP
    assert zero.zero_since == now + timedelta(hours=30)
    assert (
        decide_prepaid_balance(
            0, zero.warning_since, zero.zero_since, now + timedelta(hours=53, minutes=59)
        ).decision
        is PrepaidBalanceDecision.STOP
    )
    assert (
        decide_prepaid_balance(
            0, zero.warning_since, zero.zero_since, now + timedelta(hours=54)
        ).decision
        is PrepaidBalanceDecision.DELETE
    )
    refill = decide_prepaid_balance(
        5_000, zero.warning_since, zero.zero_since, now + timedelta(hours=54)
    )
    assert refill.zero_since is None
    assert refill.decision is PrepaidBalanceDecision.NONE
    healthy = decide_prepaid_balance(200_000, refill.warning_since, None, now + timedelta(hours=55))
    assert healthy.decision is PrepaidBalanceDecision.RECOVERED
    assert healthy.warning_since is None


@pytest.mark.asyncio
async def test_stopped_zero_server_keeps_zero_watermark_until_deletion_request():
    now = datetime.now(UTC)
    row = server(now, state=ServerLifecycleState.STOPPED)
    row.prepaid_zero_since = now - timedelta(hours=24)
    wallets = Wallets(row, 0)
    saved = []

    async def save_markers(
        server_id, *, expected_warning_since, expected_zero_since, warning_since, zero_since
    ):
        assert server_id == row.id and expected_warning_since is None
        assert expected_zero_since == row.prepaid_zero_since
        saved.append(zero_since)

    policy = PrepaidBalancePolicyService(
        SimpleNamespace(
            list_running=lambda: async_value([]),
            list_stopped=lambda: async_value([row]),
            save_prepaid_balance_markers=save_markers,
        ),
        wallets,
    )
    result = await policy.evaluate(now)
    assert result.delete_requested_server_ids == [row.id]
    assert row.prepaid_zero_since == now - timedelta(hours=24)
    assert saved == [row.prepaid_zero_since]
    assert not result.errors
