"""Consumer boundaries for durable hourly creation and first-hour money.

Repositories reuse the established hourly test ports. Financial effects below
are explicit stateful ports, so a provider uncertainty cannot masquerade as a
successful charge or an available-balance refund.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cloud_platform.core.money import Money
from cloud_platform.modules.billing.service import (
    PrepaidHourlyBillingService,
    PrepaidHourlyPeriodStatus,
)
from cloud_platform.modules.compute.domain import ServerLifecycleState
from cloud_platform.modules.hourly.service import (
    HourlyCloudService,
    HourlyError,
    HourlyNotAvailableError,
    HourlyRequestFailedError,
    hourly_reference_name,
)
from cloud_platform.modules.operations.domain import OperationStatus, OperationType
from cloud_platform.modules.wallet.domain import HoldStatus, LedgerEntryType
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderOutcomeUnknown,
    ProviderUnavailable,
)
from tests.unit.test_hetzner_hourly_failover import Pool
from tests.unit.test_hourly_service_branches import (
    USER,
    FakeAccountRepo,
    FakeAuditRepo,
    FakeCloud,
    FakeOffersRepo,
    FakeOpsRepo,
    _match,
    _usd_offer,
)
from tests.unit.test_prepaid_hourly_checkout import (
    Holds,
    Periods,
    Quotes,
    Servers,
    Snapshots,
    Wallets,
)


class MoneyPort(Holds):
    """Idempotent reservation, capture ledger, and spendable-balance model."""

    def __init__(self, wallets):
        super().__init__()
        self.wallets = wallets
        self.available = wallets.wallet.balance
        self.entries = {}
        self.releases = 0
        self.captures = 0

    async def create_hold(self, wallet_id, amount, currency, idempotency_key):
        assert wallet_id == self.wallets.wallet.id
        assert currency == self.wallets.wallet.currency
        previous = await self.get_by_idempotency(wallet_id, idempotency_key)
        if previous is not None:
            assert (previous.amount, previous.currency) == (amount, currency)
        return await super().create_hold(wallet_id, amount, currency, idempotency_key)

    async def release_hold(self, wallet_id, hold_id, key):
        hold = self.rows[wallet_id, key]
        assert hold.id == hold_id
        if hold.status is HoldStatus.CREATED:
            hold.release()
            self.available += hold.amount
            self.releases += 1
        assert hold.status is HoldStatus.RELEASED
        return hold

    async def capture_hold(self, wallet_id, hold_id, key):
        hold = self.rows[wallet_id, key]
        assert hold.id == hold_id
        if hold.status is HoldStatus.CREATED:
            hold.capture()
            self.wallets.wallet.balance -= hold.amount
            self.captures += 1
            self.entries[wallet_id, f"capture-{key}"] = SimpleNamespace(
                amount=Money(Decimal(hold.amount), hold.currency),
                entry_type=LedgerEntryType.CHARGE,
                reference_type="hold",
                reference_id=str(hold.id),
                description=f"hold captured for {key}",
            )
        assert hold.status is HoldStatus.CAPTURED
        return hold

    async def get_entry_by_idempotency(self, wallet_id, key):
        return self.entries.get((wallet_id, key))


class PaidPeriods(Periods):
    def __init__(self, servers, ledger):
        super().__init__()
        self.servers = servers
        self.ledger = ledger

    async def list_charged_pending(self):
        return [
            period
            for period in self.rows.values()
            if period.status is PrepaidHourlyPeriodStatus.PENDING
            and (period.wallet_id, period.idempotency_key) in self.ledger.entries
        ]

    async def mark_paid(self, period):
        key = period.server_id, period.period_start
        self.rows[key] = replace(self.rows[key], status=PrepaidHourlyPeriodStatus.PAID)
        self.servers.servers[period.server_id].prepaid_paid_until = period.period_end


class ScriptedCloud(FakeCloud):
    def __init__(self):
        super().__init__()
        self.validation_error = None
        self.images_error = None
        self.lookup_error = None
        self.response = {}
        self.validations = []
        self.lookups = []

    async def validate_hourly_offer_for_checkout(self, **kwargs):
        self.validations.append(kwargs)
        if self.validation_error:
            raise self.validation_error
        return await super().validate_hourly_offer_for_checkout(**kwargs)

    async def list_images(self, region):
        if self.images_error:
            raise self.images_error
        return await super().list_images(region)

    async def create_instance(self, **kwargs):
        response = await super().create_instance(**kwargs)
        response.__dict__.update(self.response)
        return response

    async def find_by_reference(self, region, reference):
        self.lookups.append((region, reference))
        if self.lookup_error:
            raise self.lookup_error
        return await super().find_by_reference(region, reference)


async def rig(*, currency="USD", account=None, snapshots=None):
    offer = replace(await _usd_offer(), provider_account_id=account)
    servers, ops = Servers(), FakeOpsRepo()
    snapshots = snapshots or Snapshots()
    wallets, quotes = Wallets(currency), Quotes()
    holds = MoneyPort(wallets)
    periods = PaidPeriods(servers, holds)
    cloud = ScriptedCloud()
    resolver = SimpleNamespace(
        adapter_for=lambda provider, owner: cloud if owner == account else None
    )
    offers = FakeOffersRepo([offer])
    billing = PrepaidHourlyBillingService(
        server_repo=servers,
        wallet_repo=wallets,
        ledger_repo=holds,
        snapshot_repo=snapshots,
        period_repo=periods,
        fx_resolver=quotes,
    )
    service = HourlyCloudService(
        server_repo=servers,
        offers_repo=offers,
        account_repo=FakeAccountRepo(),
        wallet_repo=wallets,
        hold_repo=holds,
        hold_service=holds,
        snapshot_service=snapshots,
        operation_repo=ops,
        audit_repo=FakeAuditRepo(),
        cloud_providers={offer.provider_key: cloud},
        cloud_resolver=resolver if account else None,
        prepaid_billing=billing,
        period_repo=periods,
        prepay_server=AsyncMock(),
    )
    return SimpleNamespace(
        service=service,
        servers=servers,
        ops=ops,
        snapshots=snapshots,
        wallets=wallets,
        holds=holds,
        periods=periods,
        quotes=quotes,
        cloud=cloud,
        offer=offer,
        offers=offers,
    )


async def buy(env, *, key="boundary-order", **overrides):
    args = dict(
        user=USER,
        offer_id=env.offer.id,
        image_id="UBUNTU",
        image_label="Ubuntu",
        idempotency_key=key,
        expected_selling_price_minor=env.offer.selling_price_minor,
        expected_selling_currency="USD",
    )
    args.update(overrides)
    return await env.service.create_instance(**args)


def operation(env, server):
    return env.ops.ops[f"server-create:{server.id}"]


def hold(env, key="boundary-order"):
    return env.holds.rows[env.wallets.wallet.id, f"server-create:{key}"]


async def test_missing_operation_repairs_from_cached_snapshot_after_catalog_disappears():
    env = await rig(currency="IRT")
    server = (await buy(env)).server
    original = await env.snapshots.require_snapshot(server.id)
    reserved = hold(env)
    env.ops.ops.clear()
    env.offers._offers.clear()

    replay = await buy(env)

    assert replay.replayed and replay.server.id == server.id
    assert await env.snapshots.require_snapshot(server.id) is original
    assert operation(env, server).status is OperationStatus.PENDING
    assert hold(env) is reserved and reserved.status is HoldStatus.CREATED
    assert len(env.servers.servers) == len(env.periods.rows) == len(env.holds.rows) == 1
    assert env.quotes.calls == 1
    assert env.holds.available == 100_000 - reserved.amount
    assert env.cloud.posts == 0


@pytest.mark.parametrize("changed", ["price", "currency"])
async def test_cached_bundle_repair_never_accepts_a_changed_customer_confirmation(changed):
    env = await rig(currency="IRT")
    server = (await buy(env)).server
    env.ops.ops.clear()
    changes = (
        {"expected_selling_price_minor": env.offer.selling_price_minor + 1}
        if changed == "price"
        else {"expected_selling_currency": "EUR"}
    )

    with pytest.raises(HourlyError, match="different price"):
        await buy(env, **changes)

    assert not env.ops.ops and env.cloud.posts == 0
    assert server.state is ServerLifecycleState.REQUESTED
    assert hold(env).status is HoldStatus.CREATED
    assert env.quotes.calls == len(env.periods.rows) == 1


async def test_concurrent_confirmation_waits_for_creator_without_quarantining_intent():
    class PausedSnapshots(Snapshots):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.replay_read = asyncio.Event()

        async def create_snapshot(self, **kwargs):
            self.entered.set()
            await self.release.wait()
            return await super().create_snapshot(**kwargs)

        async def require_snapshot(self, server_id):
            self.replay_read.set()
            return await super().require_snapshot(server_id)

    snapshots = PausedSnapshots()
    env = await rig(currency="IRT", snapshots=snapshots)
    tasks = []
    try:
        async with asyncio.timeout(5):
            tasks.append(asyncio.create_task(buy(env)))
            await snapshots.entered.wait()
            server = next(iter(env.servers.servers.values()))
            # Prevent repair from borrowing live facts while the creator owns
            # the uncommitted bundle. The committed fingerprint remains valid.
            env.offers._offers.clear()
            tasks.append(asyncio.create_task(buy(env)))
            await snapshots.replay_read.wait()
            assert server.state is ServerLifecycleState.REQUESTED
            assert env.cloud.posts == 0 and not env.holds.rows
            snapshots.release.set()
            first, replay = await asyncio.gather(*tasks)
    finally:
        snapshots.release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert first.server.id == replay.server.id and replay.replayed
    assert len(env.servers.servers) == len(snapshots.rows) == len(env.ops.ops) == 1
    assert len(env.periods.rows) == len(env.holds.rows) == env.quotes.calls == 1
    assert hold(env).status is HoldStatus.CREATED
    assert env.holds.available == 100_000 - hold(env).amount


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation_type", OperationType.SERVER_DELETE),
        ("resource_type", "another-resource"),
        ("resource_id", uuid4()),
        ("provider_key", "another-provider"),
    ],
)
async def test_foreign_operation_cannot_authorize_create_or_consume_reservation(field, value):
    env = await rig()
    server = (await buy(env)).server
    op = operation(env, server)
    setattr(op, field, value)

    with pytest.raises(HourlyError, match="operation"):
        await env.service.process_server(server.id)

    assert op.status is OperationStatus.PENDING and op.attempts == 0
    assert server.provider_server_id is None and server.state is ServerLifecycleState.REQUESTED
    assert env.cloud.posts == 0 and hold(env).status is HoldStatus.CREATED
    assert env.holds.captures == env.holds.releases == 0


@pytest.mark.parametrize("router", ["missing-owner", "legacy-only"])
async def test_removed_pinned_credential_never_falls_back_to_default_create(router):
    env = await rig(account="owned-project")
    server = (await buy(env)).server
    if router == "missing-owner":
        env.service._cloud_resolver = SimpleNamespace(adapter_for=lambda *args: None)
    else:
        env.service._cloud_resolver = None

    assert await env.service.process_server(server.id) == "failed"
    assert await env.service.process_server(server.id) == "skipped"
    assert env.cloud.posts == 0
    assert operation(env, server).status is OperationStatus.FAILED
    assert server.state is ServerLifecycleState.ERROR
    assert server.credential_account_id == "owned-project"
    assert hold(env).status is HoldStatus.RELEASED
    assert env.holds.available == env.wallets.wallet.balance == 100_000
    assert env.holds.releases == 1


@pytest.mark.parametrize("failure_stage", ["validation", "images"])
async def test_transient_prepost_reads_keep_one_hold_and_retry_the_same_operation(failure_stage):
    env = await rig()
    server = (await buy(env)).server
    reserved = hold(env)
    if failure_stage == "validation":
        env.cloud.validation_error = ProviderUnavailable("catalog unavailable")
    else:
        env.cloud.images_error = ProviderUnavailable("images unavailable")

    assert await env.service.process_server(server.id) == "requeued"
    op = operation(env, server)
    assert op.status is OperationStatus.PENDING and op.attempts == 1
    assert server.state is ServerLifecycleState.REQUESTED and env.cloud.posts == 0
    assert hold(env) is reserved and reserved.status is HoldStatus.CREATED
    assert env.holds.releases == env.holds.captures == 0
    env.cloud.validation_error = env.cloud.images_error = None
    assert await env.service.process_server(server.id) == "provisioned"
    assert op.status is OperationStatus.COMPLETED and op.attempts == 2
    assert env.cloud.posts == len(env.holds.rows) == 1
    assert env.holds.available == 100_000 - reserved.amount


@pytest.mark.parametrize("failure_stage", ["validation", "images"])
async def test_definitive_prepost_rejection_releases_hold_once(failure_stage):
    env = await rig()
    server = (await buy(env)).server
    if failure_stage == "validation":
        env.cloud.validation_error = ProviderAuthError("account disabled")
    else:
        env.cloud.images_error = ProviderError("image access denied")

    assert await env.service.process_server(server.id) == "failed"
    with pytest.raises(HourlyRequestFailedError):
        await buy(env)
    assert await env.service.process_server(server.id) == "skipped"
    assert operation(env, server).status is OperationStatus.FAILED
    assert server.state is ServerLifecycleState.ERROR and server.provider_server_id is None
    assert env.cloud.posts == 0 and hold(env).status is HoldStatus.RELEASED
    assert env.holds.releases == 1 and env.holds.captures == 0
    assert env.holds.available == env.wallets.wallet.balance == 100_000


@pytest.mark.parametrize(
    "response",
    [
        {"id": ""},
        {"state": "failed"},
        {"state": "provider-new-unrecognized-state"},
        {"account_id": "foreign-project"},
        {"image_id": "not-the-paid-image"},
    ],
)
async def test_untrustworthy_post_receipt_retains_money_and_never_reposts(response):
    env = await rig(currency="IRT", account="owned-project")
    server = (await buy(env)).server
    env.cloud.response = response

    assert await env.service.process_server(server.id) == "outcome-unknown"
    assert await env.service.process_server(server.id) == "skipped"
    assert await env.service.reconcile_server(server.id) == "still-unknown"
    assert (await buy(env)).replayed
    assert env.cloud.posts == 1
    assert operation(env, server).status is OperationStatus.OUTCOME_UNKNOWN
    assert server.provider_server_id is None
    assert hold(env).status is HoldStatus.CREATED
    assert env.holds.available == 100_000 - hold(env).amount
    assert env.holds.releases == env.holds.captures == 0
    assert len(env.periods.rows) == env.quotes.calls == 1


@pytest.mark.parametrize("state", ["", "migrating-in-an-unknown-state", "failed"])
async def test_stale_inflight_recovery_requires_both_identity_and_known_provider_state(state):
    env = await rig(currency="IRT")
    server = (await buy(env)).server
    op = operation(env, server)
    op.mark_in_flight()
    op.updated_at = datetime.now(UTC) - timedelta(minutes=20)
    env.cloud.find_result = _match(server.id, state=state)

    assert await env.service.process_server(server.id) == "outcome-unknown"
    assert op.status is OperationStatus.OUTCOME_UNKNOWN
    assert env.cloud.posts == 0 and len(env.cloud.lookups) == 1
    assert server.provider_server_id is None and hold(env).status is HoldStatus.CREATED
    assert env.holds.captures == env.holds.releases == 0


async def test_fresh_inflight_read_failure_is_not_evidence_for_post_or_refund():
    env = await rig(currency="IRT")
    server = (await buy(env)).server
    op = operation(env, server)
    op.mark_in_flight()
    op.updated_at = datetime.now(UTC).replace(tzinfo=None)
    env.cloud.lookup_error = ProviderUnavailable("inventory offline")

    assert await env.service.process_server(server.id) == "still-inflight"
    assert op.status is OperationStatus.IN_FLIGHT and op.attempts == 1
    assert env.cloud.posts == 0
    assert hold(env).status is HoldStatus.CREATED
    assert env.holds.available == 100_000 - hold(env).amount


async def test_accepted_irt_create_captures_bound_quote_only_at_provisioning_bridge():
    env = await rig(currency="IRT")
    server = (await buy(env)).server
    period_key = server.id, server.created_at
    original = env.periods.rows[period_key]
    # An already reserved quote remains the paid contract after its expiry.
    expired = replace(
        original.fx_snapshot,
        observed_at=datetime.now(UTC) - timedelta(hours=1),
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    env.periods.rows[period_key] = replace(original, fx_snapshot=expired)

    with pytest.raises(HourlyError, match="attached provider"):
        await env.service.capture_first_hour(server.id)
    assert env.holds.captures == 0
    assert await env.service.process_server(server.id) == "provisioned"
    assert operation(env, server).status is OperationStatus.COMPLETED
    assert hold(env).status is HoldStatus.CREATED
    assert env.wallets.wallet.balance == 100_000 and not env.holds.entries
    assert server.prepaid_paid_until is None

    await env.service.capture_first_hour(server.id)
    await env.service.capture_first_hour(server.id)
    replay = await buy(env)

    assert replay.replayed and replay.server.id == server.id
    assert env.holds.captures == len(env.holds.entries) == 1
    assert env.wallets.wallet.balance == env.holds.available == 100_000 - original.irt_minor
    assert hold(env).status is HoldStatus.CAPTURED
    paid = env.periods.rows[period_key]
    assert paid.status is PrepaidHourlyPeriodStatus.PAID
    assert paid.fx_snapshot == expired and paid.irt_minor == original.irt_minor
    assert server.prepaid_paid_until == original.period_end
    assert env.quotes.calls == len(env.periods.rows) == len(env.holds.rows) == 1
    assert env.cloud.posts == 1


@pytest.mark.parametrize("corruption", ["period-wallet", "period-status", "wallet-currency"])
async def test_first_hour_gate_does_not_repair_inconsistent_money_before_post(corruption):
    env = await rig(currency="IRT")
    server = (await buy(env)).server
    key = server.id, server.created_at
    period = env.periods.rows[key]
    if corruption == "period-wallet":
        env.periods.rows[key] = replace(period, wallet_id=uuid4())
    elif corruption == "period-status":
        env.periods.rows[key] = replace(period, status=PrepaidHourlyPeriodStatus.PAID)
    else:
        env.wallets.wallet.currency = "USD"

    assert await env.service.process_server(server.id) == "awaiting-first-hour"
    assert operation(env, server).status is OperationStatus.PENDING
    assert operation(env, server).attempts == 0
    assert server.state is ServerLifecycleState.REQUESTED and env.cloud.posts == 0
    assert hold(env).status is HoldStatus.CREATED
    assert env.holds.captures == env.holds.releases == 0 and env.quotes.calls == 1


async def test_hold_disappearing_during_worker_reads_requeues_without_new_fx_or_post():
    env = await rig(currency="IRT")
    server = (await buy(env)).server
    saved_reader = env.cloud.list_images
    saved_hold = hold(env)

    async def lose_reservation(region):
        env.holds.rows.clear()
        return await saved_reader(region)

    env.cloud.list_images = lose_reservation
    assert await env.service.process_server(server.id) == "requeued"
    assert operation(env, server).status is OperationStatus.PENDING
    assert operation(env, server).attempts == 1
    assert env.cloud.posts == 0 and not env.holds.rows
    assert env.quotes.calls == len(env.periods.rows) == 1
    assert saved_hold.status is HoldStatus.CREATED
    assert env.holds.captures == env.holds.releases == 0


@pytest.fixture
async def boundary_pool():
    instance = await Pool().setup()
    wallets = Wallets("USD")
    holds = MoneyPort(wallets)
    instance.service._wallets = wallets
    instance.service._hold_repo = holds
    instance.service._holds = holds
    instance.money = holds
    try:
        yield instance
    finally:
        for provider in instance.providers.values():
            await provider.close()


async def test_pool_inventory_outage_before_sent_can_requeue_without_an_account_attempt(
    boundary_pool,
):
    env = boundary_pool
    server = (await env.create()).server
    env.counts.update({"hz-a": None, "hz-b": None})

    assert await env.service.process_server(server.id) == "requeued"
    op = env.ops.ops[f"server-create:{server.id}"]
    assert op.status is OperationStatus.PENDING
    assert env.receipt(server.id)["attempts"] == []
    assert env.posts == [] and server.provider_server_id is None
    reserved = next(iter(env.money.rows.values()))
    assert reserved.status is HoldStatus.CREATED and env.money.releases == 0
    env.counts.update({"hz-a": 4, "hz-b": 2})
    assert await env.service.process_server(server.id) == "provisioned"
    assert len(env.posts) == len(env.money.rows) == 1
    assert op.status is OperationStatus.COMPLETED


async def test_pool_unknown_readonly_recovery_uses_receipt_owner_and_captures_no_irt(boundary_pool):
    env = boundary_pool
    server = (await env.create()).server
    env.failure = "unknown"
    assert await env.service.process_server(server.id) == "outcome-unknown"
    op = env.ops.ops[f"server-create:{server.id}"]
    owner = env.adapters["hz-a"]
    lookup = AsyncMock(
        return_value=SimpleNamespace(
            id="landed-instance",
            state="running",
            region="fsn1",
            instance_type="cx22",
            image_id="100",
            account_id="hz-a",
            reference=hourly_reference_name(server.id),
        )
    )
    owner.find_by_operation = lookup
    # A newer routing preference is not ownership of the previously sent POST.
    env.counts.update({"hz-a": 5, "hz-b": 0})

    assert await env.service.process_server(server.id) == "attached"
    assert await env.service.process_server(server.id) == "skipped"
    lookup.assert_awaited_once_with(op.operation_key, platform_server_id=str(server.id))
    assert server.credential_account_id == "hz-a" and server.provider_server_id == "landed-instance"
    assert server.state is ServerLifecycleState.PROVISIONING
    assert op.status is OperationStatus.COMPLETED
    assert [account for account, _ in env.posts] == ["hz-a"]
    assert env.receipt(server.id)["accepted_account_id"] == "hz-a"
    assert env.money.releases == env.money.captures == 0
    assert len(env.money.rows) == 1


async def test_pool_worker_revalidation_auth_failure_does_not_try_other_credentials(boundary_pool):
    env = boundary_pool
    server = (await env.create()).server
    env.adapters["hz-a"].validate_hourly_offer_for_checkout = AsyncMock(
        side_effect=ProviderAuthError("credential revoked")
    )

    assert await env.service.process_server(server.id) == "failed"
    assert env.posts == [] and env.receipt(server.id)["attempts"] == []
    assert server.state is ServerLifecycleState.ERROR and server.provider_server_id is None
    assert server.credential_account_id == "hz-a"
    assert next(iter(env.money.rows.values())).status is HoldStatus.RELEASED
    assert env.money.available == 100_000 and env.money.releases == 1


@pytest.mark.parametrize("change", ["price", "withdrawal"])
async def test_checkout_rechecks_catalog_after_provider_validation_before_reserving(change):
    env = await rig(currency="IRT")
    validate = env.cloud.validate_hourly_offer_for_checkout

    async def refresh_during_validation(**kwargs):
        facts = await validate(**kwargs)
        env.offers._offers[:] = [
            replace(env.offer, selling_price_minor=env.offer.selling_price_minor + 1)
            if change == "price"
            else replace(env.offer, enabled=False)
        ]
        return facts

    env.cloud.validate_hourly_offer_for_checkout = refresh_during_validation
    with pytest.raises(HourlyNotAvailableError, match="during checkout"):
        await buy(env)

    assert not env.servers.servers and not env.snapshots.rows and not env.ops.ops
    assert not env.holds.rows and not env.periods.rows and env.quotes.calls == 0
    assert env.holds.available == env.wallets.wallet.balance == 100_000
    assert env.cloud.posts == 0


async def test_missing_prepaid_snapshot_stays_repairable_without_claim_or_refund():
    env = await rig(currency="IRT")
    server = (await buy(env)).server
    env.snapshots.rows.clear()

    assert await env.service.process_server(server.id) == "awaiting-first-hour"
    assert server.state is ServerLifecycleState.REQUESTED
    assert operation(env, server).status is OperationStatus.PENDING
    assert operation(env, server).attempts == 0
    assert env.cloud.posts == 0 and hold(env).status is HoldStatus.CREATED
    assert env.holds.captures == env.holds.releases == 0


async def test_snapshot_read_failure_after_claim_is_definitive_prepost_failure():
    env = await rig()
    server = (await buy(env)).server
    original_reader = env.snapshots.require_snapshot
    reads = 0

    async def fail_after_preflight(server_id):
        nonlocal reads
        reads += 1
        if reads == 2:
            raise RuntimeError("snapshot store disconnected")
        return await original_reader(server_id)

    env.snapshots.require_snapshot = fail_after_preflight
    assert await env.service.process_server(server.id) == "failed"
    assert operation(env, server).status is OperationStatus.FAILED
    assert operation(env, server).attempts == 1
    assert server.state is ServerLifecycleState.ERROR and server.provider_server_id is None
    assert env.cloud.posts == 0 and hold(env).status is HoldStatus.RELEASED
    assert env.holds.releases == 1 and env.holds.available == 100_000


@pytest.mark.parametrize("credential_store", ["absent", "unavailable"])
async def test_lost_one_time_credential_persistence_does_not_repeat_accepted_post(credential_store):
    env = await rig(currency="IRT")
    server = (await buy(env)).server
    env.cloud.response = {
        "create_password": SimpleNamespace(reveal=lambda: "synthetic-one-time-password"),
        "ssh_username": "root",
    }
    if credential_store == "unavailable":
        env.service._credential_store = SimpleNamespace(
            save_issued=AsyncMock(side_effect=RuntimeError("encrypted store unavailable"))
        )

    assert await env.service.process_server(server.id) == "outcome-unknown"
    assert await env.service.process_server(server.id) == "skipped"
    assert operation(env, server).status is OperationStatus.OUTCOME_UNKNOWN
    assert server.provider_server_id is None and env.cloud.posts == 1
    assert hold(env).status is HoldStatus.CREATED and not env.holds.entries
    assert env.holds.releases == env.holds.captures == 0
    assert len(env.periods.rows) == env.quotes.calls == 1


async def test_capture_commit_gap_replays_existing_charge_before_advancing_coverage():
    env = await rig(currency="IRT")
    server = (await buy(env)).server
    assert await env.service.process_server(server.id) == "provisioned"
    mark_paid = env.periods.mark_paid

    async def unavailable(period):
        raise RuntimeError("coverage commit unavailable")

    env.periods.mark_paid = unavailable
    with pytest.raises(RuntimeError, match="coverage commit"):
        await env.service.capture_first_hour(server.id)
    assert hold(env).status is HoldStatus.CAPTURED
    assert env.holds.captures == len(env.holds.entries) == 1
    assert server.prepaid_paid_until is None
    assert (
        env.periods.rows[server.id, server.created_at].status is PrepaidHourlyPeriodStatus.PENDING
    )
    assert (await buy(env)).replayed

    env.periods.mark_paid = mark_paid
    await env.service.capture_first_hour(server.id)
    assert env.holds.captures == len(env.holds.entries) == 1
    assert env.wallets.wallet.balance == env.holds.available == 100_000 - hold(env).amount
    assert server.prepaid_paid_until == server.created_at + timedelta(hours=1)
    assert env.periods.rows[server.id, server.created_at].status is PrepaidHourlyPeriodStatus.PAID
    assert env.quotes.calls == env.cloud.posts == 1


async def test_pool_stale_claim_without_sent_receipt_can_requeue_but_not_create_in_recovery_pass(
    boundary_pool,
):
    env = boundary_pool
    server = (await env.create()).server
    op = env.ops.ops[f"server-create:{server.id}"]
    await env.receipts.claim(op.id, server.id, catalog_account_id="hz-a")
    op.updated_at = datetime.now(UTC) - timedelta(minutes=20)

    assert await env.service.reconcile_server(server.id) == "still-inflight"
    assert await env.service.process_server(server.id) == "requeued"
    assert op.status is OperationStatus.PENDING and env.posts == []
    assert env.receipt(server.id)["attempts"] == []
    assert next(iter(env.money.rows.values())).status is HoldStatus.CREATED
    assert env.money.captures == env.money.releases == 0
    assert await env.service.process_server(server.id) == "provisioned"
    assert len(env.posts) == len(env.money.rows) == 1


async def test_pool_accepted_receipt_repairs_completion_by_owned_resource_id_not_another_post(
    boundary_pool,
):
    env = boundary_pool
    server = (await env.create()).server
    save_outcome = env.receipts.save_outcome

    async def completion_unavailable(*args, **kwargs):
        raise RuntimeError("completion commit unavailable")

    env.receipts.save_outcome = completion_unavailable
    with pytest.raises(RuntimeError, match="completion commit"):
        await env.service.process_server(server.id)
    op = env.ops.ops[f"server-create:{server.id}"]
    attempt = env.receipt(server.id)["attempts"][-1]
    assert attempt["phase"] == "accepted" and op.status is OperationStatus.IN_FLIGHT
    provider_id = attempt["provider_server_id"]
    assert server.provider_server_id == provider_id
    read_owned = AsyncMock(
        return_value=SimpleNamespace(
            id=provider_id,
            state="running",
            region="fsn1",
            instance_type="cx22",
            image_id="100",
            account_id="hz-a",
            reference=hourly_reference_name(server.id),
        )
    )
    env.adapters["hz-a"].get_instance = read_owned
    env.receipts.save_outcome = save_outcome

    assert await env.service.process_server(server.id) == "attached"
    read_owned.assert_awaited_once_with(provider_id)
    assert op.status is OperationStatus.COMPLETED
    assert server.state is ServerLifecycleState.PROVISIONING
    assert server.credential_account_id == "hz-a"
    assert [account for account, _ in env.posts] == ["hz-a"]
    assert next(iter(env.money.rows.values())).status is HoldStatus.CREATED
    assert env.money.releases == env.money.captures == 0


async def test_capacity_refusal_failover_keeps_exactly_one_customer_reservation(boundary_pool):
    env = boundary_pool
    server = (await env.create()).server
    reserved = next(iter(env.money.rows.values()))
    amount = reserved.amount
    snapshot = env.snapshots.created[0][1]
    env.failure = "capacity"

    assert await env.service.process_server(server.id) == "provisioned"
    assert (await env.create()).replayed
    assert await env.service.process_server(server.id) == "skipped"
    assert [account for account, _ in env.posts] == ["hz-a", "hz-b"]
    assert [attempt["phase"] for attempt in env.receipt(server.id)["attempts"]] == [
        "capacity_refused",
        "accepted",
    ]
    assert server.credential_account_id == "hz-b"
    assert server.provider_server_id is not None
    assert server.state is ServerLifecycleState.PROVISIONING
    assert env.ops.ops[f"server-create:{server.id}"].status is OperationStatus.COMPLETED
    assert env.snapshots.created == [(server.id, snapshot)]
    assert len(env.money.rows) == 1 and next(iter(env.money.rows.values())) is reserved
    assert reserved.status is HoldStatus.CREATED and reserved.amount == amount
    assert env.money.available == 100_000 - amount
    assert env.money.captures == env.money.releases == 0


async def test_capacity_refusal_with_no_contract_compatible_alternative_refunds_once(boundary_pool):
    env = boundary_pool
    server = (await env.create()).server
    env.failure = "capacity"
    env.b_image = "101"

    assert await env.service.process_server(server.id) == "failed"
    assert await env.service.process_server(server.id) == "skipped"
    with pytest.raises(HourlyRequestFailedError):
        await env.create()
    assert [account for account, _ in env.posts] == ["hz-a"]
    assert env.receipt(server.id)["attempts"][-1]["phase"] == "capacity_refused"
    assert server.provider_server_id is None and server.state is ServerLifecycleState.ERROR
    assert env.ops.ops[f"server-create:{server.id}"].status is OperationStatus.FAILED
    assert next(iter(env.money.rows.values())).status is HoldStatus.RELEASED
    assert env.money.releases == 1 and env.money.captures == 0
    assert env.money.available == 100_000


async def test_timeout_recovery_cannot_fall_back_when_original_account_was_removed():
    env = await rig(currency="IRT", account="owned-project")
    server = (await buy(env)).server
    env.cloud.create_error = ProviderOutcomeUnknown("POST acknowledgment lost")
    assert await env.service.process_server(server.id) == "outcome-unknown"
    env.service._cloud_resolver = SimpleNamespace(adapter_for=lambda *args: None)

    assert await env.service.reconcile_server(server.id) == "skipped"
    assert await env.service.process_server(server.id) == "skipped"
    assert env.cloud.posts == 1 and env.cloud.lookups == []
    assert operation(env, server).status is OperationStatus.OUTCOME_UNKNOWN
    assert server.credential_account_id == "owned-project" and server.provider_server_id is None
    assert hold(env).status is HoldStatus.CREATED
    assert len(env.periods.rows) == env.quotes.calls == 1
    assert env.holds.releases == env.holds.captures == 0
