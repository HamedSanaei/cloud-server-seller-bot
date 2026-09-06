"""Provider order lifecycle domain (LEASEWEB-MVP).

One **provider order** is the durable record of an asynchronous provider
provisioning request (e.g. Leaseweb ordering VPS). It exists BEFORE any
chargeable provider call, is 1:1 with a platform server, and carries the
deterministic operation key that doubles as the IdempotencyKey sent to the
provider — so a worker crash, a callback replay or a process restart can
never place a second order.

The row also snapshots every fact needed to correlate the order back to
Leaseweb AFTER a lost response: exact provider product id, location, OS,
contract term, billing cycle, the PROVIDER cost (never derived from the
customer selling price) and the customer selling price. These snapshots
are committed BEFORE the chargeable POST (release hardening).

Status machine (coarse, reconciliation-driven):

    PENDING_SUBMIT -> SUBMITTED (provider order id recorded)
    SUBMITTED -> PROVISIONING (provider accepted, resource pending)
    PROVISIONING -> ACTIVE (provider resource discovered)
    SUBMITTED/PROVISIONING -> FAILED (definitive provider rejection)
    any active -> NEEDS_REVIEW (ambiguous state for a human)
    PENDING_SUBMIT/SUBMITTED -> OUTCOME_UNKNOWN (a billable POST was sent
        but its result is unknown; NEVER re-POSTed automatically — a
        READ-ONLY recovery scan (or a human) resolves it)
    OUTCOME_UNKNOWN -> SUBMITTED (recovery attached exactly one order)
    OUTCOME_UNKNOWN -> NEEDS_REVIEW (recovery ambiguous / no proof)

**Payment settlement** is a SEPARATE durable sub-state (``settlement_status``):
provider acceptance and local charge settlement are different facts. The
provider order id is persisted first and NEVER lost; the wallet hold is
captured exactly once (idempotent, CHARGE-repairing) and must be COMPLETE
before any activation/delivery. A capture failure never marks the provider
order failed, never releases the hold and never re-POSTs — the local
settlement is retried by the reconciler.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class OrderStatus(StrEnum):
    PENDING_SUBMIT = "pending_submit"  # intent persisted, not yet POSTed
    SUBMITTED = "submitted"  # provider order id recorded
    PROVISIONING = "provisioning"  # provider accepted; resource pending
    ACTIVE = "active"  # provider resource discovered (server id known)
    FAILED = "failed"  # definitive rejection before acceptance
    OUTCOME_UNKNOWN = "outcome_unknown"  # billable POST sent, result unknown
    NEEDS_REVIEW = "needs_review"  # ambiguous; human must decide


class SettlementStatus(StrEnum):
    """Durable local settlement state of an accepted provider order.

    Kept separate from :class:`OrderStatus`: the provider purchase may be
    fully accepted while the local wallet capture is still pending or in
    need of financial review. Activation/delivery is only allowed from
    COMPLETE.
    """

    PENDING = "pending"  # accepted but not yet locally settled
    COMPLETE = "complete"  # hold CAPTURED and exactly one CHARGE ledger entry
    NEEDS_REVIEW = "needs_review"  # hold missing/released/unsettleable


#: Statuses the reconciler keeps polling.
_OPEN = frozenset({OrderStatus.SUBMITTED, OrderStatus.PROVISIONING})


class OrderError(Exception):
    """Base error for provider-order operations."""


class OrderStateConflict(OrderError):
    """An illegal order status transition."""


@dataclass(slots=True)
class ProviderOrder:
    """One provider-side provisioning order."""

    id: UUID
    server_id: UUID
    operation_key: str
    provider_key: str
    #: The sellable offer the order was placed from (price + product pin).
    offer_id: UUID | None = None
    status: OrderStatus = OrderStatus.PENDING_SUBMIT
    provider_order_id: str | None = None
    delivery_estimate: str | None = None
    provider_contract_id: str | None = None
    provider_service_id: str | None = None
    error: str | None = None
    attempts: int = 0
    last_polled_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    #: Order-fact snapshots committed BEFORE the chargeable POST (see module
    #: docstring). These are what a read-only recovery scan correlates on.
    product_id: str | None = None
    location_id: str | None = None
    os_name: str | None = None
    contract_term: str | None = None
    billing_cycle: str | None = None
    provider_cost_minor: int | None = None
    provider_cost_currency: str | None = None
    selling_price_minor: int | None = None
    selling_currency: str | None = None
    #: When the chargeable POST was sent (claim time); the recovery scan
    #: window starts here. Set before POSTing, persisted on any outcome.
    post_attempted_at: datetime | None = None
    #: Local payment settlement (release hardening): the provider purchase
    #: and the local charge are different facts. Delivery is blocked until
    #: settlement is COMPLETE (hold CAPTURED + CHARGE ledger entry).
    settlement_status: SettlementStatus = SettlementStatus.PENDING
    settlement_attempted_at: datetime | None = None
    settlement_attempts: int = 0
    settlement_error: str | None = None

    def __post_init__(self) -> None:
        if not self.operation_key or not self.operation_key.strip():
            raise ValueError("operation_key must not be empty")
        if not self.provider_key or not self.provider_key.strip():
            raise ValueError("provider_key must not be empty")
        if self.attempts < 0:
            raise ValueError("attempts must not be negative")
        if self.settlement_attempts < 0:
            raise ValueError("settlement_attempts must not be negative")

    @property
    def is_open(self) -> bool:
        return self.status in _OPEN

    def mark_submitted(self, provider_order_id: str, *, contract_id: str | None = None) -> None:
        """Record the provider order id (accepted)."""
        if self.status not in (
            OrderStatus.PENDING_SUBMIT,
            OrderStatus.SUBMITTED,
            OrderStatus.PROVISIONING,
            OrderStatus.OUTCOME_UNKNOWN,
        ):
            raise OrderStateConflict(f"order {self.id}: cannot submit from {self.status.value}")
        self.provider_order_id = provider_order_id
        if contract_id:
            self.provider_contract_id = contract_id
        if self.status in (OrderStatus.PENDING_SUBMIT, OrderStatus.OUTCOME_UNKNOWN):
            self.status = OrderStatus.SUBMITTED
        self.error = None

    def mark_outcome_unknown(self, error: str) -> None:
        """A billable POST was sent but its result is unknown (release
        hardening). The order is NEVER automatically re-POSTed from this
        state; a read-only recovery scan or a human resolves it."""
        if self.status is OrderStatus.OUTCOME_UNKNOWN:
            self.error = error
            return
        if self.status not in (
            OrderStatus.PENDING_SUBMIT,
            OrderStatus.SUBMITTED,
        ):
            raise OrderStateConflict(
                f"order {self.id}: cannot mark outcome unknown from {self.status.value}"
            )
        self.status = OrderStatus.OUTCOME_UNKNOWN
        self.error = error

    def mark_provisioning(self, *, delivery_estimate: str | None = None) -> None:
        if self.status not in (OrderStatus.SUBMITTED, OrderStatus.PROVISIONING):
            raise OrderStateConflict(
                f"order {self.id}: cannot mark provisioning from {self.status.value}"
            )
        self.status = OrderStatus.PROVISIONING
        if delivery_estimate:
            self.delivery_estimate = delivery_estimate

    def mark_active(
        self,
        *,
        provider_contract_id: str | None = None,
        provider_service_id: str | None = None,
    ) -> None:
        if self.status not in (
            OrderStatus.SUBMITTED,
            OrderStatus.PROVISIONING,
            OrderStatus.ACTIVE,
        ):
            raise OrderStateConflict(f"order {self.id}: cannot activate from {self.status.value}")
        self.status = OrderStatus.ACTIVE
        if provider_contract_id:
            self.provider_contract_id = provider_contract_id
        if provider_service_id:
            self.provider_service_id = provider_service_id
        self.error = None

    def mark_failed(self, error: str) -> None:
        if self.status is OrderStatus.FAILED:
            return  # idempotent
        if self.status not in (
            OrderStatus.PENDING_SUBMIT,
            OrderStatus.SUBMITTED,
            OrderStatus.PROVISIONING,
        ):
            raise OrderStateConflict(f"order {self.id}: cannot fail from {self.status.value}")
        self.status = OrderStatus.FAILED
        self.error = error

    def mark_needs_review(self, error: str) -> None:
        if self.status is OrderStatus.NEEDS_REVIEW:
            return  # idempotent
        self.status = OrderStatus.NEEDS_REVIEW
        self.error = error

    def resolve_submitted(self, provider_order_id: str) -> None:
        """MANUAL resolution only: attach the provider order id a human
        verified at the provider (``resolve-existing``).

        Allowed from OUTCOME_UNKNOWN and NEEDS_REVIEW (ambiguous provenance)
        and moves the order to SUBMITTED so the normal reconciler takes over.
        Deliberately separate from :meth:`mark_submitted`: no automated path
        may attach an order id that was not confirmed by the provider POST.
        """
        if self.status not in (OrderStatus.OUTCOME_UNKNOWN, OrderStatus.NEEDS_REVIEW):
            raise OrderStateConflict(
                f"order {self.id}: cannot resolve-submit from {self.status.value}; "
                "only ambiguous orders (OUTCOME_UNKNOWN/NEEDS_REVIEW) may be "
                "resolved manually"
            )
        self.provider_order_id = provider_order_id
        self.status = OrderStatus.SUBMITTED
        self.error = None

    def reset_to_pending_submit(self) -> None:
        """MANUAL resolution only: return the order to the retryable queue.

        Used by ``orders retry`` (definitive FAILED) and
        ``resolve-not-created`` (ambiguous outcome whose non-creation a human
        verified). Keeps the same operation key / snapshots; only a human
        may invoke it, never an automated transition.
        """
        if self.status not in (
            OrderStatus.FAILED,
            OrderStatus.OUTCOME_UNKNOWN,
            OrderStatus.NEEDS_REVIEW,
        ):
            raise OrderStateConflict(
                f"order {self.id}: cannot reset to pending_submit from {self.status.value}"
            )
        self.status = OrderStatus.PENDING_SUBMIT
        self.error = None


class ProviderOrderRepository(Protocol):
    """Port for provider-order persistence."""

    async def get(self, order_id: UUID) -> ProviderOrder | None: ...

    async def get_by_server(self, server_id: UUID) -> ProviderOrder | None: ...

    async def get_by_operation_key(self, operation_key: str) -> ProviderOrder | None: ...

    async def create(
        self,
        *,
        server_id: UUID,
        operation_key: str,
        provider_key: str,
        offer_id: UUID,
        product_id: str | None = None,
        location_id: str | None = None,
        os_name: str | None = None,
        contract_term: str | None = None,
        billing_cycle: str | None = None,
        provider_cost_minor: int | None = None,
        provider_cost_currency: str | None = None,
        selling_price_minor: int | None = None,
        selling_currency: str | None = None,
    ) -> ProviderOrder:
        """Create the PENDING_SUBMIT row (unique per server + key) with the
        order-fact snapshots committed before any provider call."""
        ...

    async def save(self, order: ProviderOrder) -> ProviderOrder: ...

    async def list_open(self, provider_key: str, limit: int = 100) -> list[ProviderOrder]:
        """SUBMITTED + PROVISIONING orders (reconciler candidates)."""
        ...

    async def list_outcome_unknown(self, provider_key: str, limit: int = 50) -> list[ProviderOrder]:
        """OUTCOME_UNKNOWN orders (read-only recovery candidates)."""
        ...

    async def list_by_status(
        self, provider_key: str, status: OrderStatus, limit: int = 100
    ) -> list[ProviderOrder]: ...

    async def list_failed(self, provider_key: str, limit: int = 50) -> list[ProviderOrder]: ...

    async def list_needs_review(
        self, provider_key: str, limit: int = 50
    ) -> list[ProviderOrder]: ...

    async def list_attention(self, provider_key: str, limit: int = 100) -> list[ProviderOrder]:
        """FAILED + NEEDS_REVIEW orders (the operator attention queue)."""
        ...
