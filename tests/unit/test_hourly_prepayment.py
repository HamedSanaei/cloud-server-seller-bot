"""Consumer-level activation, boundary, suspension, power and creation tests."""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cloud_platform.modules.billing.service import LowBalancePolicyConfig, LowBalancePolicyService
from cloud_platform.modules.compute.domain import (
    BILLING_MODEL_PREPAID_MONTHLY,
    ServerLifecycleState,
)
from cloud_platform.modules.hourly.service import HourlyCloudService
from cloud_platform.modules.operations.service import (
    PowerCommandService,
    PowerOperationFailedError,
    ServerStateReconciler,
    StateReconciliationOutcome,
)
from cloud_platform.modules.users.identity import IdentityRequiredError
from cloud_platform.modules.wallet.domain import HoldStatus, InsufficientHoldBalanceError
from cloud_platform.providers.errors import ProviderError, ProviderOutcomeUnknown
from cloud_platform.providers.registry import ProviderRegistry
from tests.unit.hourly_money import hourly_money
from tests.unit.test_accrual_job import (
    CAPTURE_KEY,
    HOLD_KEY,
    IK,
    SELLING,
    T0,
    Harness,
    _hold,
    _server,
)
from tests.unit.test_hourly_cloud_flow import (
    PROVIDER,
    USER,
    FakeAccountRepo,
    FakeAuditRepo,
    FakeHourlyProvider,
    FakeOffersRepo,
    FakeOpsRepo,
    FakeServerRepo,
    FakeSnapshots,
    FakeWalletRepo2,
    _usd_offer,
)
from tests.unit.test_power_commands import FakeOpRepo
from tests.unit.test_power_commands import FakeProvider as PowerProvider
from tests.unit.test_server_state_reconciler import FakeProvider as StateProvider
from tests.unit.test_server_state_reconciler import FakeServerRepo as StateServers


async def test_activation_anchor_excludes_creation_and_provisioning_wait():
    server = _server(state=ServerLifecycleState.PROVISIONING, billing_started_at=None)
    h = Harness(servers=[server], holds={HOLD_KEY: _hold(HOLD_KEY)})
    activation = T0 + timedelta(hours=4, microseconds=123456)
    job = h.make_job()
    await job.prepay_server(server, activation)
    assert server.billing_started_at == activation
    assert server.last_accrued_at == activation + timedelta(hours=1)
    assert h.wallet.balance == 100_000 - SELLING
    assert h.accrual_rows[CAPTURE_KEY].period_start == activation
    await job.prepay_server(server, activation + timedelta(minutes=59))
    assert len(h.entries) == 1
    await job.prepay_server(server, activation + timedelta(hours=1))
    assert len(h.entries) == 2
    assert h.wallet.balance == 100_000 - 2 * SELLING
    await job.prepay_server(server, activation + timedelta(hours=1))
    assert len(h.entries) == 2


async def test_stale_activation_cannot_overwrite_committed_anchor_or_paid_hour():
    persisted = _server(state=ServerLifecycleState.PROVISIONING, billing_started_at=None)
    first, stale = replace(persisted), replace(persisted)
    h = Harness(servers=[persisted], holds={HOLD_KEY: _hold(HOLD_KEY)})
    activation = T0 + timedelta(seconds=30, microseconds=123456)
    await h.make_job().prepay_server(first, activation)
    await h.make_job().prepay_server(stale, activation + timedelta(seconds=15))
    assert stale.billing_started_at == activation
    assert stale.last_accrued_at == activation + timedelta(hours=1)
    assert h.wallet.balance == 100_000 - SELLING
    assert len(h.entries) == 1


async def test_renewal_prepays_before_second_precision_boundary():
    server = _server()
    h = Harness(servers=[server], holds={HOLD_KEY: _hold(HOLD_KEY)})
    await h.make_job().prepay_server(server, T0)
    boundary = T0 + timedelta(hours=1)
    await h.make_job().prepay_server(server, boundary - timedelta(seconds=5), renew_ahead_seconds=5)
    assert server.last_accrued_at == boundary + timedelta(hours=1)
    assert h.wallet.balance == 100_000 - 2 * SELLING


