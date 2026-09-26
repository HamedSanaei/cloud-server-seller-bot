"""Durable provider ACCOUNT CAPACITY knowledge (LEASEWEB-MULTIACCOUNT).

A credential account can be perfectly authenticated, able to sell a location,
able to list an instance type and its images — and still refuse a new instance
because the Sales Organization has reached its customer/instance limit::

    {"errorCode": "PC-2031", "errorMessage": "Customer limit reached"}

That is a fact about the ACCOUNT, not about the offer, and it has exactly one
safe response: stop advertising NEW orders through that account, keep serving
and reconciling everything it already owns, and require POSITIVE evidence
before it is handed new business again.

Leaseweb publishes NO quota/limit endpoint (the official Public Cloud API
schema has no quota path at all — verified against ``leaseweb/api-definitions``
``publicCloud/paths``), so the signal cannot be read: it can only be LEARNED
from a definitive provider answer to a create, which is what this module
remembers.

Evidence semantics (the correctness rule of this module)
-------------------------------------------------------

A refusal is a fact about the past; eligibility for the future is a separate
question. The state model therefore never answers "may this account take a new
order?" with "the refusal is old":

``HEALTHY``
    No refusal on record, or an operator/positive-proof cleared it.
``LIMIT_REACHED``
    A definitive refusal was observed and is still inside its cooling window.
``UNKNOWN_AFTER_LIMIT``
    The cooling window elapsed without any proof that provider capacity
    recovered. The refusal may no longer be FRESH, but nothing has been proven
    either, so the account is still not eligible for NEW orders.

Time passing is NOT evidence of recovery: Leaseweb does not free a Sales
Organization's customer limit because an hour went by. Only one of these
transitions may produce ``HEALTHY``:

* an operator action after capacity was actually freed
  (``leaseweb cloud accounts clear --account <id>``);
* a future verified read-only quota API whose semantics PROVE that new
  instance capacity is available;
* another genuinely positive provider signal with the same meaning.

``list_regions`` / ``list_instanceTypes`` / ``list_images`` / ``list_instances``
are NOT such a signal: they prove authentication and catalog access, never
create quota. No billable create is ever issued to probe capacity.

Invariants:

- **Scoped to new orders only.** A limit reached on one account never changes
  how an existing server is managed, reconciled, powered or deleted: those
  flows resolve the account pinned on the resource, not the capacity state.
- **Never a cross-account retry.** Capacity never re-routes an ACCEPTED
  contract. It only informs which account a *future* catalog observation is
  published under.
- **Evidence is durable and idempotent.** Every refusal is appended to an
  evidence log keyed by its source (a live observation or the provider
  operation that produced it), so a historical failure can be reconciled
  exactly once and an account's history survives restarts.
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
    "CapacityChangeRepublisher",
    "CapacityEvent",
    "CapacityEventKind",
    "CapacityObservation",
    "HistoricalCapacityEvidence",
    "HistoricalCapacityEvidenceSource",
    "validate_limit_ttl",
]

#: How long one definitive account-limit signal is treated as FRESH evidence.
#: After it elapses the account moves to ``UNKNOWN_AFTER_LIMIT`` — never back
#: to ``HEALTHY`` — until an operator or a verified positive provider signal
#: proves capacity was restored. The window is short on purpose: it bounds how
#: long the storefront keeps treating the refusal as current, and it is the
#: operator's cue to re-probe after freeing provider instances.
DEFAULT_LIMIT_TTL_SECONDS = 3600

#: A zero/negative TTL would mean "never expire" by accident, which is a
#: permanent-disable failure mode. Floor it.
MIN_LIMIT_TTL_SECONDS = 60


class AccountCapacityState(StrEnum):
    """Whether an account may currently receive NEW billable orders."""

    HEALTHY = "healthy"
    """No definitive capacity refusal is on record (the default)."""

    LIMIT_REACHED = "limit_reached"
    """The provider definitively refused a new instance (e.g. PC-2031), and
    the refusal is still inside its cooling window."""

    UNKNOWN_AFTER_LIMIT = "unknown_after_limit"
    """The refusal's cooling window elapsed with NO proof of recovery.

    Not ``HEALTHY``: nobody has established that the provider limit lifted.
    The account stays out of NEW-order publication until an operator (or a
    verified positive provider signal) clears it."""


class CapacityEventKind(StrEnum):
    """Why one evidence row exists (the append-only capacity history)."""

    REFUSAL = "refusal"
    """A refusal observed live by the platform (a provider create POST)."""

    BACKFILL = "backfill"
    """A refusal recovered from a historical provider operation failure."""

    CLEARED = "cleared"
    """An operator clear / positive proof moved the account back to HEALTHY."""


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
class HistoricalCapacityEvidence:
    """A refusal proven by a PAST provider operation (never a live call).

    ``source_ref`` is the operation key that carries the evidence and is the
    idempotency anchor of the reconciliation: replaying the same failure can
    never append a second record.
    """

    provider_key: str
    credential_account_id: str
    source_ref: str
    error_code: str | None = None
    correlation_id: str | None = None
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not str(self.provider_key or "").strip():
            raise ValueError("provider_key must not be empty")
        if not str(self.credential_account_id or "").strip():
            raise ValueError("credential_account_id must not be empty")
        if not str(self.source_ref or "").strip():
            raise ValueError("source_ref must not be empty")

    def as_observation(self) -> CapacityObservation:
        return CapacityObservation(
            error_code=self.error_code,
            correlation_id=self.correlation_id,
        )


@dataclass(frozen=True, slots=True)
class CapacityEvent:
    """One row of the append-only capacity history (safe facts only)."""

    provider_key: str
    credential_account_id: str
    kind: CapacityEventKind
    state: AccountCapacityState
    error_code: str | None = None
    correlation_id: str | None = None
    location_id: str | None = None
    product_id: str | None = None
    source_ref: str | None = None
    observed_at: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime | None = None


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
        """Whether the cooling window elapsed (evidence is no longer fresh)."""
        if self.expires_at is None:
            return False
        reference = _aware(now)
        return reference >= _aware(self.expires_at)

    def cooling_expired(self, *, now: datetime | None = None) -> bool:
        """Whether a refusal's freshness window has elapsed (``LIMIT_REACHED``
        only — a settled ``UNKNOWN_AFTER_LIMIT`` row is not "cooling")."""
        return self.state is AccountCapacityState.LIMIT_REACHED and self.expired(now=now)

    def settled(self, *, now: datetime | None = None) -> AccountCapacity:
        """The record as it must be READ today.

        An expired ``LIMIT_REACHED`` refusal becomes ``UNKNOWN_AFTER_LIMIT``:
        the window is over, but nothing proved recovery. Applied on every read
        so no caller can observe "elapsed means eligible".
        """
        if self.state is AccountCapacityState.LIMIT_REACHED and self.expired(now=now):
            return replace(self, state=AccountCapacityState.UNKNOWN_AFTER_LIMIT)
        return self

    def is_limit_reached(self, *, now: datetime | None = None) -> bool:
        """Whether this account must be kept out of NEW orders.

        Deliberately NOT time-decorated: an elapsed cooling window removes the
        freshness of the refusal, never the fact that recovery is unproven.
        """
        return self.settled(now=now).state in (
            AccountCapacityState.LIMIT_REACHED,
            AccountCapacityState.UNKNOWN_AFTER_LIMIT,
        )

    def accepts_new_orders(self, *, now: datetime | None = None) -> bool:
        """The ONE question catalog publication and checkout ask."""
        return not self.is_limit_reached(now=now)

    def blocked_reason(self, *, now: datetime | None = None) -> str | None:
        """A safe, operator-facing reason, or None when eligible."""
        settled = self.settled(now=now)
        if settled.state is AccountCapacityState.LIMIT_REACHED:
            return "limit-reached"
        if settled.state is AccountCapacityState.UNKNOWN_AFTER_LIMIT:
            return "unknown-after-limit"
        return None

    # -- transitions -------------------------------------------------------

    def with_limit_reached(
        self,
        *,
        observation: CapacityObservation,
        ttl_seconds: int = DEFAULT_LIMIT_TTL_SECONDS,
        now: datetime | None = None,
        observed_at: datetime | None = None,
    ) -> AccountCapacity:
        """Mark the account out of capacity, refreshing the evidence + window.

        ``observations`` accumulates so the operator can distinguish a single
        refusal from an account that keeps refusing. A historical observation
        keeps its own timestamp (and therefore its own, already-elapsed
        window) instead of pretending it is fresh.
        """
        ttl = validate_limit_ttl(ttl_seconds)
        observed = _aware(observed_at or now)
        current = self.settled()
        # Never let an OLDER observation move the window backwards.
        if (
            observed_at is not None
            and current.observed_at is not None
            and _aware(current.observed_at) > observed
            and current.state is not AccountCapacityState.HEALTHY
        ):
            return replace(current, observations=current.observations + 1)
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
        """Accounts that must NOT receive new orders right now.

        Includes settled ``UNKNOWN_AFTER_LIMIT`` accounts: an elapsed window is
        not evidence of recovery.
        """
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

    async def record_historical_evidence(
        self,
        evidence: HistoricalCapacityEvidence,
        *,
        ttl_seconds: int = DEFAULT_LIMIT_TTL_SECONDS,
        now: datetime | None = None,
    ) -> AccountCapacity | None:
        """Remember a refusal PROVEN by a past operation, exactly once.

        Returns the resulting record, or ``None`` when this exact evidence was
        already reconciled (the idempotency contract).
        """
        ...

    async def record_healthy(
        self,
        provider_key: str,
        credential_account_id: str,
        *,
        now: datetime | None = None,
        reason: str | None = None,
    ) -> AccountCapacity:
        """Clear the limit state after proven success or an operator re-probe."""
        ...

    async def settle_expired(
        self, provider_key: str, *, now: datetime | None = None
    ) -> tuple[str, ...]:
        """Persist the ``LIMIT_REACHED`` -> ``UNKNOWN_AFTER_LIMIT`` transition.

        Optional housekeeping: reads already settle, this only makes the stored
        row match what the platform believes.
        """
        ...

    async def list_events(
        self,
        provider_key: str,
        *,
        credential_account_id: str | None = None,
        limit: int = 20,
    ) -> tuple[CapacityEvent, ...]:
        """The append-only evidence history (newest first)."""
        ...

    async def clear(self, provider_key: str, credential_account_id: str) -> bool:
        """Delete the record entirely; True when a row was removed."""
        ...


class HistoricalCapacityEvidenceSource(Protocol):
    """Reads refusals out of PAST provider failures (read-only, no calls out)."""

    async def failed_capacity_evidence(
        self, provider_key: str, *, limit: int = 200
    ) -> tuple[HistoricalCapacityEvidence, ...]:
        """Failed create operations whose sanitized error proves PC-2031."""
        ...


class CapacityChangeRepublisher(Protocol):
    """Reacts to a NEW definitive capacity refusal (best effort, never fatal).

    The reaction is a NEW-ORDER publication refresh for the affected account:
    re-route pairs another account PROVED read-only, otherwise unpublish them.
    It must never touch an accepted contract, never re-pin an existing server
    and never re-send the refused provider POST.
    """

    async def after_capacity_refusal(
        self, *, provider_key: str, credential_account_id: str
    ) -> object | None:
        """Refresh future-order publication for one limited account."""
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
