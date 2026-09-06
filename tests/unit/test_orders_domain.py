"""Domain tests for the MANUAL-only provider-order resolution transitions
(LEASEWEB-MVP release hardening)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from cloud_platform.modules.orders.domain import (
    OrderStateConflict,
    OrderStatus,
    ProviderOrder,
    SettlementStatus,
)


def _order(status: OrderStatus) -> ProviderOrder:
    return ProviderOrder(
        id=uuid4(),
        server_id=uuid4(),
        operation_key="order-create:server",
        provider_key="leaseweb",
        status=status,
        provider_cost_minor=999,
        provider_cost_currency="EUR",
        contract_term="1_MONTH",
        billing_cycle="1_MONTH",
    )


class TestResolveSubmitted:
    @pytest.mark.parametrize("start", [OrderStatus.OUTCOME_UNKNOWN, OrderStatus.NEEDS_REVIEW])
    def test_attaches_id_from_ambiguous_states(self, start: OrderStatus) -> None:
        order = _order(start)
        order.error = "ambiguous outcome"
        order.resolve_submitted("LS-ORD-9")
        assert order.status is OrderStatus.SUBMITTED
        assert order.provider_order_id == "LS-ORD-9"
        assert order.error is None

    @pytest.mark.parametrize(
        "start",
        [
            OrderStatus.PENDING_SUBMIT,
            OrderStatus.SUBMITTED,
            OrderStatus.PROVISIONING,
            OrderStatus.ACTIVE,
            OrderStatus.FAILED,
        ],
    )
    def test_refused_outside_ambiguous_states(self, start: OrderStatus) -> None:
        order = _order(start)
        with pytest.raises(OrderStateConflict):
            order.resolve_submitted("LS-ORD-9")
        assert order.status is start
        assert order.provider_order_id is None


class TestResetToPendingSubmit:
    @pytest.mark.parametrize(
        "start",
        [OrderStatus.FAILED, OrderStatus.OUTCOME_UNKNOWN, OrderStatus.NEEDS_REVIEW],
    )
    def test_returns_to_retryable_queue(self, start: OrderStatus) -> None:
        order = _order(start)
        order.error = "some error"
        order.reset_to_pending_submit()
        assert order.status is OrderStatus.PENDING_SUBMIT
        assert order.error is None
        assert order.operation_key  # SAME local identity, never regenerated

    @pytest.mark.parametrize(
        "start",
        [
            OrderStatus.PENDING_SUBMIT,
            OrderStatus.SUBMITTED,
            OrderStatus.PROVISIONING,
            OrderStatus.ACTIVE,
        ],
    )
    def test_refused_outside_retryable_origins(self, start: OrderStatus) -> None:
        order = _order(start)
        with pytest.raises(OrderStateConflict):
            order.reset_to_pending_submit()
        assert order.status is start


class TestSettlementFields:
    def test_defaults_are_safe(self) -> None:
        order = _order(OrderStatus.PENDING_SUBMIT)
        assert order.settlement_status is SettlementStatus.PENDING
        assert order.settlement_attempts == 0
        assert order.settlement_attempted_at is None
        assert order.settlement_error is None

    def test_negative_attempts_rejected(self) -> None:
        with pytest.raises(ValueError):
            ProviderOrder(
                id=uuid4(),
                server_id=uuid4(),
                operation_key="order-create:server",
                provider_key="leaseweb",
                status=OrderStatus.PENDING_SUBMIT,
                settlement_attempts=-1,
            )


class TestAutomatedTransitionGuards:
    """Automated transitions must not smuggle in manual-only behaviors."""

    def test_mark_submitted_illegal_from_manual_states(self) -> None:
        for start in (OrderStatus.NEEDS_REVIEW, OrderStatus.FAILED, OrderStatus.ACTIVE):
            order = _order(start)
            with pytest.raises(OrderStateConflict):
                order.mark_submitted("LS-ORD-1")
            assert order.provider_order_id is None

    def test_mark_submitted_allows_unknown_outcome(self) -> None:
        order = _order(OrderStatus.OUTCOME_UNKNOWN)
        order.mark_submitted("LS-ORD-1")
        assert order.status is OrderStatus.SUBMITTED
        assert order.provider_order_id == "LS-ORD-1"

    def test_mark_outcome_unknown_refused_after_activation(self) -> None:
        order = _order(OrderStatus.ACTIVE)
        with pytest.raises(OrderStateConflict):
            order.mark_outcome_unknown("late ambiguity")

    def test_mark_provisioning_refused_from_pending(self) -> None:
        order = _order(OrderStatus.PENDING_SUBMIT)
        with pytest.raises(OrderStateConflict):
            order.mark_provisioning()

    def test_mark_active_refused_from_pending_or_unknown(self) -> None:
        for start in (OrderStatus.PENDING_SUBMIT, OrderStatus.OUTCOME_UNKNOWN):
            order = _order(start)
            with pytest.raises(OrderStateConflict):
                order.mark_active()

    def test_mark_failed_idempotent_and_guarded(self) -> None:
        order = _order(OrderStatus.FAILED)
        order.mark_failed("again")
        assert order.status is OrderStatus.FAILED
        for start in (OrderStatus.ACTIVE, OrderStatus.OUTCOME_UNKNOWN):
            with pytest.raises(OrderStateConflict):
                _order(start).mark_failed("boom")

    def test_mark_needs_review_idempotent(self) -> None:
        order = _order(OrderStatus.NEEDS_REVIEW)
        order.error = "first reason"
        order.mark_needs_review("still")
        assert order.status is OrderStatus.NEEDS_REVIEW
        assert order.error == "first reason"  # idempotent: first reason kept

    def test_reset_refused_for_open_submitted(self) -> None:
        order = _order(OrderStatus.SUBMITTED)
        with pytest.raises(OrderStateConflict):
            order.reset_to_pending_submit()