async def test_early_renewal_shortage_preserves_paid_time_until_expiry():
    from cloud_platform.modules.wallet.domain import InsufficientBalanceError

    server = _server()
    h = Harness(servers=[server], holds={HOLD_KEY: _hold(HOLD_KEY)})
    h.wallet.balance = SELLING
    await h.make_job().prepay_server(server, T0)
    boundary = T0 + timedelta(hours=1)
    with pytest.raises(InsufficientBalanceError):
        await h.make_job().prepay_server(
            server, boundary - timedelta(seconds=5), renew_ahead_seconds=5
        )
    assert server.state is ServerLifecycleState.RUNNING
    assert server.last_accrued_at == boundary
    with pytest.raises(InsufficientBalanceError):
        await h.make_job().prepay_server(server, boundary, renew_ahead_seconds=5)
    assert server.state is ServerLifecycleState.DELETE_REQUESTED


async def test_stopped_provider_resource_still_prepays_each_hour():
    server = _server()
    h = Harness(servers=[server], holds={HOLD_KEY: _hold(HOLD_KEY)})
    await h.make_job().prepay_server(server, T0)
    server.state = ServerLifecycleState.STOPPED
    report = await h.make_job().run(T0 + timedelta(hours=1))
    assert report.periods_posted == 1
    assert server.last_accrued_at == T0 + timedelta(hours=2)
    assert server.state is ServerLifecycleState.STOPPED
    assert h.wallet.balance == 100_000 - 2 * SELLING


async def test_insufficient_next_hour_requests_existing_delete_saga():
    server = _server()
    h = Harness(servers=[server], holds={HOLD_KEY: _hold(HOLD_KEY)})
    h.wallet.balance = SELLING
    await h.make_job().prepay_server(server, T0)
    before = await h.make_job().run(T0 + timedelta(minutes=59))
    assert before.insufficient_balance == 0
    assert server.state is ServerLifecycleState.RUNNING
    report = await h.make_job().run(T0 + timedelta(hours=1))
    assert report.insufficient_balance == 1
    assert server.state is ServerLifecycleState.DELETE_REQUESTED
    assert server.last_accrued_at == T0 + timedelta(hours=1)
    assert h.wallet.balance == 0
    assert len(h.entries) == 1
    again = await h.make_job().run(T0 + timedelta(hours=1))
    assert again.servers_checked == 0


async def test_low_balance_threshold_never_deletes_paid_time():
    server = _server()
    h = Harness(servers=[server], holds={HOLD_KEY: _hold(HOLD_KEY)})
    h.wallet.balance = SELLING
    await h.make_job().prepay_server(server, T0)
    policy = LowBalancePolicyService(
        server_repo=h, wallet_repo=h._WalletRepo(h), audit_repo=h.audit
    )
    config = LowBalancePolicyConfig(threshold_minor=SELLING, grace_hours=0)
    await policy.evaluate(config, T0)
    report = await policy.evaluate(config, T0 + timedelta(minutes=59))
    assert report.auto_delete == 0
    assert report.grace == 1
    assert server.state is ServerLifecycleState.RUNNING


async def test_monthly_is_excluded_from_activation_and_periodic_prepayment():
    server = _server(billing_model=BILLING_MODEL_PREPAID_MONTHLY, billing_started_at=None)
    h = Harness(servers=[server], holds={HOLD_KEY: _hold(HOLD_KEY)})
    await h.make_job().prepay_server(server, T0)
    report = await h.make_job().run(T0 + timedelta(hours=20))
    assert report.servers_checked == 0
    assert server.billing_started_at is None
    assert h.holds[HOLD_KEY].status is HoldStatus.CREATED
    assert h.entries == {}


