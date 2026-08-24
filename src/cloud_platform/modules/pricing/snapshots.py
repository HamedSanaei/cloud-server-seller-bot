"""Snapshot cost representation (M13-004).

Acceptance: snapshot cost/capability represented.

Snapshots are storage billed by size-time. The per-GB rate is an EXPLICIT
OPERATOR INPUT (:class:`SnapshotRateCard`, sourced from settings / the
price book at composition time) - provider prices are never hardcoded
(architecture invariant). The capability side is the optional
``snapshot_support_of`` probe on the provider port; the cost side is pure
integer/Decimal arithmetic here.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

#: Hours per month used by the catalog's monthly->hourly derivation; one
#: shared constant so every cost surface converts identically.
HOURS_PER_MONTH = Decimal(720)

_CURRENCY_LENGTH = 3


class SnapshotPricingError(ValueError):
    """Invalid snapshot rate card or size."""


@dataclass(frozen=True, slots=True)
class SnapshotRateCard:
    """The operator-declared price of snapshot storage.

    ``per_gb_month_minor`` is in MINOR currency units (e.g. cents) per GB
    per month. A card with rate 0 is valid and means "snapshots are free"
    for this environment - a deliberate operator decision, not a default
    guess about any provider.
    """

    currency: str
    per_gb_month_minor: int

    def __post_init__(self) -> None:
        if not self.currency or len(self.currency) != _CURRENCY_LENGTH:
            raise SnapshotPricingError("currency must be a 3-letter ISO 4217 code")
        if self.per_gb_month_minor < 0:
            raise SnapshotPricingError("per_gb_month_minor must be non-negative")


def snapshot_monthly_minor(card: SnapshotRateCard, disk_gb: int) -> int:
    """Monthly storage cost for one snapshot of ``disk_gb``, minor units."""
    if disk_gb < 0:
        raise SnapshotPricingError("disk_gb must be non-negative")
    return card.per_gb_month_minor * int(disk_gb)


def snapshot_hourly_quantum_minor(
    card: SnapshotRateCard,
    disk_gb: int,
    quantum_seconds: int = 3600,
) -> int:
    """Per-quantum accrual for one snapshot, mirroring the catalog's
    monthly->hourly convention (Decimal division by 720, ROUND_HALF_UP to
    whole minor units, then prorated across the quantum).

    The result is deterministic integer arithmetic - never float.
    """
    if quantum_seconds <= 0:
        raise SnapshotPricingError("quantum_seconds must be positive")
    monthly = Decimal(snapshot_monthly_minor(card, disk_gb))
    hourly = (monthly / HOURS_PER_MONTH).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    per_quantum = hourly * Decimal(quantum_seconds) / Decimal(3600)
    return int(per_quantum.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
