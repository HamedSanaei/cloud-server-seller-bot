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
``RECOVERY_CANDIDATE``
    A bounded recovery attempt is open: inventory evidence (a freed instance)
    or the scheduled backoff made the platform willing to spend exactly ONE
    real customer order as a canary. The canary's provider verdict — not time
    — is what returns the account to ``HEALTHY`` (accepted) or ``LIMIT_REACHED``
    with an exponential backoff (refused again).

Time passing is NOT evidence of recovery: Leaseweb does not free a Sales
Organization's customer limit because an hour went by. Only one of these
transitions may produce ``HEALTHY``:

* a real order the provider ACCEPTED while the account was a recovery
  candidate (the automated canary);
* an operator action after capacity was actually freed
  (``leaseweb cloud accounts capacity override-clear --account <id>``, the
  emergency override — normal recovery no longer needs it);
* a future verified read-only quota API whose semantics PROVE that new
  instance capacity is available; the audited finding is that Leaseweb's
  official Public Cloud API defines NO quota/limit endpoint (only regions,
  instance types, images, instances and contracts), so inventory plus the
  canary is what the platform relies on;
* another genuinely positive provider signal with the same meaning.

``list_regions`` / ``list_instanceTypes`` / ``list_images`` are NOT such a
signal: they prove authentication and catalog access, never create quota.
``list_instances`` IS a recovery SIGNAL (it is the inventory baseline: a freed
instance can unlock the canary), but it is never proof by itself. No billable
synthetic create is ever issued to probe capacity.

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

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