async def test_state_reconciler_pays_before_running_and_customer_delivery():
    server = _server(state=ServerLifecycleState.PROVISIONING, billing_started_at=None)
    h = Harness(servers=[server], holds={HOLD_KEY: _hold(HOLD_KEY)})
    registry = ProviderRegistry()
    registry.register(StateProvider())
    job = h.make_job()
    activation = T0 + timedelta(hours=3)
    reconciler = ServerStateReconciler(
        server_repo=StateServers([server]),
        provider_registry=registry,
        audit_repo=h.audit,
        prepay_server=lambda current: job.prepay_server(current, activation),
    )

    async def delivered(current):
        assert current.state is ServerLifecycleState.RUNNING
        assert current.last_accrued_at == activation + timedelta(hours=1)
        assert CAPTURE_KEY in h.entries

    reconciler._notify_hourly_customer = AsyncMock(side_effect=delivered)
    assert await reconciler.reconcile() == {StateReconciliationOutcome.REPAIRED: 1}
    assert await reconciler.reconcile() == {StateReconciliationOutcome.CONSISTENT: 1}
    assert len(h.entries) == 1


async def test_reconciliation_never_delivers_when_prepayment_fails():
    server = _server(state=ServerLifecycleState.PROVISIONING, billing_started_at=None)
    h = Harness(servers=[server])
    h.wallet.balance = 0
    registry = ProviderRegistry()
    registry.register(StateProvider())
    reconciler = ServerStateReconciler(
        server_repo=StateServers([server]),
        provider_registry=registry,
        audit_repo=h.audit,
        prepay_server=lambda current: h.make_job().prepay_server(current, T0),
    )
    reconciler._notify_hourly_customer = AsyncMock()
    assert await reconciler.reconcile() == {StateReconciliationOutcome.INCONCLUSIVE: 1}
    assert server.state is ServerLifecycleState.DELETE_REQUESTED
    reconciler._notify_hourly_customer.assert_not_awaited()
    assert h.entries == {}


@pytest.mark.parametrize("action", ["power_on", "reboot"])
async def test_power_resume_and_reboot_cannot_send_an_unpaid_provider_action(action):
    server = _server()
    h = Harness(servers=[server], holds={HOLD_KEY: _hold(HOLD_KEY)})
    h.wallet.balance = SELLING
    await h.make_job().prepay_server(server, T0)
    if action == "power_on":
        server.state = ServerLifecycleState.STOPPED
    provider = PowerProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    service = PowerCommandService(
        server_repo=StateServers([server]),
        operation_repo=FakeOpRepo(),
        provider_registry=registry,
        audit_repo=h.audit,
        prepay_server=lambda current: h.make_job().prepay_server(current, T0 + timedelta(hours=1)),
    )
    with pytest.raises(PowerOperationFailedError, match="insufficient balance"):
        await getattr(service, action)(server.user_id, server.id, "next-hour-command")
    assert provider.calls == []
    assert h.wallet.balance == 0
    assert server.state is ServerLifecycleState.DELETE_REQUESTED


async def _hourly_order():
    offer = await _usd_offer()
    cloud = FakeHourlyProvider()
    money = hourly_money()
    service = HourlyCloudService(
        server_repo=FakeServerRepo(),
        offers_repo=FakeOffersRepo([offer]),
        account_repo=FakeAccountRepo(),
        wallet_repo=FakeWalletRepo2(),
        snapshot_service=FakeSnapshots(),
        operation_repo=FakeOpsRepo(),
        audit_repo=FakeAuditRepo(),
        cloud_providers={PROVIDER: cloud},
        **money,
    )
    return offer, cloud, service, money


async def test_hourly_request_reserves_first_hour_before_provider_post_and_replay():
    offer, cloud, service, money = await _hourly_order()
    args = dict(
        user=USER,
        offer_id=offer.id,
        image_id="UBUNTU_24_04",
        image_label="Ubuntu",
        idempotency_key=IK,
    )
    result = await service.create_instance(**args)
    replay = await service.create_instance(**args)
    assert replay.server.id == result.server.id
    holds = money["hold_repo"].holds
    assert len(holds) == 1
    assert holds[HOLD_KEY].amount == offer.selling_price_minor
    assert holds[HOLD_KEY].status is HoldStatus.CREATED
    assert cloud.posts == []
    money["prepay_server"].assert_not_awaited()


