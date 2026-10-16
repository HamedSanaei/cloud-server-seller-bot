"""Corporate hourly coverage must never manufacture a personal wallet payment."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cloud_platform.modules.billing.service import (
    PrepaidBalancePolicyService,
    PrepaidHourlyBillingService,
    PrepaidHourlyPeriodStatus,
    prepaid_hourly_key,
)
from cloud_platform.modules.compute.domain import CloudServer, ServerLifecycleState
from cloud_platform.modules.funding import (
    FundingAuthorizationError,
    FundingGrantState,
    FundingSource,
    OperatorExpense,
    OperatorFundingGrant,
    expense_operation_key,
)
from cloud_platform.modules.hourly.domain import operator_funded


class Funding:
    def __init__(self):
        self.authorized = True
        self.grants = {}
        self.expenses = {}
        self.reasons = []

    async def resolve_source(self, owner_id):
        return FundingSource.OPERATOR if self.authorized else FundingSource.CUSTOMER_WALLET

    async def cancel(self, grant, *, reason):
        self.grants[grant.operation_key] = replace(grant, state=FundingGrantState.CANCELLED)
        return self.grants[grant.operation_key]

    async def authorize_new_period(self, *, owner_id, operator_id):
        if not self.authorized or owner_id != operator_id:
            raise FundingAuthorizationError("operator access revoked")

    async def reserve(self, **facts):
        await self.authorize_new_period(
            owner_id=facts["owner_id"], operator_id=facts["operator_id"]
        )
        key = facts["operation_key"]
        if key not in self.grants:
            self.reasons.append(facts["reason"])
            self.grants[key] = OperatorFundingGrant(
                id=uuid4(),
                owner_user_id=facts["owner_id"],
                operator_user_id=facts["operator_id"],
                operation_key=key,
                resource_key=facts["resource_key"],
                purpose=facts["purpose"],
                nominal_amount_minor=facts["nominal_amount_minor"],
                nominal_currency=facts["nominal_currency"],
                provider_cost_snapshot=facts["provider_cost_snapshot"],
                created_at=datetime.now(UTC),
            )
        return self.grants[key]

    async def get_grant(self, key):
        return self.grants.get(key)

    async def get_expense(self, key):
        return self.expenses.get(key)

    async def settle(self, grant):
        key = expense_operation_key(grant.operation_key)
        if key not in self.expenses:
            self.expenses[key] = OperatorExpense(
                id=uuid4(),
                grant_id=grant.id,
                operation_key=key,
                owner_user_id=grant.owner_user_id,
                operator_user_id=grant.operator_user_id,
                resource_key=grant.resource_key,
                purpose=grant.purpose,
                nominal_amount_minor=grant.nominal_amount_minor,
                nominal_currency=grant.nominal_currency,
                provider_cost_snapshot=grant.provider_cost_snapshot,
                occurred_at=datetime.now(UTC),
            )
            self.grants[grant.operation_key] = replace(grant, state=FundingGrantState.SETTLED)
        return self.expenses[key]


class Periods:
    def __init__(self, server, funding):
        self.server, self.funding = server, funding
        self.rows = {}
        self.fail_once = False

    async def get(self, server_id, start):
        return self.rows.get((server_id, start))

    async def bind(self, period):
        return self.rows.setdefault((period.server_id, period.period_start), period)

    async def list_charged_pending(self):
        return [
            period
            for period in self.rows.values()
            if period.status is PrepaidHourlyPeriodStatus.PENDING
            and expense_operation_key(period.idempotency_key) in self.funding.expenses
        ]

    async def mark_paid(self, period):
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("coverage write interrupted after expense commit")
        assert period.operator_expense_id is not None
        self.rows[(period.server_id, period.period_start)] = replace(
            period, status=PrepaidHourlyPeriodStatus.PAID
        )
        self.server.prepaid_paid_until = period.period_end


def fixture():
    owner = uuid4()
    row = CloudServer(
        id=uuid4(),
        user_id=owner,
        provider_key="hetzner",
        provider_account_id=uuid4(),
        provider_server_id="fixture-resource",
        state=ServerLifecycleState.RUNNING,
        billing_model="hourly",
        created_at=datetime.now(UTC) - timedelta(minutes=2),
        funding_source=FundingSource.OPERATOR,
        funding_operator_id=owner,
    )
    funding = Funding()
    periods = Periods(row, funding)
    forbidden = SimpleNamespace(get=AsyncMock(side_effect=AssertionError("personal wallet read")))
    servers = SimpleNamespace(
        get=AsyncMock(return_value=row), list_running=AsyncMock(return_value=[row])
    )
    price = SimpleNamespace(
        selling_minor=25,
        selling_currency="USD",
        offer=SimpleNamespace(
            provider_rate_exact="0.00000000000000000000000000123456789", currency="EUR"
        ),
    )
    service = PrepaidHourlyBillingService(
        server_repo=servers,
        wallet_repo=forbidden,
        ledger_repo=forbidden,
        snapshot_repo=SimpleNamespace(get=AsyncMock(return_value=price)),
        period_repo=periods,
        funding_service=funding,
        fx_resolver=None,
    )
    return row, price, funding, periods, forbidden, service


async def test_operator_first_hour_has_exact_native_cost_and_no_wallet_or_fx():
    row, price, funding, _, wallets, service = fixture()
    period = await service.prepare_operator_first_hour(row, price)
    assert period.wallet_id is period.irt_minor is period.fx_snapshot is None
    grant = await funding.get_grant(period.idempotency_key)
    assert grant.provider_cost_snapshot.amount_exact == price.offer.provider_rate_exact
    assert grant.provider_cost_snapshot.currency == "EUR"
    assert funding.reasons and not funding.expenses
    wallets.get.assert_not_called()


async def test_accepted_first_hour_can_settle_after_revocation_and_replay_once():
    row, price, funding, _, wallets, service = fixture()
    await service.prepare_operator_first_hour(row, price)
    funding.authorized = False
    first = await service.capture_operator_first_hour(row)
    second = await service.capture_operator_first_hour(row)
    assert first.operator_expense_id == second.operator_expense_id
    assert len(funding.expenses) == 1
    assert row.prepaid_paid_until == row.created_at + timedelta(hours=1)
    wallets.get.assert_not_called()


async def test_revocation_blocks_new_period_and_requests_nondestructive_suspension():
    row, price, funding, _, wallets, service = fixture()
    await service.prepare_operator_first_hour(row, price)
    await service.capture_operator_first_hour(row)
    original_boundary, original_provider_id = row.prepaid_paid_until, row.provider_server_id
    funding.authorized = False
    report = await service.run(customer_enabled=False)
    assert report.operator_suspend_server_ids == [row.id]
    assert report.stop_requested_server_ids == []
    assert row.state is ServerLifecycleState.RUNNING
    assert (
        row.prepaid_paid_until == original_boundary
        and row.provider_server_id == original_provider_id
    )
    assert len(funding.grants) == len(funding.expenses) == 1
    wallets.get.assert_not_called()


async def test_power_on_requires_fresh_authorization_even_with_paid_coverage():
    row, price, funding, _, _, service = fixture()
    await service.prepare_operator_first_hour(row, price)
    await service.capture_operator_first_hour(row)
    funding.authorized = False
    with pytest.raises(FundingAuthorizationError):
        await service.ensure_operator_coverage(row)


async def test_committed_expense_repairs_coverage_after_restart_without_double_cost():
    row, price, funding, periods, wallets, service = fixture()
    await service.prepare_operator_first_hour(row, price)
    periods.fail_once = True
    with pytest.raises(RuntimeError, match="interrupted"):
        await service.capture_operator_first_hour(row)
    assert len(funding.expenses) == 1 and row.prepaid_paid_until is None
    funding.authorized = False
    report = await service.run()
    assert report.replayed == 1 and row.prepaid_paid_until is not None
    assert len(funding.expenses) == 1 and report.operator_suspend_server_ids == [row.id]
    wallets.get.assert_not_called()


async def test_fabricated_paid_period_without_expense_fails_closed():
    row, price, _, periods, _, service = fixture()
    pending = await service.prepare_operator_first_hour(row, price)
    periods.rows[(row.id, row.created_at)] = replace(
        pending, status=PrepaidHourlyPeriodStatus.PAID, operator_expense_id=uuid4()
    )
    with pytest.raises(ValueError, match="no coherent corporate expense"):
        await service.capture_operator_first_hour(row)
    assert row.prepaid_paid_until is None


async def test_provider_acceptance_required_before_expense_receipt():
    row, price, funding, _, _, service = fixture()
    await service.prepare_operator_first_hour(row, price)
    row.provider_server_id = None
    with pytest.raises(ValueError, match="accepted provider resource"):
        await service.capture_operator_first_hour(row)
    assert not funding.expenses


async def test_operator_period_missing_exact_native_rate_fails_before_grant():
    row, price, funding, _, _, service = fixture()
    price.offer.provider_rate_exact = None
    with pytest.raises(ValueError, match="immutable native exact rate"):
        await service.prepare_operator_first_hour(row, price)
    assert not funding.grants


async def test_customer_balance_policy_never_reads_or_deletes_operator_wallet():
    row, _, _, _, wallets, _ = fixture()
    row.billing_model = "hourly_prepaid_irt"
    row.prepaid_zero_since = datetime.now(UTC) - timedelta(days=3)
    servers = SimpleNamespace(
        list_running=AsyncMock(return_value=[row]), list_stopped=AsyncMock(return_value=[])
    )
    report = await PrepaidBalancePolicyService(servers, wallets).evaluate()
    assert not report.decisions
    wallets.get.assert_not_called()


def test_hourly_source_policy_is_self_owned_and_excludes_monthly_lifecycle():
    row, *_ = fixture()
    row.funding_operator_id = uuid4()
    with pytest.raises(FundingAuthorizationError, match="self-owned"):
        operator_funded(row)
    row.funding_operator_id = row.user_id
    row.billing_model = "prepaid_monthly_fixed"
    assert not operator_funded(row)
    assert prepaid_hourly_key(row.id, row.created_at)


async def operator_checkout():
    from tests.unit.test_prepaid_hourly_checkout import checkout

    setup = await checkout()
    funding = Funding()
    forbidden = SimpleNamespace(
        get=AsyncMock(side_effect=AssertionError("personal wallet read")),
        get_by_idempotency=AsyncMock(side_effect=AssertionError("personal hold read")),
        create_hold=AsyncMock(side_effect=AssertionError("personal hold create")),
    )
    periods = Periods(None, funding)
    setup.service._funding = funding
    setup.service._wallets = forbidden
    setup.service._holds = setup.service._hold_repo = forbidden
    setup.service._periods = periods
    billing = setup.service._prepaid_billing
    billing._funding = funding
    billing._wallets = billing._ledger = forbidden
    billing._fx = None
    billing._periods = periods
    setup.funding, setup.forbidden, setup.periods = funding, forbidden, periods
    return setup


async def test_complete_operator_checkout_dispatch_and_receipt_never_uses_personal_money():
    from tests.unit.test_prepaid_hourly_checkout import buy

    setup = await operator_checkout()
    row = (await buy(setup)).server
    setup.periods.server = row
    assert row.funding_source is FundingSource.OPERATOR
    assert row.funding_operator_id == row.user_id
    assert not setup.cloud.posts and not setup.funding.expenses
    assert await setup.service.process_server(row.id) == "provisioned"
    assert len(setup.cloud.posts) == len(setup.funding.expenses) == 1
    assert row.prepaid_paid_until == row.created_at + timedelta(hours=1)
    replay = await buy(setup)
    assert replay.replayed and replay.server.id == row.id
    assert len(setup.cloud.posts) == len(setup.funding.expenses) == 1
    setup.forbidden.get.assert_not_called()
    setup.forbidden.create_hold.assert_not_called()
    assert setup.quotes.calls == 0


async def test_dispatch_after_revocation_durably_fails_without_post_or_wallet():
    from cloud_platform.modules.operations.domain import OperationStatus
    from tests.unit.test_prepaid_hourly_checkout import buy

    setup = await operator_checkout()
    row = (await buy(setup)).server
    setup.periods.server = row
    setup.funding.authorized = False
    assert await setup.service.process_server(row.id) == "failed"
    operation = await setup.service._ops.get_by_key(f"server-create:{row.id}")
    assert operation.status is OperationStatus.FAILED
    assert row.state is ServerLifecycleState.ERROR
    assert not setup.cloud.posts and not setup.funding.expenses
    assert next(iter(setup.funding.grants.values())).state is FundingGrantState.CANCELLED
    setup.forbidden.get.assert_not_called()


async def test_revocation_after_provider_acceptance_preserves_receipt_and_blocks_activation():
    from tests.unit.test_prepaid_hourly_checkout import buy

    setup = await operator_checkout()
    row = (await buy(setup)).server
    setup.periods.server = row
    original = setup.cloud.create_instance

    async def accept_then_revoke(**kwargs):
        result = await original(**kwargs)
        setup.funding.authorized = False
        return result

    setup.cloud.create_instance = accept_then_revoke
    assert await setup.service.process_server(row.id) == "provisioned"
    assert row.provider_server_id and len(setup.funding.expenses) == 1
    assert row.state is ServerLifecycleState.PROVISIONING
    with pytest.raises(FundingAuthorizationError):
        await setup.service._prepaid_billing.ensure_operator_coverage(row)
    assert len(setup.cloud.posts) == 1


async def test_missing_receipt_keeps_accepted_resource_repairable_before_running():
    from tests.unit.test_prepaid_hourly_checkout import buy

    setup = await operator_checkout()
    row = (await buy(setup)).server
    setup.periods.server = row
    setup.periods.fail_once = True
    with pytest.raises(RuntimeError, match="interrupted"):
        await setup.service.process_server(row.id)
    assert len(setup.cloud.posts) == 1 and row.provider_server_id
    assert row.prepaid_paid_until is None and row.state is ServerLifecycleState.PROVISIONING
    assert len(setup.funding.expenses) == 1
    setup.funding.authorized = False
    await setup.service.capture_first_hour(row.id)
    assert row.prepaid_paid_until is not None and len(setup.funding.expenses) == 1
    setup.forbidden.get.assert_not_called()


async def test_readonly_unknown_create_recovery_after_revocation_records_original_expense():
    from cloud_platform.modules.hourly.service import hourly_reference_name
    from tests.unit.test_prepaid_hourly_checkout import buy

    setup = await operator_checkout()
    row = (await buy(setup)).server
    setup.periods.server = row
    operation = await setup.service._ops.get_by_key(f"server-create:{row.id}")
    operation.mark_in_flight()
    setup.funding.authorized = False
    setup.cloud.find_by_reference = AsyncMock(
        return_value=SimpleNamespace(
            id="accepted-before-revoke",
            state="running",
            region=setup.offer.location_id,
            reference=hourly_reference_name(row.id),
            instance_type=setup.offer.product_id,
            image_id=row.image_id,
        )
    )
    result = await setup.service.process_server(row.id)
    assert result == "recovered"
    assert row.provider_server_id == "accepted-before-revoke"
    assert not setup.cloud.posts and len(setup.funding.expenses) == 1
    assert row.prepaid_paid_until is not None
    setup.forbidden.get.assert_not_called()


async def test_paid_watermark_without_corporate_receipt_cannot_activate_even_ahead_of_renewal():
    row, _, _, _, wallets, service = fixture()
    row.prepaid_paid_until = datetime.now(UTC) + timedelta(minutes=50)
    with pytest.raises(ValueError, match="corporate cutover receipt"):
        await service.ensure_operator_coverage(row)
    wallets.get.assert_not_called()


async def test_suspension_uses_idempotent_power_operation_and_never_wallet_delete():
    from cloud_platform.modules.operations.service import PowerCommandService
    from tests.unit.test_prepaid_provider_lifecycle import Operations, dependencies

    row, *_ = fixture()
    row.provider_key = "test"
    lifecycle, repo, wallets, provider, _, delete, registry = dependencies(row)
    lifecycle._power = PowerCommandService(
        server_repo=repo,
        operation_repo=Operations(),
        provider_registry=registry,
        audit_repo=AsyncMock(),
    )
    original_id = row.provider_server_id
    await lifecycle.suspend_operator(row.id)
    await lifecycle.suspend_operator(row.id)
    assert len(provider.power_calls) == 1
    assert row.state is ServerLifecycleState.STOPPED and row.provider_server_id == original_id
    delete.request.assert_not_called()
    wallets.get.assert_not_called()


async def test_audited_cutover_receipt_reuses_current_coverage_without_rewriting_personal_history():
    from cloud_platform.modules.hourly.domain import OperatorHourlyCutoverCoverage

    row, price, funding, periods, wallets, service = fixture()
    bound = await service.prepare_operator_first_hour(row, price)
    paid = await service.capture_operator_first_hour(row)
    legacy = SimpleNamespace(funding_source=FundingSource.CUSTOMER_WALLET, status="paid")
    periods.rows[(row.id, row.created_at)] = legacy
    receipt = OperatorHourlyCutoverCoverage(
        row.id,
        row.user_id,
        bound.period_start,
        bound.period_end,
        bound.idempotency_key,
        expense_operation_key(bound.idempotency_key),
        bound.operator_grant_id,
        paid.operator_expense_id,
    )
    periods.get_operator_cutover_coverage = AsyncMock(return_value=receipt)
    row.created_at -= timedelta(days=5)
    original_created = row.created_at
    funding.authorized = False
    assert await service.capture_operator_first_hour(row) == receipt
    funding.authorized = True
    await service.ensure_operator_coverage(row)
    assert periods.rows == {(row.id, bound.period_start): legacy}
    assert row.created_at == original_created and row.prepaid_paid_until == receipt.period_end
    assert len(funding.expenses) == 1
    wallets.get.assert_not_called()


async def test_cutover_receipt_with_changed_cost_cannot_activate():
    from cloud_platform.modules.hourly.domain import OperatorHourlyCutoverCoverage

    row, price, funding, periods, _, service = fixture()
    bound = await service.prepare_operator_first_hour(row, price)
    paid = await service.capture_operator_first_hour(row)
    periods.rows.clear()
    periods.get_operator_cutover_coverage = AsyncMock(
        return_value=OperatorHourlyCutoverCoverage(
            row.id,
            row.user_id,
            bound.period_start,
            bound.period_end,
            bound.idempotency_key,
            expense_operation_key(bound.idempotency_key),
            bound.operator_grant_id,
            paid.operator_expense_id,
        )
    )
    row.created_at -= timedelta(days=2)
    key = expense_operation_key(bound.idempotency_key)
    funding.expenses[key] = replace(funding.expenses[key], nominal_amount_minor=999)
    with pytest.raises(ValueError, match="immutable funding facts"):
        await service.ensure_operator_coverage(row)


async def test_stale_customer_branch_refreshes_operator_source_before_wallet_read():
    from cloud_platform.modules.billing.service import AccrualJob

    row, _, _, _, wallets, _ = fixture()
    stale = replace(row, funding_source=FundingSource.CUSTOMER_WALLET, funding_operator_id=None)
    corporate_billing = SimpleNamespace(ensure_operator_coverage=AsyncMock(return_value=row))
    accrual = AccrualJob(
        server_repo=SimpleNamespace(get=AsyncMock(return_value=row)),
        wallet_repo=wallets,
        hold_repo=SimpleNamespace(),
        hold_service=SimpleNamespace(),
        ledger_repo=SimpleNamespace(),
        accrual_repo=SimpleNamespace(),
        snapshot_repo=SimpleNamespace(),
        audit_repo=AsyncMock(),
        operator_billing=corporate_billing,
    )
    assert await accrual.prepay_server(stale) is row
    assert stale.funding_source is FundingSource.OPERATOR
    corporate_billing.ensure_operator_coverage.assert_awaited_once_with(row)
    wallets.get.assert_not_called()


async def test_operator_provisioning_can_be_suspended_without_fake_running_transition():
    from cloud_platform.modules.operations.service import PowerCommandService
    from tests.unit.test_prepaid_provider_lifecycle import Operations, dependencies

    row, *_ = fixture()
    row.provider_key = "test"
    row.state = ServerLifecycleState.PROVISIONING
    lifecycle, repo, wallets, provider, _, delete, registry = dependencies(row)
    operations = Operations()
    lifecycle._power = PowerCommandService(
        server_repo=repo,
        operation_repo=operations,
        provider_registry=registry,
        audit_repo=AsyncMock(),
    )
    provider_id = row.provider_server_id
    await lifecycle.suspend_operator(row.id)
    await lifecycle.suspend_operator(row.id)
    assert row.state is ServerLifecycleState.STOPPED and row.provider_server_id == provider_id
    assert len(provider.power_calls) == 1
    operation = next(iter(operations.items.values()))
    assert operation.status.value == "completed"
    assert operation.provider_response["operator_funding_suspension"] is True
    assert operation.provider_response["reason"]
    wallets.get.assert_not_called()
    delete.request.assert_not_called()


async def test_public_poweroff_cannot_use_operator_provisioning_suspension_bypass():
    from cloud_platform.modules.operations.service import (
        PowerActionNotAllowedError,
        PowerCommandService,
    )
    from tests.unit.test_prepaid_provider_lifecycle import Operations, dependencies

    row, *_ = fixture()
    row.provider_key = "test"
    row.state = ServerLifecycleState.PROVISIONING
    _, repo, _, provider, _, _, registry = dependencies(row)
    commands = PowerCommandService(
        server_repo=repo,
        operation_repo=Operations(),
        provider_registry=registry,
        audit_repo=AsyncMock(),
    )
    with pytest.raises(PowerActionNotAllowedError):
        await commands.power_off(row.user_id, row.id, "public-provisioning-off")
    assert not provider.power_calls and row.state is ServerLifecycleState.PROVISIONING


def test_operator_suspension_domain_method_rejects_customer_and_wrong_provider_identity():
    row, *_ = fixture()
    row.state = ServerLifecycleState.PROVISIONING
    with pytest.raises(ValueError, match="operator suspension"):
        row.mark_operator_suspended(provider_server_id="other-resource")
    row.funding_source = FundingSource.CUSTOMER_WALLET
    row.funding_operator_id = None
    with pytest.raises(ValueError, match="operator suspension"):
        row.mark_operator_suspended(provider_server_id=row.provider_server_id)
    assert row.state is ServerLifecycleState.PROVISIONING
