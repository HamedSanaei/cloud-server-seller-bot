"""Domain tests for the MANUAL-only provider-order resolution transitions
(LEASEWEB-MVP release hardening)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from cloud_platform.modules.orders.domain import (
    OrderStateConflict,
    OrderStatus,
    ProviderOrder,
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