@pytest.mark.parametrize("ambiguous", [False, True])
async def test_failed_creation_releases_hold_only_when_provider_outcome_is_known(ambiguous):
    offer, cloud, service, money = await _hourly_order()
    result = await service.create_instance(
        user=USER,
        offer_id=offer.id,
        image_id="UBUNTU_24_04",
        image_label="Ubuntu",
        idempotency_key=IK,
    )

    async def fail(**kwargs):
        assert money["hold_repo"].holds[HOLD_KEY].status is HoldStatus.CREATED
        if ambiguous:
            raise ProviderOutcomeUnknown("uncertain create")
        raise ProviderError("definitive refusal")

    cloud.create_instance = fail
    outcome = await service.process_server(result.server.id)
    assert outcome == ("outcome-unknown" if ambiguous else "failed")
    assert money["hold_repo"].holds[HOLD_KEY].status is (
        HoldStatus.CREATED if ambiguous else HoldStatus.RELEASED
    )
    money["prepay_server"].assert_not_awaited()


async def test_insufficient_reservation_prevents_provider_creation():
    offer, cloud, service, money = await _hourly_order()
    money["hold_service"].create_hold = AsyncMock(
        side_effect=InsufficientHoldBalanceError("short wallet")
    )
    with pytest.raises(InsufficientHoldBalanceError):
        await service.create_instance(
            user=USER,
            offer_id=offer.id,
            image_id="UBUNTU_24_04",
            image_label="Ubuntu",
            idempotency_key=IK,
        )
    assert cloud.posts == []
    assert money["hold_repo"].holds == {}
    assert service._ops.ops == {}


async def test_missing_identity_rejected_before_hourly_wallet_or_provider_effects():
    offer, cloud, service, money = await _hourly_order()
    service._wallets.get = AsyncMock()
    with pytest.raises(IdentityRequiredError):
        await service.create_instance(
            user=replace(USER, phone_verified_at=None),
            offer_id=offer.id,
            image_id="UBUNTU_24_04",
            image_label="Ubuntu",
            idempotency_key=IK,
        )
    service._wallets.get.assert_not_awaited()
    assert service._servers.servers == {}
    assert money["hold_repo"].holds == {}
    assert cloud.posts == []


@pytest.mark.parametrize("missing_field", ["status", "currency"])
async def test_undeclared_wallet_unit_or_state_never_reserves_or_creates(missing_field):
    from types import SimpleNamespace

    from cloud_platform.modules.hourly.service import HourlyError
    from cloud_platform.modules.wallet.domain import WalletStatus

    offer, cloud, service, money = await _hourly_order()
    wallet = SimpleNamespace(
        id=uuid4(),
        balance=50_000,
        currency="USD",
        status=WalletStatus.ACTIVE,
    )
    delattr(wallet, missing_field)
    service._wallets.get = AsyncMock(return_value=wallet)
    with pytest.raises(HourlyError):
        await service.create_instance(
            user=USER,
            offer_id=offer.id,
            image_id="UBUNTU_24_04",
            image_label="Ubuntu",
            idempotency_key=IK,
        )
    assert service._servers.servers == {}
    assert service._ops.ops == {}
    assert money["hold_repo"].holds == {}
    assert cloud.posts == []


@pytest.mark.parametrize("minutes, hours_bought", [(30, 1), (60, 2)])
async def test_successful_power_resume_uses_paid_coverage_exactly_once(minutes, hours_bought):
    server = _server()
    h = Harness(servers=[server], holds={HOLD_KEY: _hold(HOLD_KEY)})
    await h.make_job().prepay_server(server, T0)
    server.state = ServerLifecycleState.STOPPED
    at = T0 + timedelta(minutes=minutes)

    class PaidProvider(PowerProvider):
        async def power_on(self, resource_id, key):
            assert server.last_accrued_at > at
            assert len(h.entries) == hours_bought
            await super().power_on(resource_id, key)

    provider = PaidProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    service = PowerCommandService(
        server_repo=StateServers([server]),
        operation_repo=FakeOpRepo(),
        provider_registry=registry,
        audit_repo=h.audit,
        prepay_server=lambda current: h.make_job().prepay_server(current, at),
    )
    first = await service.power_on(server.user_id, server.id, "resume-once")
    assert not first.replayed
    replay = await service.power_on(server.user_id, server.id, "resume-once")
    assert replay.replayed
    assert len(provider.calls) == 1
    assert h.wallet.balance == 100_000 - hours_bought * SELLING
