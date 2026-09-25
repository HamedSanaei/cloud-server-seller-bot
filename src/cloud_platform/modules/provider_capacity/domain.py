"""Durable provider ACCOUNT CAPACITY knowledge (LEASEWEB-MULTIACCOUNT).

A credential account can be perfectly authenticated, able to sell a location,
able to list an instance type and its images — and still refuse a new instance
because the Sales Organization has reached its customer/instance limit::

    {"errorCode": "PC-2031", "errorMessage": "Customer limit reached"}

That is a fact about the ACCOUNT, not about the offer, and it has exactly one
safe response: stop advertising NEW orders through that account, keep serving
and reconciling everything it already owns, and expire the state so a transient
account state cannot disable a credential forever.

Leaseweb publishes NO quota/limit endpoint (the official Public Cloud API
schema has no quota path at all — verified against ``leaseweb/api-definitions``
``publicCloud/paths``), so the signal cannot be read: it can only be LEARNED
from a definitive provider answer to a create, which is what this module
remembers.

Invariants:

- **Scoped to new orders only.** A limit reached on one account never changes
  how an existing server is managed, reconciled, powered or deleted: those
  flows resolve the account pinned on the resource, not the capacity state.
- **Never a cross-account retry.** Capacity never re-routes an ACCEPTED
  contract. It only informs which account a *future* catalog observation is
  published under.
- **Time bounded.** Every limit signal carries an expiry. Once expired the
  account is eligible again (the provider may have freed capacity); a repeated
  definitive signal refreshes the window instead of extending it silently.
- **Operator clearable.** ``clear``/``record_healthy`` exist so the operator can
  re-probe after removing provider instances, without editing the database.
- **No secrets, ever.** The row stores a provider error CODE, a correlation id
  (a routing identifier safe to quote to support), and the location/product
  that was refused. Never an API key, never a request body.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

__all__ = [
    "DEFAULT_LIMIT_TTL_SECONDS",
    "MIN_LIMIT_TTL_SECONDS",
    "AccountCapacity",
    "AccountCapacityRepository",
    "AccountCapacityState",
    "CapacityObservation",
    "validate_limit_ttl",
]

#: How long one definitive account-limit signal keeps an account out of NEW
#: order publication before it is probed again by ordinary catalog sync work.
#: An hour is deliberately short: the provider can free capacity at any time,
#: and re-publishing an offer is a cheap, reversible operation (unlike hiding a
#: whole account's inventory forever after one refusal).
DEFAULT_LIMIT_TTL_SECONDS = 3600

#: A zero/negative TTL would mean "never expire" by accident, which is exactly
#: the permanent-disable failure mode this module exists to avoid.
MIN_LIMIT_TTL_SECONDS = 60


class AccountCapacityState(StrEnum):
    """Whether an account may currently receive NEW billable orders."""

    HEALTHY = "healthy"
    """No definitive capacity refusal is on record (the default)."""

    LIMIT_REACHED = "limit_reached"
    """The provider definitively refused a new instance (e.g. PC-2031)."""


@dataclass(frozen=True, slots=True)
class CapacityObservation:
    """The safe evidence attached to one capacity signal."""

    error_code: str | None = None
    correlation_id: str | None = None
    location_id: str | None = None
    product_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("error_code", "correlation_id", "location_id", "product_id"):
            value = getattr(self, name)
            if value is not None and not str(value).strip():
                raise ValueError(f"{name} must be a non-empty string when supplied")


@dataclass(frozen=True, slots=True)
class AccountCapacity:
    """One credential account's NEW-ORDER eligibility, with its evidence."""

    provider_key: str
    credential_account_id: str
    state: AccountCapacityState = AccountCapacityState.HEALTHY
    error_code: str | None = None
    correlation_id: str | None = None
    location_id: str | None = None
    product_id: str | None = None
    observations: int = 1
    observed_at: datetime | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not str(self.provider_key or "").strip():
            raise ValueError("provider_key must not be empty")
        if not str(self.credential_account_id or "").strip():
            raise ValueError("credential_account_id must not be empty")
        if self.observations < 0:
            raise ValueError("observations must be >= 0")

    # -- time-aware state --------------------------------------------------

    def expired(self, *, now: datetime | None = None) -> bool:
        """Whether a limit window has elapsed (and may be probed again)."""
        if self.expires_at is None:
            return False
        reference = _aware(now)
        return reference >= _aware(self.expires_at)

    def is_limit_reached(self, *, now: datetime | None = None) -> bool:
        """Whether this account is currently out of capacity for new orders."""
        return self.state is AccountCapacityState.LIMIT_REACHED and not self.expired(now=now)

    def accepts_new_orders(self, *, now: datetime | None = None) -> bool:
        """The ONE question catalog publication and checkout ask."""
        return not self.is_limit_reached(now=now)

    # -- transitions -------------------------------------------------------

    def with_limit_reached(
        self,
        *,
        observation: CapacityObservation,
        ttl_seconds: int = DEFAULT_LIMIT_TTL_SECONDS,
        now: datetime | None = None,
    ) -> AccountCapacity:
        """Mark the account out of capacity, refreshing the evidence + window.

        ``observations`` accumulates so the operator can distinguish a single
        refusal from an account that keeps refusing.
        """
        ttl = validate_limit_ttl(ttl_seconds)
        observed = _aware(now)
        return replace(
            self,
            state=AccountCapacityState.LIMIT_REACHED,
            error_code=observation.error_code or self.error_code,
            correlation_id=observation.correlation_id or self.correlation_id,
            location_id=observation.location_id or self.location_id,
            product_id=observation.product_id or self.product_id,
            observations=self.observations + 1,
            observed_at=observed,
            expires_at=observed + timedelta(seconds=ttl),
        )

    def recovered(self, *, now: datetime | None = None) -> AccountCapacity:
        """A successful (or operator-cleared) account: eligible again.

        Evidence of the last refusal is kept for diagnostics — only the
        ELIGIBILITY changes — because "this account once refused a create" is
        useful context that must not silently disappear from the doctor.
        """
        return replace(
            self,
            state=AccountCapacityState.HEALTHY,
            expires_at=None,
            observed_at=_aware(now),
        )


