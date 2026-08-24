"""Backup toggle domain values (M13-005).

The surcharge is an operator-declared percentage (BASIS POINTS) of the
server's monthly price - the same shape providers use (e.g. 2000 bp =
20%). All math is exact integer/Decimal; floats never touch money.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

_BPS_MAX = 10_000  # 100%


class BackupPricingError(ValueError):
    """Invalid backup rate card."""


class StaleConfirmationError(Exception):
    """The price impact changed between preview and confirmed mutation."""


@dataclass(frozen=True, slots=True)
class BackupRateCard:
    """Operator-declared backup surcharge.

    ``surcharge_bps`` is basis points of the server's monthly price
    (10000 bp = 100%). 0 bp is valid and means "backups are free here" -
    a deliberate decision, not a guessed provider default.
    """

    surcharge_bps: int

    def __post_init__(self) -> None:
        if not 0 <= self.surcharge_bps <= _BPS_MAX:
            raise BackupPricingError("surcharge_bps must be between 0 and 10000")

    @classmethod
    def from_percent(cls, percent: int) -> BackupRateCard:
        return cls(surcharge_bps=percent * 100)


def backup_monthly_minor(card: BackupRateCard, base_monthly_minor: int) -> int:
    """Monthly backup surcharge for a server priced ``base_monthly_minor``.

    Exact Decimal arithmetic with ROUND_HALF_UP to whole minor units -
    identical convention to every other cost surface in the platform.
    """
    if base_monthly_minor < 0:
        raise BackupPricingError("base_monthly_minor must not be negative")
    surcharge = Decimal(base_monthly_minor) * Decimal(card.surcharge_bps) / Decimal(10000)
    return int(surcharge.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True, slots=True)
class BackupPriceImpact:
    """What enabling/disabling backups does to one server's monthly price."""

    server_id: UUID
    enabled: bool  # the TARGET state this impact describes
    base_monthly_minor: int  # the server's immutable snapshot price
    surcharge_monthly_minor: int  # the backups surcharge at current rate card
    total_monthly_minor: int  # base + surcharge when enabled, else base
    delta_monthly_minor: int  # signed change vs today's bill (+ on, - off)

    def __post_init__(self) -> None:
        if self.base_monthly_minor < 0 or self.surcharge_monthly_minor < 0:
            raise BackupPricingError("price components must not be negative")


@dataclass(frozen=True, slots=True)
class BackupSettings:
    """One server's persisted backups state."""

    server_id: UUID
    enabled: bool
    surcharge_bps_at_change: int
    updated_by: UUID | None = None
    updated_at: datetime | None = None
