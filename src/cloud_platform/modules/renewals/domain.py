"""Commercial service lifecycle (LEASEWEB-MVP + PROD-HARDENING §17-§31).

One :class:`RenewalRecord` per prepaid monthly server tracks the provider
contract reference, the LEGACY sale price, the next renewal instant and the
COMMERCIAL state of the service. The daily
:class:`~cloud_platform.modules.renewals.service.RenewalChecker` uses it to
send warnings, post EXACTLY ONE renewal debit per period under a deterministic
idempotency key, run the grace period and hand the operator anything it cannot
settle.

Two state machines, never conflated
------------------------------------

**Infrastructure state** (``CloudServer.state``) is what the PROVIDER says the
machine is doing: ``running``, ``stopped``, ``provisioning``, ``unknown``. The
provider API is the only authority for it.

:class:`RenewalStatus` below is the **commercial state**: whether the customer
has paid for the current period. A VPS can be ``RUNNING`` and ``PAYMENT_DUE``
at the same time, and that is a normal, expected combination — nothing here
stops, reinstalls or deletes a provider server.

Provider separation
-------------------

Leaseweb renews ITS OWN contract with us at its own billing cycle, and the
modern VPS API exposes no renewal and no cancellation operation. So: customer
wallet collection is a purely local concern (no provider call), and an unpaid
service that exhausts grace becomes ``SUSPENDED`` — never "cancelled" — with
the operator runbook owning any actual termination.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class RenewalStatus(StrEnum):
    """The COMMERCIAL state of a service (never the provider's state).

    Transitions::

        ACTIVE
          | (inside the charge window / expiry reached)
          v
        PAYMENT_DUE --wallet settled--> ACTIVE (next period)
          | insufficient funds
          v
        GRACE_PERIOD --wallet settled--> ACTIVE
          | grace expires (policy)
          v
        SUSPENDED --wallet settled--> ACTIVE

    ``INSUFFICIENT_FUNDS`` and ``MANUAL_CANCELLATION_REQUIRED`` are the
    legacy/operator-queue states kept for existing rows; ``EXPIRED`` marks a
    local service whose provider contract is over while records are retained.
    """

    ACTIVE = "active"
    PAYMENT_DUE = "payment_due"
    GRACE_PERIOD = "grace_period"
    SUSPENDED = "suspended"
    EXPIRED = "expired"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    MANUAL_CANCELLATION_REQUIRED = "manual_cancellation_required"
    CANCELLED = "cancelled"


#: The commercial states in which a customer may still settle the period.
PAYABLE_STATUSES: frozenset[RenewalStatus] = frozenset(
    {
        RenewalStatus.PAYMENT_DUE,
        RenewalStatus.GRACE_PERIOD,
        RenewalStatus.INSUFFICIENT_FUNDS,
        RenewalStatus.SUSPENDED,
    }
)

#: The commercial states the operator must look at (unpaid exposure).
ATTENTION_STATUSES: frozenset[RenewalStatus] = frozenset(
    {
        RenewalStatus.INSUFFICIENT_FUNDS,
        RenewalStatus.MANUAL_CANCELLATION_REQUIRED,
        RenewalStatus.SUSPENDED,
    }
)


class RenewalKind(StrEnum):
    """Dedup kinds for renewal notifications (unique per server/kind/period).

    ``WARN_<n>H`` mirrors the configured thresholds (``[commerce.renewal]
    warning_before_expiry_hours``); the legacy ``WARN_7D/3D/1D`` values remain
    so existing notification rows stay valid.
    """

    WARN_7D = "warn_7d"
    WARN_3D = "warn_3d"
    WARN_1D = "warn_1d"
    WARN_168H = "warn_168h"
    WARN_72H = "warn_72h"
    WARN_24H = "warn_24h"
    ADMIN_INSUFFICIENT = "admin_insufficient"
    MANUAL_CANCELLATION = "manual_cancellation"
    CHARGED = "charged"
    GRACE_STARTED = "grace_started"
    GRACE_EXPIRED = "grace_expired"
    SUSPENDED = "suspended"
    RENEWAL_DUE = "renewal_due"

    @classmethod
    def for_warning_hours(cls, hours: int) -> RenewalKind:
        """The dedup kind for a configured warning threshold."""
        known = {
            168: cls.WARN_168H,
            72: cls.WARN_72H,
            24: cls.WARN_24H,
        }
        return known.get(hours, cls.WARN_24H)


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
    #: Customer's automatic-renewal preference (durable, DB-backed).
    auto_charge_enabled: bool = True
    #: End of the payable window once the period expired unpaid (grace only).
    grace_until: datetime | None = None
    last_checked_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.customer_price_minor <= 0:
            raise ValueError("customer_price_minor must be positive")
        if not self.currency or len(self.currency) != 3:
            raise ValueError("currency must be a 3-letter ISO code")

    @property
    def needs_attention(self) -> bool:
        """Unpaid-risk services the operator must look at."""
        return self.status in ATTENTION_STATUSES

    @property
    def payable(self) -> bool:
        """Whether the customer can still settle this period."""
        return self.status in PAYABLE_STATUSES

    @property
    def period_end(self) -> datetime | None:
        """The period the customer is currently paying for."""
        return self.provider_renewal_at


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

    async def set_auto_renew(self, server_id: UUID, enabled: bool) -> RenewalRecord:
        """Persist the customer's automatic-renewal preference."""
        ...

    async def lock_for_update(self, server_id: UUID) -> RenewalRecord | None:
        """Read one record with a row lock (serialises concurrent renewal runs).

        Concurrency protection for money lives in the DATABASE, never in Redis:
        two workers renewing the same service must contend on one row so only
        one of them attempts the ledger write.
        """
        ...


class RenewalNotificationRepository(Protocol):
    """Exactly-once log for renewal notifications."""

    async def record(self, server_id: UUID, kind: RenewalKind, for_period: datetime) -> bool:
        """True only for the FIRST record of (server, kind, period)."""
        ...
