"""Audit regressions at the two hourly-billing models' shared boundary."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from cloud_platform.modules.billing.repository import SqlAlchemyPrepaidHourlyPeriodRepository
from cloud_platform.modules.billing.service import PrepaidHourlyPeriod, prepaid_hourly_key
from cloud_platform.modules.compute.domain import ServerLifecycleState
from cloud_platform.modules.fx.domain import ConversionSnapshot, FxPurpose
from cloud_platform.modules.operations.service import (
    PowerCommandService,
    PowerOperationFailedError,
)
from cloud_platform.modules.wallet.domain import Hold
from cloud_platform.providers.registry import ProviderRegistry
from tests.unit.test_hourly_prepayment import FakeOpRepo, PowerProvider, StateServers
from tests.unit.test_prepaid_hourly_billing import Fx, PeriodStore, Wallets, engine, server


def _period() -> PrepaidHourlyPeriod:
    start = datetime.now(UTC).replace(microsecond=0)
    server_id = uuid4()
    return PrepaidHourlyPeriod(
        server_id=server_id,
        wallet_id=uuid4(),
        period_start=start,
        period_end=start + timedelta(hours=1),
        usd_minor=250,
        irt_minor=250_000,
        fx_snapshot=ConversionSnapshot(
            source_amount_minor=250,
            source_currency="USD",
            target_amount_minor=250_000,
            target_currency="IRT",
            rate=Decimal(100_000),
            purpose=FxPurpose.CHARGE,
            source="abantether",
            path="USDTIRT.buy USD/USDT=1",
            observed_at=start,
            expires_at=start + timedelta(minutes=5),
            proxy=True,
            proxy_asset="USDT",
        ),
        idempotency_key=prepaid_hourly_key(server_id, start),
    )


@pytest.mark.parametrize("field", ["usd_minor", "irt_minor"])
def test_prepaid_money_rejects_bigint_overflow_before_persistence(field):
    with pytest.raises(ValueError, match="signed int64"):
        replace(_period(), **{field: 1 << 63})


def test_prepaid_period_normalizes_aware_boundaries_without_changing_key():
    original = _period()
    local = timezone(timedelta(hours=3, minutes=30))
    converted = replace(
        original,
        period_start=original.period_start.astimezone(local),
        period_end=original.period_end.astimezone(local),
    )
    assert converted == original
    assert converted.period_start.tzinfo is UTC
    assert converted.period_end.tzinfo is UTC


async def test_prepaid_lookup_uses_actual_utc_instant_not_local_wall_clock():
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute.return_value = result
    context = AsyncMock()
    context.__aenter__.return_value = session
    repository = SqlAlchemyPrepaidHourlyPeriodRepository(lambda: context)
    start = _period().period_start
    local = start.astimezone(timezone(timedelta(hours=3, minutes=30)))
    assert await repository.get(uuid4(), local) is None
    statement = session.execute.call_args.args[0]
    values = statement.compile().params.values()
    assert start.replace(tzinfo=None) in values
    session.execute.reset_mock()
    with pytest.raises(ValueError, match="timezone-aware"):
        await repository.get(uuid4(), start.replace(tzinfo=None))
    session.execute.assert_not_awaited()


@pytest.mark.parametrize("wrong_fact", ["wallet_id", "idempotency_key"])
async def test_irt_capture_refuses_wrong_reservation_identity_before_mutation(wrong_fact):
    row = server(datetime.now(UTC))
    row.prepaid_paid_until = None
    wallets, periods = Wallets(row, 300_000), PeriodStore(row)
    billing = engine(row, wallets, periods, Fx(100_000))
    period = await billing.prepare_first_hour(row, wallets.wallet.id, row.created_at, 250)
    hold = Hold(
        wallet_id=wallets.wallet.id,
        amount=period.irt_minor,
        currency="IRT",
        idempotency_key=f"server-create:{row.idempotency_key}",
        id=uuid4(),
    )
    setattr(hold, wrong_fact, uuid4() if wrong_fact == "wallet_id" else "different-key")
    holds = SimpleNamespace(get_by_idempotency=AsyncMock(return_value=hold))
    mutations = SimpleNamespace(capture_hold=AsyncMock())
    with pytest.raises(ValueError, match="reservation"):
        await billing.capture_first_hour(row, holds, mutations)
    mutations.capture_hold.assert_not_awaited()
    assert wallets.wallet.balance == 300_000 and row.prepaid_paid_until is None


@pytest.mark.parametrize("action", ["power_on", "reboot"])
@pytest.mark.parametrize("funded", [False, True])
async def test_irt_provider_power_action_requires_paid_coverage(action, funded):
    now = datetime.now(UTC)
    row = server(now)
    row.provider_server_id = "provider-resource"
    row.prepaid_paid_until = now + timedelta(hours=1) if funded else now - timedelta(seconds=1)
    if action == "power_on":
        row.state = ServerLifecycleState.STOPPED
    provider = PowerProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    # The test provider owns the same provider-neutral account route.
    row.provider_key = provider.key
    prepay = AsyncMock()
    command = PowerCommandService(
        server_repo=StateServers([row]),
        operation_repo=FakeOpRepo(),
        provider_registry=registry,
        audit_repo=AsyncMock(),
        prepay_server=prepay,
    )
    if funded:
        await getattr(command, action)(row.user_id, row.id, f"irt:{action}")
        assert len(provider.calls) == 1
    else:
        with pytest.raises(PowerOperationFailedError, match="insufficient balance"):
            await getattr(command, action)(row.user_id, row.id, f"irt:{action}")
        assert provider.calls == []
    # Never run the same-currency USD settlement against an IRT wallet.
    prepay.assert_not_awaited()
