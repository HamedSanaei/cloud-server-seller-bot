"""Floating IP cost representation (M13-008).

Acceptance: independent resource billing supported.

A floating (primary) IP is a RESOURCE IN ITS OWN RIGHT: it accrues cost
every hour while it EXISTS - bound to a server or not, exactly one rate
card unit per IP. The per-IP monthly rate is an EXPLICIT OPERATOR INPUT
(:class:`FloatingIpRateCard`, from settings / the price book at
composition time) - provider prices are never hardcoded. The math is the
same exact Decimal/720 ROUND_HALF_UP convention as every other cost
surface; floats never touch money.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

#: Hours per month used by the catalog's monthly->hourly derivation.
HOURS_PER_MONTH = Decimal(720)

_CURRENCY_LENGTH = 3


class FloatingIpPricingError(ValueError):
    """Invalid floating-IP rate card."""


@dataclass(frozen=True, slots=True)
class FloatingIpRateCard:
    """The operator-declared price of one floating IP.

    ``per_ip_month_minor`` is minor currency units per IP per month,
    charged for EVERY allocated IP regardless of assignment state (the
    provider bills reserved addresses too). Rate 0 is valid and means
    "floating IPs are free here" - a deliberate decision, not a guess.
    """

    currency: str
    per_ip_month_minor: int

    def __post_init__(self) -> None:
        if not self.currency or len(self.currency) != _CURRENCY_LENGTH:
            raise FloatingIpPricingError("currency must be a 3-letter ISO 4217 code")
        if self.per_ip_month_minor < 0:
            raise FloatingIpPricingError("per_ip_month_minor must be non-negative")


def floating_monthly_minor(card: FloatingIpRateCard, ip_count: int = 1) -> int:
    """Monthly cost for ``ip_count`` floating IPs, minor units."""
    if ip_count < 0:
        raise FloatingIpPricingError("ip_count must be non-negative")
    return card.per_ip_month_minor * ip_count


def floating_hourly_quantum_minor(
    card: FloatingIpRateCard,
    ip_count: int = 1,
    quantum_seconds: int = 3600,
) -> int:
    """Per-quantum accrual for ``ip_count`` floating IPs.

    Mirrors the catalog's monthly->hourly convention: Decimal division by
    720 with ROUND_HALF_UP to whole minor units per hour, then prorated
    across the quantum. Deterministic integer arithmetic only.
    """
    if quantum_seconds <= 0:
        raise FloatingIpPricingError("quantum_seconds must be positive")
    monthly = Decimal(floating_monthly_minor(card, ip_count))
    hourly = (monthly / HOURS_PER_MONTH).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    per_quantum = hourly * Decimal(quantum_seconds) / Decimal(3600)
    return int(per_quantum.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
