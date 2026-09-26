"""Storefront capacity status: the one read-only view of Cloud sellability.

The operator has three places asking the same question — ``offers readiness``
(release/readiness gate), ``leaseweb cloud accounts doctor`` and
``business-log doctor`` — and the answer must be computed ONCE, from durable
facts, never by re-deriving it per command:

* how many Cloud offers are actually sellable right now
  (``cloud_sellable_offers``);
* how many credential accounts are blocked, i.e. must not receive new orders
  (``capacity_blocked_accounts``) and how many carry an ELAPSED refusal whose
  recovery is unproven (``capacity_unknown_accounts``);
* how many are inside an open canary window
  (``recovery_candidate_accounts``);
* whether the Cloud storefront is available at all
  (``cloud_storefront_available``) and for how long it has been unavailable
  (``cloud_storefront_outage_seconds``).

Nothing here mutates state or calls a provider: it is derived purely from the
durable capacity rows plus a sellable-offer count the caller provides.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from cloud_platform.modules.provider_capacity.domain import (
    AccountCapacity,
    AccountCapacityState,
)

__all__ = [
    "METRIC_CAPACITY_BLOCKED_ACCOUNTS",
    "METRIC_CAPACITY_UNKNOWN_ACCOUNTS",
    "METRIC_CLOUD_SELLABLE_OFFERS",
    "METRIC_CLOUD_STOREFRONT_AVAILABLE",
    "METRIC_CLOUD_STOREFRONT_OUTAGE_SECONDS",
    "METRIC_RECOVERY_CANDIDATE_ACCOUNTS",
    "StorefrontCapacityStatus",
    "capacity_status",
]

#: The exact metric names the doctor/readiness surfaces expose.
METRIC_CLOUD_SELLABLE_OFFERS = "cloud_sellable_offers"
METRIC_CAPACITY_BLOCKED_ACCOUNTS = "capacity_blocked_accounts"
METRIC_CAPACITY_UNKNOWN_ACCOUNTS = "capacity_unknown_accounts"
METRIC_RECOVERY_CANDIDATE_ACCOUNTS = "recovery_candidate_accounts"
METRIC_CLOUD_STOREFRONT_AVAILABLE = "cloud_storefront_available"
METRIC_CLOUD_STOREFRONT_OUTAGE_SECONDS = "cloud_storefront_outage_seconds"


@dataclass(frozen=True, slots=True)
class StorefrontCapacityStatus:
    """Read-only storefront availability, derived from durable capacity rows."""

    provider_key: str
    sellable_offers: int
    blocked_accounts: tuple[str, ...] = ()
    unknown_accounts: tuple[str, ...] = ()
    recovery_candidates: tuple[str, ...] = ()
    outage_since: datetime | None = None

    def __post_init__(self) -> None:
        if self.sellable_offers < 0:
            raise ValueError("sellable_offers must be >= 0")

    @property
    def storefront_available(self) -> bool:
        """Whether a customer can actually buy a Cloud product right now."""
        return self.sellable_offers > 0

    @property
    def storefront_unavailable(self) -> bool:
        return not self.storefront_available

    def outage_seconds(self, *, now: datetime | None = None) -> int:
        """How long the storefront has been unavailable, or 0 when it is open.

        The clock starts at the EARLIEST still-blocked refusal, because that is
        the moment the last sellable offer disappeared (a blocked account that
        never had offers cannot close the storefront by itself).
        """
        if self.storefront_available or self.outage_since is None:
            return 0
        reference = now or datetime.now(UTC)
        since = self.outage_since
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        seconds = int((reference - since).total_seconds())
        return seconds if seconds > 0 else 0

    def metrics(self, *, now: datetime | None = None) -> dict[str, object]:
        """The exact operator-facing metric map (stable key names)."""
        return {
            METRIC_CLOUD_SELLABLE_OFFERS: self.sellable_offers,
            METRIC_CAPACITY_BLOCKED_ACCOUNTS: len(self.blocked_accounts),
            METRIC_CAPACITY_UNKNOWN_ACCOUNTS: len(self.unknown_accounts),
            METRIC_RECOVERY_CANDIDATE_ACCOUNTS: len(self.recovery_candidates),
            METRIC_CLOUD_STOREFRONT_AVAILABLE: self.storefront_available,
            METRIC_CLOUD_STOREFRONT_OUTAGE_SECONDS: self.outage_seconds(now=now),
        }

    def summary(self) -> str:
        return (
            f"provider={self.provider_key} sellable={self.sellable_offers} "
            f"blocked={len(self.blocked_accounts)} unknown={len(self.unknown_accounts)} "
            f"candidates={len(self.recovery_candidates)} "
            f"available={'yes' if self.storefront_available else 'no'}"
        )


def capacity_status(
    provider_key: str,
    records: tuple[AccountCapacity, ...],
    *,
    sellable_offers: int,
    now: datetime | None = None,
) -> StorefrontCapacityStatus:
    """Fold durable capacity rows + a sellable count into one status."""
    reference = now or datetime.now(UTC)
    blocked: list[str] = []
    unknown: list[str] = []
    candidates: list[str] = []
    outage_since: datetime | None = None
    for record in records:
        settled = record.settled(now=reference)
        if settled.state is AccountCapacityState.RECOVERY_CANDIDATE:
            candidates.append(settled.credential_account_id)
            continue
        if settled.state is AccountCapacityState.UNKNOWN_AFTER_LIMIT:
            unknown.append(settled.credential_account_id)
        if not settled.is_limit_reached(now=reference):
            continue
        blocked.append(settled.credential_account_id)
        observed = settled.observed_at
        if observed is not None and (outage_since is None or observed < outage_since):
            outage_since = observed
    return StorefrontCapacityStatus(
        provider_key=provider_key,
        sellable_offers=int(sellable_offers),
        blocked_accounts=tuple(sorted(blocked)),
        unknown_accounts=tuple(sorted(unknown)),
        recovery_candidates=tuple(sorted(candidates)),
        outage_since=outage_since,
    )
