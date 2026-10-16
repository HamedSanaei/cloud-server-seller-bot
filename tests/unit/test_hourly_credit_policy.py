"""Toman-only prepaid lifecycle, affordability, and stale-control regressions."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from cloud_platform.modules.billing.service import PrepaidBalanceDecision, decide_prepaid_balance
from cloud_platform.modules.compute.domain import ServerLifecycleState
from cloud_platform.modules.fx.domain import FxPurpose
from cloud_platform.modules.operations.service import (
    PowerCommandService,
    PowerControlsService,
    PowerOperationFailedError,
    PrepaidProviderLifecycleOutcome,
)
from cloud_platform.modules.servers.models import ServerOperation
from cloud_platform.modules.servers.service import ServerOperationNotAllowedError
from tests.unit.test_fx_usd_proxy import _resolver
from tests.unit.test_prepaid_hourly_billing import Fx, PeriodStore, Wallets, engine, server
from tests.unit.test_prepaid_provider_lifecycle import Operations, dependencies
from tests.unit.test_servers_management_service import (
    CUSTOMER,
    SERVER_ID,
    make_server,
    make_service,
)


@pytest.mark.parametrize("balance", [0, 999])
def test_insufficient_balance_does_not_truncate_paid_hour(balance):
    now = datetime.now(UTC)
    paid_until = now + timedelta(minutes=25)
    early = decide_prepaid_balance(
        balance, None, None, now, required_minor=1_000, paid_until=paid_until
    )
    assert early.decision is PrepaidBalanceDecision.NONE and early.zero_since is None
    due = decide_prepaid_balance(
        balance, None, None, paid_until, required_minor=1_000, paid_until=paid_until
    )
    assert due.decision is PrepaidBalanceDecision.STOP and due.zero_since == paid_until


def test_partial_topup_does_not_reset_unfunded_retention_clock():
    now = datetime.now(UTC)
    since = now - timedelta(hours=24)
    assert decide_prepaid_balance(999, None, since, now, required_minor=1_000).decision is (
        PrepaidBalanceDecision.DELETE
    )
    outcome = decide_prepaid_balance(1_000, None, since, now, required_minor=1_000)
    assert outcome.zero_since is None


async def test_internal_one_usd_is_one_usdt_with_exact_toman_rial_factor():
    fx = _resolver(allow_usdt_proxy_for_settlement=True)
    toman = await fx.resolve(100, "USD", "IRT", FxPurpose.CHARGE)
    rial = await fx.resolve(100, "USD", "IRR", FxPurpose.CHARGE)
    assert toman.target_amount_minor == 100_000
    assert rial.target_amount_minor == toman.target_amount_minor * 10
    assert toman.proxy and toman.proxy_asset == "USDT"
    assert toman.target_currency == "IRT" and rial.target_currency == "IRR"


async def test_zero_after_successful_renewal_preserves_paid_coverage():
    now = datetime.now(UTC)
    row = server(now)
    wallets, periods = Wallets(row, 250_000), PeriodStore(row)
    service = engine(row, wallets, periods, Fx(100_000))
    result = await service.run(now)
    assert result.charged == 1 and wallets.wallet.balance == 0
    decision = decide_prepaid_balance(
        0, None, None, now, required_minor=250_000, paid_until=row.prepaid_paid_until
    )
    assert decision.decision is PrepaidBalanceDecision.NONE
    assert await service.can_start(row)  # Already paid; no second debit needed.


async def test_resume_after_topup_buys_only_one_hour_not_stopped_gap():
    now = datetime.now(UTC)
    row = server(now - timedelta(days=2), state=ServerLifecycleState.STOPPED)
    wallets, periods = Wallets(row, 249_999), PeriodStore(row)
    service = engine(row, wallets, periods, Fx(100_000))
    assert not await service.can_start(row)
    wallets.wallet.balance = 250_000
    assert await service.can_start(row)
    await service.ensure_customer_coverage(row)
    assert now < row.prepaid_paid_until <= datetime.now(UTC) + timedelta(hours=1)
    assert wallets.wallet.balance == 0 and wallets.debits == 1
    await service.ensure_customer_coverage(replace(row))
    assert wallets.debits == 1


async def test_resume_recovers_commit_before_mark_failure_without_duplicate_charge():
    row = server(datetime.now(UTC) - timedelta(days=1), state=ServerLifecycleState.STOPPED)
    wallets, periods = Wallets(row, 500_000), PeriodStore(row)
    service = engine(row, wallets, periods, Fx(100_000))
    periods.fail_mark_once = True
    with pytest.raises(RuntimeError, match="interrupted"):
        await service.ensure_customer_coverage(row)
    await service.ensure_customer_coverage(row)
    assert wallets.debits == 1 and wallets.wallet.balance == 250_000


async def test_start_visibility_rechecks_credit_and_rejects_stale_buttons():
    row = make_server(state=ServerLifecycleState.STOPPED)
    row.billing_model = "hourly_prepaid_irt"
    service, _provider, power, *_ = make_service(server=row)
    allowed = AsyncMock(return_value=False)
    service._hourly_can_start = allowed
    assert ServerOperation.START not in await service.available_operations(CUSTOMER, SERVER_ID)
    with pytest.raises(ServerOperationNotAllowedError) as error:
        await service.start_server(CUSTOMER, SERVER_ID)
    assert error.value.reason == "insufficient_balance"
    assert power.calls == []
    allowed.return_value = True
    assert ServerOperation.START in await service.available_operations(CUSTOMER, SERVER_ID)


async def test_lifecycle_preserves_paid_zero_and_positive_insufficient_balance_stops():
    now = datetime.now(UTC)
    row = server(now + timedelta(minutes=30))
    row.provider_server_id = "provider-1"
    row.provider_key = "test"
    row.prepaid_zero_since = now - timedelta(hours=24)
    lifecycle, _, _, _, power, delete, _ = dependencies(row, balance=1)
    lifecycle._can_fund_hour = AsyncMock(return_value=False)
    assert await lifecycle.enforce(row.id, now=now) is PrepaidProviderLifecycleOutcome.SKIPPED
    power.power_off.assert_not_awaited()
    row.prepaid_paid_until = now
    assert (
        await lifecycle.enforce(row.id, now=now) is PrepaidProviderLifecycleOutcome.STOP_REQUESTED
    )
    power.power_off.assert_awaited_once()
    delete.request.assert_not_awaited()


async def test_unknown_fx_never_authorizes_deletion_and_sufficient_topup_cancels_it():
    now = datetime.now(UTC)
    row = server(now, state=ServerLifecycleState.STOPPED)
    row.provider_server_id = "provider-1"
    row.provider_key = "test"
    row.prepaid_zero_since = now - timedelta(hours=24)
    lifecycle, _, _, _, _, delete, _ = dependencies(row, status="stopped", balance=500)
    lifecycle._can_fund_hour = AsyncMock(side_effect=RuntimeError("FX unavailable"))
    with pytest.raises(RuntimeError, match="FX unavailable"):
        await lifecycle.enforce(row.id, now=now)
    delete.request.assert_not_awaited()
    lifecycle._can_fund_hour = AsyncMock(return_value=True)
    assert await lifecycle.enforce(row.id, now=now) is PrepaidProviderLifecycleOutcome.SKIPPED
    delete.request.assert_not_awaited()


async def test_direct_power_command_requires_payment_before_provider_and_legacy_panel_hides_start():
    now = datetime.now(UTC)
    row = server(now - timedelta(days=1), state=ServerLifecycleState.STOPPED)
    row.provider_key, row.provider_server_id = "test", "provider-1"
    wallets, periods = Wallets(row, 249_999), PeriodStore(row)
    billing = engine(row, wallets, periods, Fx(100_000))
    _, repo, _, provider, _, _, registry = dependencies(row, status="stopped")
    provider.power_on = AsyncMock()
    command = PowerCommandService(
        server_repo=repo,
        operation_repo=Operations(),
        provider_registry=registry,
        audit_repo=AsyncMock(),
        prepay_server=billing.ensure_customer_coverage,
    )
    panel = PowerControlsService(repo, registry, "test-key", hourly_can_start=billing.can_start)
    view = await panel.detail(row.user_id, row.id)
    assert all(option.action.value != "power_on" for option in view.actions)
    with pytest.raises(PowerOperationFailedError):
        await command.power_on(row.user_id, row.id, "insufficient-request")
    provider.power_on.assert_not_awaited()
    wallets.wallet.balance = 250_000
    view = await panel.detail(row.user_id, row.id)
    assert any(option.action.value == "power_on" for option in view.actions)
    await command.power_on(row.user_id, row.id, "funded-request")
    assert wallets.wallet.balance == 0 and wallets.debits == 1
    provider.power_on.assert_awaited_once()
