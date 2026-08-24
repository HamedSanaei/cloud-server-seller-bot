"""Volume cost representation (M13-009).

Volumes are block storage billed by size-time - the same shape as
snapshots. The per-GB-month rate is an EXPLICIT OPERATOR INPUT
(:class:`VolumeRateCard`); provider prices are never hardcoded. Exact
Decimal/720 ROUND_HALF_UP arithmetic only.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

#: Hours per month used by the catalog's monthly->hourly derivation.
HOURS_PER_MONTH = Decimal(720)

_CURRENCY_LENGTH = 3


class VolumePricingError(ValueError):
    """Invalid volume rate card or size."""


@dataclass(frozen=True, slots=True)
class VolumeRateCard:
    """The operator-declared price of volume storage (per GB per month).

    ``per_gb_month_minor`` is in MINOR currency units per GB per month,
    charged for every allocated GB whether the volume is attached or not.
    Rate 0 is a valid operator decision ("volumes are free here").
    """

    currency: str
    per_gb_month_minor: int

    def __post_init__(self) -> None:
        if not self.currency or len(self.currency) != _CURRENCY_LENGTH:
            raise VolumePricingError("currency must be a 3-letter ISO 4217 code")
        if self.per_gb_month_minor < 0:
            raise VolumePricingError("per_gb_month_minor must be non-negative")


def volume_monthly_minor(card: VolumeRateCard, size_gb: int) -> int:
    """Monthly storage cost for one volume of ``size_gb``, minor units."""
    if size_gb < 0:
        raise VolumePricingError("size_gb must be non-negative")
    return card.per_gb_month_minor * int(size_gb)


def volume_hourly_quantum_minor(
    card: VolumeRateCard,
    size_gb: int,
    quantum_seconds: int = 3600,
) -> int:
    """Per-quantum accrual for one volume, mirroring the catalog's
    monthly->hourly convention (Decimal division by 720, ROUND_HALF_UP to
    whole minor units, then prorated across the quantum)."""
    if quantum_seconds <= 0:
        raise VolumePricingError("quantum_seconds must be positive")
    monthly = Decimal(volume_monthly_minor(card, size_gb))
    hourly = (monthly / HOURS_PER_MONTH).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    per_quantum = hourly * Decimal(quantum_seconds) / Decimal(3600)
    return int(per_quantum.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
