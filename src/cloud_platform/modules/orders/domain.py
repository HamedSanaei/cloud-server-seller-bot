"""Provider order lifecycle domain (LEASEWEB-MVP).

One **provider order** is the durable record of an asynchronous provider
provisioning request (e.g. Leaseweb ordering VPS). It exists BEFORE any
chargeable provider call, is 1:1 with a platform server, and carries the
deterministic operation key that doubles as the IdempotencyKey sent to the
provider — so a worker crash, a callback replay or a process restart can
never place a second order.

Status machine (coarse, reconciliation-driven):

    PENDING_SUBMIT -> SUBMITTED (provider order id recorded)
    SUBMITTED -> PROVISIONING (provider accepted, resource pending)
    PROVISIONING -> ACTIVE (provider resource discovered)
    SUBMITTED/PROVISIONING -> FAILED (definitive provider rejection)
    any active -> NEEDS_REVIEW (ambiguous state for a human)
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
    NEEDS_REVIEW = "needs_review"  # ambiguous; human must decide


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

    def __post_init__(self) -> None:
        if not self.operation_key or not self.operation_key.strip():
            raise ValueError("operation_key must not be empty")
        if not self.provider_key or not self.provider_key.strip():
            raise ValueError("provider_key must not be empty")
        if self.attempts < 0:
            raise ValueError("attempts must not be negative")

    @property
    def is_open(self) -> bool:
        return self.status in _OPEN

    def mark_submitted(self, provider_order_id: str, *, contract_id: str | None = None) -> None:
        """Record the provider order id (accepted)."""
        if self.status not in (
            OrderStatus.PENDING_SUBMIT,
            OrderStatus.SUBMITTED,
            OrderStatus.PROVISIONING,
        ):
            raise OrderStateConflict(f"order {self.id}: cannot submit from {self.status.value}")
        self.provider_order_id = provider_order_id
        if contract_id:
            self.provider_contract_id = contract_id
        if self.status is OrderStatus.PENDING_SUBMIT:
            self.status = OrderStatus.SUBMITTED
        self.error = None

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
    ) -> ProviderOrder:
        """Create the PENDING_SUBMIT row (unique per server + key)."""
        ...

    async def save(self, order: ProviderOrder) -> ProviderOrder: ...

    async def list_open(self, provider_key: str, limit: int = 100) -> list[ProviderOrder]:
        """SUBMITTED + PROVISIONING orders (reconciler candidates)."""
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
