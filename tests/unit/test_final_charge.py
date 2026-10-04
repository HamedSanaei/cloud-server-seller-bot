"""Deleting prepaid hourly service never purchases a trailing usage slice."""

from datetime import timedelta

import pytest

from cloud_platform.modules.billing.service import FinalChargeService, MissingSnapshotError
from cloud_platform.modules.compute.domain import (
    BILLING_MODEL_PREPAID_MONTHLY,
    ServerLifecycleState,
)
from cloud_platform.modules.wallet.domain import HoldStatus
from tests.unit.test_accrual_job import (
    CAPTURE_KEY,
    HOLD_KEY,
    SELLING,
    T0,
    Harness,
    _hold,
    _server,
)


def _service(h: Harness) -> FinalChargeService:
    return FinalChargeService(
        server_repo=h,
        wallet_repo=h._WalletRepo(h),
        hold_repo=h._HoldRepo(h),
        hold_service=h._HoldService(h),
        ledger_repo=h._LedgerRepo(h),
        accrual_repo=h._AccrualRepo(h),
        snapshot_repo=h._SnapshotRepo(h),
        audit_repo=h.audit,
    )


@pytest.mark.parametrize("minutes", [1, 30, 59, 60])
async def test_deletion_within_or_at_paid_hour_never_debits_again(minutes):
    server = _server()
    h = Harness(servers=[server], holds={HOLD_KEY: _hold(HOLD_KEY)})
    await h.make_job().prepay_server(server, T0)
    balance = h.wallet.balance
    server.state = ServerLifecycleState.DELETED
    service = _service(h)
    for _ in range(2):
        result = await service.charge_final(server, T0 + timedelta(minutes=minutes))
        assert result.charged_minor == 0
        assert not result.captured_hold
        assert h.wallet.balance == balance
        assert len(h.entries) == 1
        assert h.captured == [HOLD_KEY]
        assert server.last_accrued_at == T0 + timedelta(hours=1)


async def test_deletion_never_purchases_an_unpaid_remainder():
    server = _server()
    h = Harness(servers=[server], holds={HOLD_KEY: _hold(HOLD_KEY)})
    await h.make_job().prepay_server(server, T0)
    balance = h.wallet.balance
    server.state = ServerLifecycleState.DELETED
    result = await _service(h).charge_final(server, T0 + timedelta(hours=5))
    assert result.charged_minor == 0
    assert h.wallet.balance == balance
    assert len(h.entries) == 1


async def test_pre_activation_deletion_releases_unused_reservation():
    server = _server(state=ServerLifecycleState.DELETED, billing_started_at=None)
    h = Harness(servers=[server], holds={HOLD_KEY: _hold(HOLD_KEY)})
    for _ in range(2):
        result = await _service(h).charge_final(server, T0 + timedelta(minutes=30))
        assert result.charged_minor == 0
    assert h.holds[HOLD_KEY].status is HoldStatus.RELEASED
    assert h.wallet.balance == 100_000
    assert h.captured == []


async def test_capture_with_lost_bookkeeping_is_repaired_without_new_money():
    server = _server()
    h = Harness(servers=[server], holds={HOLD_KEY: _hold(HOLD_KEY)})
    await h.capture_hold(h.wallet.id, h.holds[HOLD_KEY].id, HOLD_KEY)
    assert h.accrual_rows == {}
    balance = h.wallet.balance
    server.state = ServerLifecycleState.DELETED
    result = await _service(h).charge_final(server, T0 + timedelta(minutes=30))
    assert result.replayed
    assert result.charged_minor == 0
    assert h.wallet.balance == balance
    assert h.accrual_rows[CAPTURE_KEY].selling_minor == SELLING
    assert server.last_accrued_at == T0 + timedelta(hours=1)


async def test_repair_never_regresses_later_paid_through():
    server = _server()
    h = Harness(servers=[server], holds={HOLD_KEY: _hold(HOLD_KEY)})
    await h.make_job().prepay_server(server, T0 + timedelta(hours=2))
    assert server.last_accrued_at == T0 + timedelta(hours=3)
    server.state = ServerLifecycleState.DELETED
    await _service(h).charge_final(server, T0 + timedelta(hours=2, minutes=30))
    assert server.last_accrued_at == T0 + timedelta(hours=3)
    assert h.wallet.balance == 100_000 - 3 * SELLING


async def test_captured_hold_still_requires_original_snapshot():
    server = _server()
    h = Harness(servers=[server], holds={HOLD_KEY: _hold(HOLD_KEY)})
    await h.make_job().prepay_server(server, T0)
    h.snapshots.clear()
    server.state = ServerLifecycleState.DELETED
    with pytest.raises(MissingSnapshotError):
        await _service(h).charge_final(server, T0 + timedelta(minutes=30))
    assert len(h.entries) == 1


async def test_monthly_final_settlement_is_excluded():
    server = _server(
        state=ServerLifecycleState.DELETED, billing_model=BILLING_MODEL_PREPAID_MONTHLY
    )
    h = Harness(servers=[server], holds={HOLD_KEY: _hold(HOLD_KEY)})
    result = await _service(h).charge_final(server, T0 + timedelta(minutes=30))
    assert result.charged_minor == 0
    assert h.holds[HOLD_KEY].status is HoldStatus.CREATED
    assert h.entries == {}


async def test_deletion_state_and_chronology_are_required():
    server = _server()
    h = Harness(servers=[server])
    with pytest.raises(ValueError, match="DELETED"):
        await _service(h).charge_final(server, T0)
    server.state = ServerLifecycleState.DELETED
    with pytest.raises(ValueError, match="precede"):
        await _service(h).charge_final(server, T0 - timedelta(seconds=1))
