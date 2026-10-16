"""Operator monthly lifecycles must never touch a customer wallet."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import test_leaseweb_order_worker as orders
import test_monthly_checkout as checkout
import test_renewal_checker as renewals

from cloud_platform.modules.checkout.service import (
    MonthlyCheckoutService,
    OfferCatalogViewService,
    monthly_provider_cost,
)
from cloud_platform.modules.funding.domain import (
    FundingAuthorizationError,
    FundingGrantState,
    FundingSource,
    OperatorExpense,
    OperatorFundingGrant,
    ProviderCostSnapshot,
    expense_operation_key,
)
from cloud_platform.modules.funding.service import FundingService
from cloud_platform.modules.orders.domain import SettlementStatus
from cloud_platform.modules.orders.service import OrderSettlementService, SettlementVerdict
from cloud_platform.modules.renewals.domain import RenewalStatus
from cloud_platform.modules.renewals.service import RenewalChecker
from cloud_platform.providers.base import Capability, ProviderServer


class ForbiddenWallet:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"operator path accessed customer wallet API: {name}")


class FundingLedger:
    """An atomic typed ledger, independent of wallet fixtures."""

    def __init__(self, owner: Any) -> None:
        self.owner = owner
        self.enabled = True
        self.grants: dict[str, OperatorFundingGrant] = {}
        self.expenses: dict[str, OperatorExpense] = {}
        self.reserves = 0
        self.lock = asyncio.Lock()

    async def eligible(self, owner_id: Any) -> bool:
        return self.enabled and owner_id == self.owner

    async def reserve(self, **facts: Any) -> OperatorFundingGrant:
        async with self.lock:
            self.reserves += 1
            assert facts.pop("reason").strip()
            if not await self.eligible(facts["owner_id"]) or facts["operator_id"] != self.owner:
                raise FundingAuthorizationError("operator revoked")
            key = facts["operation_key"]
            existing = self.grants.get(key)
            if existing is not None:
                return existing
            grant = OperatorFundingGrant(
                id=uuid4(),
                operator_user_id=facts["operator_id"],
                owner_user_id=facts["owner_id"],
                operation_key=key,
                resource_key=facts["resource_key"],
                purpose=facts["purpose"],
                nominal_amount_minor=facts["nominal_amount_minor"],
                nominal_currency=facts["nominal_currency"],
                provider_cost_snapshot=facts["provider_cost_snapshot"],
                created_at=datetime.now(UTC),
            )
            self.grants[key] = grant
            return grant

    async def get_grant(self, key: str) -> OperatorFundingGrant | None:
        return self.grants.get(key)

    async def get_expense(self, key: str) -> OperatorExpense | None:
        return self.expenses.get(key)

    async def settle(self, grant: OperatorFundingGrant) -> OperatorExpense:
        async with self.lock:
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


async def test_own_checkout_with_no_wallet_replays_one_authorization() -> None:
    original, deps = checkout._make_service(offer=checkout._offer(), balance=0)
    ledger = FundingLedger(checkout.USER_ID)
    service = MonthlyCheckoutService(
        server_repo=deps["servers"],
        offers_repo=original._offers,
        account_repo=original._accounts,
        wallet_repo=ForbiddenWallet(),
        hold_repo=ForbiddenWallet(),
        orders_repo=deps["orders"],
        operation_repo=deps["ops"],
        audit_repo=checkout.FakeAuditRepo(),
        provider_registry=original._registry,
        funding_service=FundingService(ledger),
    )
    first = await service.create_order(
        user=checkout._user(),
        offer_id=checkout.OFFER_ID,
        os_name="Ubuntu 24.04",
        idempotency_key="own-monthly",
    )
    replay = await service.create_order(
        user=checkout._user(),
        offer_id=checkout.OFFER_ID,
        os_name="Ubuntu 24.04",
        idempotency_key="own-monthly",
    )
    assert first.server.funding_source is FundingSource.OPERATOR
    assert first.order.funding_source is FundingSource.OPERATOR
    assert first.hold is replay.hold is None
    assert replay.server.id == first.server.id and replay.replayed
    assert len(ledger.grants) == 1 and not ledger.expenses
    assert deps["ordering"].place_order_calls == 0


async def test_customer_owner_still_uses_wallet_with_an_operator_service() -> None:
    service, deps = checkout._make_service(offer=checkout._offer())
    ledger = FundingLedger(uuid4())
    service._funding = FundingService(ledger)
    result = await service.create_order(
        user=checkout._user(),
        offer_id=checkout.OFFER_ID,
        os_name="Ubuntu 24.04",
        idempotency_key="customer-monthly",
    )
    assert result.server.funding_source is FundingSource.CUSTOMER_WALLET
    assert result.hold is not None and len(deps["holds"].holds) == 1
    assert not ledger.grants


async def test_own_monthly_confirmation_does_not_read_or_invent_a_wallet_balance() -> None:
    original, _ = checkout._make_service(offer=checkout._offer())
    service = OfferCatalogViewService(
        original._offers,
        original._registry,
        ForbiddenWallet(),
        "test-signing-key",
        funding_service=FundingService(FundingLedger(checkout.USER_ID)),
    )
    service.os_by_index = AsyncMock(return_value="Ubuntu 24.04")
    view = await service.confirmation(
        user_id=checkout.USER_ID,
        offer_id=checkout.OFFER_ID,
        os_index=0,
    )
    assert view.funding_source is FundingSource.OPERATOR and view.sufficient
    assert view.balance_minor == 0


def operator_order() -> tuple[Any, Any, Any, FundingLedger]:
    server = orders._server()
    server.funding_source = FundingSource.OPERATOR
    server.funding_operator_id = server.user_id
    offer = orders._offer()
    order = orders._order()
    order.funding_source = FundingSource.OPERATOR
    order.funding_operator_id = server.user_id
    order.funding_grant_operation_key = order.operation_key
    order.provider_cost_snapshot = monthly_provider_cost(offer).to_dict()
    order.selling_price_minor = offer.selling_price_minor
    order.selling_currency = offer.selling_currency
    return server, order, offer, FundingLedger(server.user_id)


def operator_worker(server: Any, order: Any, ledger: FundingLedger) -> tuple[Any, Any]:
    worker, deps = orders._worker(server=server, order=order)
    funding = FundingService(ledger)
    worker._funding = funding
    worker._wallets = ForbiddenWallet()
    worker._holds = ForbiddenWallet()
    worker._hold_service = ForbiddenWallet()
    worker._settlement = OrderSettlementService(
        wallet_repo=ForbiddenWallet(),
        hold_repo=ForbiddenWallet(),
        hold_service=ForbiddenWallet(),
        ledger_repo=ForbiddenWallet(),
        orders_repo=deps["orders"],
        operation_repo=deps["op_repo"],
        audit_repo=deps["audit"],
        funding_service=funding,
    )
    return worker, deps


async def test_worker_concurrent_dispatch_records_one_cost_without_wallet() -> None:
    server, order, _, ledger = operator_order()
    worker, deps = operator_worker(server, order, ledger)
    await asyncio.gather(worker.process_pending(), worker.process_pending())
    assert len(deps["ordering"].posts) == 1
    assert len(ledger.expenses) == 1
    assert order.settlement_status is SettlementStatus.COMPLETE
    await worker._settlement.ensure_order_payment_settled(order, server)
    assert len(ledger.expenses) == 1


async def test_revoked_operator_blocks_new_dispatch_before_provider_post() -> None:
    server, order, _, ledger = operator_order()
    worker, deps = operator_worker(server, order, ledger)
    ledger.enabled = False
    await worker.process_pending()
    assert not deps["ordering"].posts
    assert not ledger.expenses
    assert order.status is orders.OrderStatus.FAILED


async def test_revocation_during_provider_revalidation_still_blocks_dispatch() -> None:
    server, order, _, ledger = operator_order()
    worker, deps = operator_worker(server, order, ledger)
    original_read = deps["ordering"].get_product

    async def revoke_during_read(*args: Any) -> Any:
        detail = await original_read(*args)
        ledger.enabled = False
        return detail

    deps["ordering"].get_product = revoke_during_read
    await worker.process_pending()
    assert not deps["ordering"].posts and not ledger.expenses


async def test_settled_grant_cannot_authorize_a_second_provider_purchase() -> None:
    server, order, _, ledger = operator_order()
    worker, deps = operator_worker(server, order, ledger)
    await worker._authorize_dispatch(order, server)
    await ledger.settle(await ledger.get_grant(order.operation_key))
    await worker.process_pending()
    assert not deps["ordering"].posts
    assert len(ledger.expenses) == 1


async def test_direct_create_uses_the_same_expense_barrier() -> None:
    server, order, _, ledger = operator_order()
    worker, _ = operator_worker(server, order, ledger)

    class DirectProvider:
        key = "leaseweb"  # Capability dispatch must not depend on provider names.
        capabilities = frozenset({Capability.COMPUTE})

        def __init__(self) -> None:
            self.calls = 0

        async def create_server(self, request: Any, key: Any) -> ProviderServer:
            self.calls += 1
            return ProviderServer("native-direct-1", request.name, "running", ipv4="192.0.2.3")

    provider = DirectProvider()
    worker._registry = orders.ProviderRegistry()
    worker._registry.register(provider)
    await worker.process_pending()
    assert provider.calls == 1
    assert server.provider_server_id == "native-direct-1"
    assert order.settlement_status is SettlementStatus.COMPLETE
    assert len(ledger.expenses) == 1


async def test_accepted_cost_can_recover_after_revocation_and_restart() -> None:
    server, order, _, ledger = operator_order()
    worker, deps = operator_worker(server, order, ledger)
    await worker._authorize_dispatch(order, server)
    order.provider_order_id = "accepted-1"
    ledger.enabled = False
    # A new service instance models recovery in another process.
    restarted = OrderSettlementService(
        wallet_repo=ForbiddenWallet(),
        hold_repo=ForbiddenWallet(),
        hold_service=ForbiddenWallet(),
        ledger_repo=ForbiddenWallet(),
        orders_repo=deps["orders"],
        operation_repo=deps["op_repo"],
        audit_repo=deps["audit"],
        funding_service=FundingService(ledger),
    )
    assert await restarted.ensure_order_payment_settled(order, server) is SettlementVerdict.SETTLED
    assert await restarted.ensure_order_payment_settled(order, server) is SettlementVerdict.SETTLED
    assert len(ledger.expenses) == 1


async def test_fabricated_complete_status_does_not_replace_operator_receipt() -> None:
    server, order, _, ledger = operator_order()
    worker, _ = operator_worker(server, order, ledger)
    order.provider_order_id = "accepted-1"
    order.settlement_status = SettlementStatus.COMPLETE
    assert (
        await worker._settlement.ensure_order_payment_settled(order, server)
        is SettlementVerdict.NEEDS_REVIEW
    )
    assert not ledger.expenses


def operator_renewal() -> tuple[Any, Any, Any, FundingLedger]:
    now = datetime(2026, 10, 15, 12, tzinfo=UTC)
    server = renewals._server()
    server.funding_source = FundingSource.OPERATOR
    server.funding_operator_id = server.user_id
    record = renewals._record(now, status=RenewalStatus.PAYMENT_DUE)
    record.funding_source = FundingSource.OPERATOR
    record.funding_operator_id = server.user_id
    record.provider_cost_snapshot = ProviderCostSnapshot(
        "9.99", "EUR", "accepted-native-quote"
    ).to_dict()
    ledger = FundingLedger(server.user_id)
    checker = RenewalChecker(
        renewals_repo=renewals.FakeRenewalRepo([record]),
        notification_repo=renewals.FakeNotificationRepo(),
        server_repo=renewals.FakeServerRepo(server),
        wallet_repo=ForbiddenWallet(),
        hold_repo=ForbiddenWallet(),
        hold_service=ForbiddenWallet(),
        audit_repo=renewals.FakeAuditRepo(),
        clock=lambda: now,
        funding_service=FundingService(ledger),
    )
    return checker, record, server, ledger


async def test_operator_renewal_concurrent_replay_and_restart_has_one_period_cost() -> None:
    checker, record, server, ledger = operator_renewal()
    before = record.provider_renewal_at
    # Detached copies model two replicas reading the same period.
    await asyncio.gather(checker._check(replace(record)), checker._check(replace(record)))
    current = await checker.record_for(server.id)
    assert current.provider_renewal_at == before + timedelta(days=30)
    assert len(ledger.expenses) == 1
    assert (await checker._check(current)).action == "skipped"
    # Crash after the receipt but before the period CAS: recovery may advance
    # precisely that period, even if RBAC was revoked in the meantime.
    await checker._renewals.upsert(
        replace(
            record,
            provider_renewal_at=before,
            status=RenewalStatus.PAYMENT_DUE,
        )
    )
    ledger.enabled = False
    assert (await checker.settle_now(server.id)).reason == "already_charged"
    assert len(ledger.expenses) == 1


async def test_revocation_blocks_new_renewal_and_preserves_running_term() -> None:
    checker, record, server, ledger = operator_renewal()
    ledger.enabled = False
    previous = record.provider_renewal_at
    assert (await checker._check(record)).action == "operator_authorization_required"
    assert record.provider_renewal_at == previous
    assert server.state is renewals.ServerLifecycleState.RUNNING
    assert not ledger.grants and not ledger.expenses


async def test_operator_quote_keeps_exact_native_precision_and_rejects_float() -> None:
    offer = orders._offer()
    offer = replace(offer, billing_parameters={"provider_monthly_rate": "9.990013"})
    assert monthly_provider_cost(offer).amount_exact == "9.990013"
    offer = replace(offer, billing_parameters={"provider_monthly_rate": 9.99})
    with pytest.raises(checkout.OfferUnavailableError):
        monthly_provider_cost(offer)


async def test_period_cas_loss_never_rewinds_the_newer_term() -> None:
    checker, record, server, ledger = operator_renewal()
    before = record.provider_renewal_at
    stale = replace(record)
    await checker._check(record)
    current = await checker.record_for(server.id)
    later = replace(current, provider_renewal_at=before + timedelta(days=60))
    await checker._renewals.upsert(later)
    outcome = await checker._settle(stale, server, 0, before, before.replace(hour=0))
    assert outcome.action == "charged"
    assert (await checker.record_for(server.id)).provider_renewal_at == later.provider_renewal_at
    assert len(ledger.expenses) == 1