class AccountCapacityRepository(Protocol):
    """Persistence port for durable account-capacity knowledge."""

    async def get(self, provider_key: str, credential_account_id: str) -> AccountCapacity | None:
        """One account's record, or None when nothing was ever observed."""
        ...

    async def list_for_provider(self, provider_key: str) -> tuple[AccountCapacity, ...]:
        """Every recorded account of one provider (deterministic order)."""
        ...

    async def limit_reached_accounts(
        self, provider_key: str, *, now: datetime | None = None
    ) -> frozenset[str]:
        """Accounts that must NOT receive new orders right now."""
        ...

    async def record_limit_reached(
        self,
        *,
        provider_key: str,
        credential_account_id: str,
        observation: CapacityObservation,
        ttl_seconds: int = DEFAULT_LIMIT_TTL_SECONDS,
        now: datetime | None = None,
    ) -> AccountCapacity:
        """Remember a definitive capacity refusal (idempotent per account)."""
        ...

    async def record_healthy(
        self,
        provider_key: str,
        credential_account_id: str,
        *,
        now: datetime | None = None,
    ) -> AccountCapacity:
        """Clear the limit state after proven success or an operator re-probe."""
        ...

    async def clear(self, provider_key: str, credential_account_id: str) -> bool:
        """Delete the record entirely; True when a row was removed."""
        ...


def _aware(value: datetime | None) -> datetime:
    """Treat a naive datetime as UTC; providers/DB may hand back either shape."""
    resolved = value or datetime.now(UTC)
    if resolved.tzinfo is None:
        return resolved.replace(tzinfo=UTC)
    return resolved


def validate_limit_ttl(ttl_seconds: int) -> int:
    """The one TTL rule, shared by the domain transition and its SQL adapter."""
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise ValueError("ttl_seconds must be an integer")
    if ttl_seconds < MIN_LIMIT_TTL_SECONDS:
        raise ValueError(f"ttl_seconds must be >= {MIN_LIMIT_TTL_SECONDS}")
    return ttl_seconds
