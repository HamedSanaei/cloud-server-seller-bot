"""Monthly renewal domain (LEASEWEB-MVP).

One :class:`RenewalRecord` per prepaid monthly server tracks the provider
contract reference, the customer renewal price and the next renewal
instant. The daily :class:`~cloud_platform.modules.renewals.service.RenewalChecker`
uses it to send the 7/3/1-day warnings and to post EXACTLY ONE monthly
renewal debit per period under a deterministic idempotency key.

Leaseweb renews services automatically at its own billing; the current VPS
API has no verified cancel endpoint, so unpaid services are flagged
``manual_cancellation_required`` and handed to the operator runbook — never
silently renewed at our expense.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class RenewalStatus(StrEnum):
    ACTIVE = "active"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    MANUAL_CANCELLATION_REQUIRED = "manual_cancellation_required"
    CANCELLED = "cancelled"


class RenewalKind(StrEnum):
    """Dedup kinds for renewal notifications (unique per server/kind/period)."""

    WARN_7D = "warn_7d"
    WARN_3D = "warn_3d"
    WARN_1D = "warn_1d"
    ADMIN_INSUFFICIENT = "admin_insufficient"
    MANUAL_CANCELLATION = "manual_cancellation"
    CHARGED = "charged"


class RenewalError(Exception):
    """Base error for renewal operations."""


@dataclass(slots=True)
class RenewalRecord:
    """The renewal facts of one prepaid monthly server."""

    server_id: UUID
    purchased_at: datetime
    customer_price_minor: int
    currency: str
    provider_contract_id: str | None = None
    provider_order_ref: str | None = None
    provider_renewal_at: datetime | None = None
    renewal_date_estimated: bool = False
    status: RenewalStatus = RenewalStatus.ACTIVE
    auto_charge_enabled: bool = True
    last_checked_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.customer_price_minor <= 0:
            raise ValueError("customer_price_minor must be positive")
        if not self.currency or len(self.currency) != 3:
            raise ValueError("currency must be a 3-letter ISO code")

    @property
    def needs_attention(self) -> bool:
        """Unpaid-risk services the operator must look at."""
        return self.status in (
            RenewalStatus.INSUFFICIENT_FUNDS,
            RenewalStatus.MANUAL_CANCELLATION_REQUIRED,
        )


class RenewalRepository(Protocol):
    """Port for renewal persistence."""

    async def get(self, server_id: UUID) -> RenewalRecord | None: ...

    async def upsert(self, record: RenewalRecord) -> RenewalRecord: ...

    async def list_active(self) -> list[RenewalRecord]:
        """Every non-cancelled record (daily checker candidates)."""
        ...

    async def list_needing_attention(self, limit: int = 100) -> list[RenewalRecord]:
        """Records flagged insufficient/manual-cancellation (admin queue)."""
        ...


class RenewalNotificationRepository(Protocol):
    """Exactly-once log for renewal notifications."""

    async def record(self, server_id: UUID, kind: RenewalKind, for_period: datetime) -> bool:
        """True only for the FIRST record of (server, kind, period)."""
        ...