__all__ = [
    "DEFAULT_LIMIT_TTL_SECONDS",
    "DEFAULT_RECOVERY_BACKOFF_SECONDS",
    "MIN_LIMIT_TTL_SECONDS",
    "MIN_RECOVERY_DELAY_SECONDS",
    "AccountCapacity",
    "AccountCapacityRepository",
    "AccountCapacityState",
    "CapacityChangeRepublisher",
    "CapacityEvent",
    "CapacityEventKind",
    "CapacityObservation",
    "HistoricalCapacityEvidence",
    "HistoricalCapacityEvidenceSource",
    "recovery_backoff_seconds",
    "validate_limit_ttl",
    "validate_recovery_backoff",
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

#: Default RECOVERY ATTEMPT schedule (seconds): 15 min, 30 min, 1 h, 2 h and
#: then a permanent 6 h cadence. The sequence is the interval between two
#: real canary attempts; the LAST value is the cap, so an account that keeps
#: refusing is re-probed at most every 6 hours instead of hammering the
#: provider. Every value is operator-configurable.
DEFAULT_RECOVERY_BACKOFF_SECONDS: tuple[int, ...] = (900, 1800, 3600, 7200, 21600)

#: A recovery delay shorter than this would mean "retry immediately in a
#: loop", which is exactly the blind retry the capacity model forbids.
MIN_RECOVERY_DELAY_SECONDS = 60


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

    RECOVERY_CANDIDATE = "recovery_candidate"
    """The account is out of proven capacity, but a bounded recovery attempt
    is OPEN: inventory evidence (a freed instance / an operator change) or the
    scheduled backoff made the platform willing to spend exactly ONE real
    customer order as a CANARY.

    Inside this state exactly one order may reach the provider at a time —
    serialized by a durable PostgreSQL canary lease — and its outcome is what
    moves the account:

    * provider ACCEPTS the create -> ``HEALTHY`` (recovery proven, previous
      refusals keep their evidence, publication is refreshed);
    * provider REFUSES again (``PC-2031``) -> ``LIMIT_REACHED`` with an
      exponential backoff before the next window;

    No synthetic/billable probe is ever created: the canary IS a real
    customer order, and its provider rejection is answered with the standard
    capacity message and never a charge."""


class CapacityEventKind(StrEnum):
    """Why one evidence row exists (the append-only capacity history)."""

    REFUSAL = "refusal"
    """A refusal observed live by the platform (a provider create POST)."""

    BACKFILL = "backfill"
    """A refusal recovered from a historical provider operation failure."""

    CLEARED = "cleared"
    """An operator clear / positive proof moved the account back to HEALTHY."""

    RECOVERY_ATTEMPT = "recovery_attempt"
    """One canary attempt acquired its durable lease (a real order will test
    the account). Recorded so the number of attempts has its own timeline."""

    RECOVERED = "recovered"
    """A canary order was ACCEPTED by the provider: capacity is proven."""


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
    #: Inventory baseline (instances the account held when the refusal was
    #: learned). A LOWER count later is local evidence that capacity may have
    #: been freed, which brings the next recovery window forward.
    baseline_instance_count: int | None = None
    baseline_instance_ids_hash: str | None = None
    baseline_observed_at: datetime | None = None
    #: Recovery attempts actually exercised (canary orders that reached the
    #: provider) and their schedule. Reset by a proven recovery.
    recovery_attempts: int = 0
    last_recovery_attempt_at: datetime | None = None
    next_recovery_attempt_at: datetime | None = None
    #: Durable single-canary serialization: at most one in-flight attempt.
    canary_lease_expires_at: datetime | None = None
    canary_lease_ref: str | None = None
    #: Operator-notification bookkeeping (the outbox key is the dedupe, these
    #: columns keep the decision itself idempotent across processes).
    outage_notified_at: datetime | None = None
    last_reminder_at: datetime | None = None

    def __post_init__(self) -> None:
        if not str(self.provider_key or "").strip():
            raise ValueError("provider_key must not be empty")
        if not str(self.credential_account_id or "").strip():
            raise ValueError("credential_account_id must not be empty")
        if self.observations < 0:
            raise ValueError("observations must be >= 0")
        if self.recovery_attempts < 0:
            raise ValueError("recovery_attempts must be >= 0")
        if self.baseline_instance_count is not None and self.baseline_instance_count < 0:
            raise ValueError("baseline_instance_count must be >= 0")

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

    # -- automatic recovery -------------------------------------------------

    def canary_lease_held(self, *, now: datetime | None = None) -> bool:
        """Whether another canary attempt is currently in flight."""
        if self.canary_lease_expires_at is None:
            return False
        return _aware(now) < _aware(self.canary_lease_expires_at)

    def recovery_window_due(self, *, now: datetime | None = None) -> bool:
        """Whether a SCHEDULED attempt window may be opened right now.

        A blocked account with no schedule is never "due": the scheduler owns
        the delay, and time alone never makes an account eligible.
        """
        if not self.is_limit_reached(now=now):
            return False
        if self.next_recovery_attempt_at is None:
            return False
        return _aware(now) >= _aware(self.next_recovery_attempt_at)

    def accepts_canary(self, *, now: datetime | None = None) -> bool:
        """Whether one real order may be spent proving this account again."""
        if self.state is not AccountCapacityState.RECOVERY_CANDIDATE:
            return False
        return not self.canary_lease_held(now=now)

    def reminder_due(
        self,
        *,
        now: datetime | None = None,
        delay_seconds: int = 1800,
        interval_seconds: int = 21600,
    ) -> datetime | None:
        """The reminder instant when one is due, otherwise None.

        First reminder ``delay_seconds`` after the refusal was observed, then
        ``interval_seconds`` apart. ``outage_notified_at`` must exist: the
        reminder follows the outage card, never replaces it.
        """
        if self.outage_notified_at is None or self.observed_at is None:
            return None
        base = (
            _aware(self.last_reminder_at) + timedelta(seconds=interval_seconds)
            if self.last_reminder_at is not None
            else _aware(self.observed_at) + timedelta(seconds=delay_seconds)
        )
        reference = _aware(now)
        return base if reference >= base else None

    def with_baseline(
        self,
        *,
        instance_count: int,
        ids_hash: str,
        now: datetime | None = None,
    ) -> AccountCapacity:
        """Remember the instance census that this refusal is measured against."""
        if isinstance(instance_count, bool) or not isinstance(instance_count, int):
            raise ValueError("instance_count must be an integer")
        if instance_count < 0:
            raise ValueError("instance_count must be >= 0")
        if not str(ids_hash or "").strip():
            raise ValueError("ids_hash must not be empty")
        return replace(
            self,
            baseline_instance_count=instance_count,
            baseline_instance_ids_hash=str(ids_hash),
            baseline_observed_at=_aware(now),
        )

    def with_recovery_scheduled(
        self, *, delay_seconds: int, now: datetime | None = None
    ) -> AccountCapacity:
        """Schedule the next attempt window, never sooner than the floor."""
        if isinstance(delay_seconds, bool) or not isinstance(delay_seconds, int):
            raise ValueError("delay_seconds must be an integer")
        if delay_seconds < MIN_RECOVERY_DELAY_SECONDS:
            raise ValueError(f"delay_seconds must be >= {MIN_RECOVERY_DELAY_SECONDS}")
        return replace(
            self, next_recovery_attempt_at=_aware(now) + timedelta(seconds=delay_seconds)
        )

    def with_recovery_window_open(self, *, now: datetime | None = None) -> AccountCapacity:
        """Open the canary window: one real order may now prove recovery."""
        return replace(
            self,
            state=AccountCapacityState.RECOVERY_CANDIDATE,
            next_recovery_attempt_at=None,
        )

    def with_canary_attempt(
        self, *, ref: str, lease_seconds: int, now: datetime | None = None
    ) -> AccountCapacity:
        """Attach a durable lease to one in-flight canary attempt."""
        if not str(ref or "").strip():
            raise ValueError("ref must not be empty")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int):
            raise ValueError("lease_seconds must be an integer")
        if lease_seconds < MIN_RECOVERY_DELAY_SECONDS:
            raise ValueError(f"lease_seconds must be >= {MIN_RECOVERY_DELAY_SECONDS}")
        return replace(
            self,
            canary_lease_ref=str(ref),
            canary_lease_expires_at=_aware(now) + timedelta(seconds=lease_seconds),
            last_recovery_attempt_at=_aware(now),
        )

    def with_canary_lease_released(self) -> AccountCapacity:
        """Release the lease without claiming any capacity outcome."""
        return replace(self, canary_lease_ref=None, canary_lease_expires_at=None)

    def with_canary_attempt_refused(
        self,
        *,
        observation: CapacityObservation,
        delay_seconds: int,
        now: datetime | None = None,
    ) -> AccountCapacity:
        """A canary was refused again: back off before the next window.

        The account returns to ``LIMIT_REACHED`` (no window, no publication)
        and the next attempt is scheduled with the exponential backoff. The
        canonical ``limit `` evidence is refreshed from this refusal.
        """
        if isinstance(delay_seconds, bool) or not isinstance(delay_seconds, int):
            raise ValueError("delay_seconds must be an integer")
        if delay_seconds < MIN_RECOVERY_DELAY_SECONDS:
            raise ValueError(f"delay_seconds must be >= {MIN_RECOVERY_DELAY_SECONDS}")
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
            expires_at=observed + timedelta(seconds=self._ttl_seconds_for_evidence()),
            recovery_attempts=self.recovery_attempts + 1,
            last_recovery_attempt_at=observed,
            next_recovery_attempt_at=observed + timedelta(seconds=delay_seconds),
            canary_lease_ref=None,
            canary_lease_expires_at=None,
        )

    def with_recovery_proven(self, *, now: datetime | None = None) -> AccountCapacity:
        """A real order was ACCEPTED through this account: capacity is proven.

        Eligibility and the recovery schedule reset; the refusal evidence is
        deliberately kept (only the state changes), exactly like an operator
        clear.
        """
        observed = _aware(now)
        return replace(
            self,
            state=AccountCapacityState.HEALTHY,
            expires_at=None,
            observed_at=observed,
            recovery_attempts=0,
            next_recovery_attempt_at=None,
            canary_lease_ref=None,
            canary_lease_expires_at=None,
            outage_notified_at=None,
            last_reminder_at=None,
        )

    def _ttl_seconds_for_evidence(self) -> int:
        """The remaining freshness window, used only to refresh ``expires_at``.

        A refusal observed live already carries its window; a transition that
        refreshes evidence without an explicit TTL keeps whatever window was
        configured for the row (never a new magic number).
        """
        if self.observed_at is None or self.expires_at is None:
            return DEFAULT_LIMIT_TTL_SECONDS
        span = int((_aware(self.expires_at) - _aware(self.observed_at)).total_seconds())
        return span if span >= MIN_LIMIT_TTL_SECONDS else DEFAULT_LIMIT_TTL_SECONDS

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

    async def record_inventory_census(
        self,
        provider_key: str,
        credential_account_id: str,
        *,
        instance_count: int,
        ids_hash: str,
        now: datetime | None = None,
    ) -> AccountCapacity | None:
        """Read-only inventory evidence for ONE account (never a provider call).

        Captures the baseline when the refusal has none yet, and BRINGS THE
        NEXT RECOVERY WINDOW FORWARD to ``now`` when the count dropped below
        the baseline (an instance was freed). Returns the settled record, or
        ``None`` when the account has no capacity row to reconcile.
        """
        ...

    async def bring_forward_recovery(
        self,
        provider_key: str,
        credential_account_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Local evidence (an app-owned instance was deleted) opens the next
        recovery window as soon as possible; True when a schedule changed."""
        ...

    async def schedule_recovery(
        self,
        provider_key: str,
        credential_account_id: str,
        *,
        delay_seconds: int,
        now: datetime | None = None,
    ) -> AccountCapacity | None:
        """Schedule the next attempt window for a blocked account (no-op when
        one is already scheduled, in flight, or the account is eligible)."""
        ...

    async def open_recovery_window(
        self, provider_key: str, credential_account_id: str, *, now: datetime | None = None
    ) -> AccountCapacity | None:
        """Move a blocked account whose schedule is due to ``RECOVERY_CANDIDATE``
        (one real order may now attempt it). Returns ``None`` when the window
        may not open."""
        ...

    async def begin_canary_attempt(
        self,
        provider_key: str,
        credential_account_id: str,
        *,
        ref: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> AccountCapacity | None:
        """Atomically acquire the SINGLE canary lease for one attempt.

        ``None`` means another attempt holds the lease, the account is not a
        recovery candidate, or the reference is empty — never "try anyway".
        """
        ...

    async def release_canary_lease(
        self,
        provider_key: str,
        credential_account_id: str,
        *,
        ref: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Release the lease (matching ``ref`` when supplied); True when it was."""
        ...

    async def record_canary_refusal(
        self,
        provider_key: str,
        credential_account_id: str,
        *,
        ref: str,
        observation: CapacityObservation,
        delay_seconds: int,
        ttl_seconds: int = DEFAULT_LIMIT_TTL_SECONDS,
        now: datetime | None = None,
    ) -> AccountCapacity | None:
        """Record a canary refused by the provider: backoff + ``LIMIT_REACHED``.

        Idempotent per counter reference: replaying the same attempt never
        increments the attempt count twice.
        """
        ...

    async def record_recovery_proven(
        self,
        provider_key: str,
        credential_account_id: str,
        *,
        ref: str,
        now: datetime | None = None,
    ) -> AccountCapacity | None:
        """Record a PROVEN recovery (the canary order was accepted)."""
        ...

    async def mark_outage_notified(
        self,
        provider_key: str,
        credential_account_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Remember that the outage card was enqueued for this refusal."""
        ...

    async def mark_reminder_sent(
        self,
        provider_key: str,
        credential_account_id: str,
        *,
        sent_at: datetime | None = None,
    ) -> bool:
        """Remember the reminder instant (the outbox key dedupes the rest)."""
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

    async def after_capacity_recovery(
        self, *, provider_key: str, credential_account_id: str
    ) -> object | None:
        """Re-prove and republish the pairs of a RECOVERED account.

        Called the moment a real canary order proves capacity returned, with
        the same read-only pair proof the periodic sync uses. It never touches
        an accepted contract and never creates anything.
        """
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


def validate_recovery_backoff(schedule: object) -> tuple[int, ...]:
    """The one recovery-schedule rule (shared by config, controller and SQL)."""
    if not isinstance(schedule, (list, tuple)) or not schedule:
        raise ValueError("recovery backoff schedule must be a non-empty sequence")
    values: list[int] = []
    for value in schedule:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("every recovery backoff value must be an integer")
        if value < MIN_RECOVERY_DELAY_SECONDS:
            raise ValueError(
                f"every recovery backoff value must be >= {MIN_RECOVERY_DELAY_SECONDS}"
            )
        values.append(value)
    return tuple(values)


def recovery_backoff_seconds(
    attempts: int, schedule: Sequence[int] = DEFAULT_RECOVERY_BACKOFF_SECONDS
) -> int:
    """The delay before attempt number ``attempts`` (0-based), capped at the last."""
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        raise ValueError("attempts must be a non-negative integer")
    offsets = validate_recovery_backoff(schedule)
    return offsets[attempts] if attempts < len(offsets) else offsets[-1]
