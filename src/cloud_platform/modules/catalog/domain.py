"""Catalog domain: provider pricing ingestion (M04-005).

Prices are **location-aware** and come from provider data — nothing is
hard-coded. All financial arithmetic uses :class:`~decimal.Decimal` only
(never float), and per-quantum prices are stored as integer minor units.

When a provider reports both an hourly and a monthly price for a location,
the provider's own **hourly** price is authoritative. Only when hourly is
absent is it derived as ``monthly / HOURS_PER_MONTH`` with ``ROUND_HALF_UP``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol
from uuid import UUID

logger = logging.getLogger(__name__)

# Billing-hours basis used to derive an hourly price from a monthly one.
HOURS_PER_MONTH: Decimal = Decimal(720)


class PricingError(Exception):
    """Raised when provider pricing cannot be ingested."""


class CatalogError(Exception):
    """Base error for catalog operations."""


class OfferNotFoundError(CatalogError):
    """Raised when a plan/location combination is not in the catalog."""


@dataclass(frozen=True, slots=True)
class ProviderPriceEntry:
    """One location's price as reported by a provider.

    ``hourly``/``monthly`` are major units (e.g. "15.87" EUR) parsed from the
    provider's own strings. At least one must be present; ``hourly`` wins.
    """

    location_id: str
    currency: str
    hourly: Decimal | None = None
    monthly: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.location_id or not self.location_id.strip():
            raise ValueError("location_id must not be empty")
        if not self.currency or not self.currency.strip():
            raise ValueError("currency must not be empty")
        if self.hourly is None and self.monthly is None:
            raise ValueError("price entry has neither hourly nor monthly price")
        for name, value in (("hourly", self.hourly), ("monthly", self.monthly)):
            if value is not None and value < 0:
                raise ValueError(f"{name} price must not be negative")


@dataclass(frozen=True, slots=True)
class PlanPricing:
    """A provider plan's specs plus its per-location prices."""

    plan_id: str
    name: str
    architecture: str
    vcpu: int
    memory_mb: int
    disk_gb: int
    prices: tuple[ProviderPriceEntry, ...]
    description: str | None = None
    cpu_type: str | None = None
    storage_type: str | None = None

    def __post_init__(self) -> None:
        if not self.plan_id or not self.plan_id.strip():
            raise ValueError("plan_id must not be empty")
        if not self.name or not self.name.strip():
            raise ValueError("name must not be empty")
        if not self.prices:
            raise ValueError("plan must have at least one price entry")
        for name, value in (
            ("vcpu", self.vcpu),
            ("memory_mb", self.memory_mb),
            ("disk_gb", self.disk_gb),
        ):
            if value < 0:
                raise ValueError(f"{name} must not be negative")


@dataclass(frozen=True, slots=True)
class IngestedPrice:
    """One persisted location price after ingestion."""

    location_id: str
    currency: str
    hourly_minor: int
    created: bool = False


@dataclass(frozen=True, slots=True)
class IngestedPricing:
    """Outcome of ingesting one plan's per-location prices."""

    plan_id: str
    prices: tuple[IngestedPrice, ...]


def to_minor_units(major: Decimal, factor: int = 100) -> int:
    """Convert a major-unit Decimal to integer minor units, half-up.

    Uses Decimal arithmetic only — no float anywhere.
    """
    if factor <= 0:
        raise ValueError("factor must be positive")
    if major < 0:
        raise ValueError("amount must not be negative")
    return int((major * factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def hourly_minor(entry: ProviderPriceEntry) -> int:
    """The provider's hourly price in minor units for one location.

    Prefers the provider's own hourly price; derives from monthly only when
    hourly is absent.
    """
    if entry.hourly is not None:
        return to_minor_units(entry.hourly)
    assert entry.monthly is not None  # guaranteed by ProviderPriceEntry
    hourly_major = entry.monthly / HOURS_PER_MONTH
    return to_minor_units(hourly_major)


@dataclass(frozen=True, slots=True)
class CatalogEntrySpec:
    """Full specification of one location-aware catalog row."""

    provider_key: str
    plan_id: str
    location_id: str
    name: str
    architecture: str
    vcpu: int
    memory_mb: int
    disk_gb: int
    currency: str
    price_per_quantum: int
    quantum_seconds: int = 3600
    description: str | None = None
    extra_metadata: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class OfferRef:
    """Identifies one sellable offer: a plan at a location of a provider."""

    provider_key: str
    plan_id: str
    location_id: str

    def __post_init__(self) -> None:
        for name, value in (
            ("provider_key", self.provider_key),
            ("plan_id", self.plan_id),
            ("location_id", self.location_id),
        ):
            if not value or not value.strip():
                raise ValueError(f"{name} must not be empty")

    @property
    def key(self) -> str:
        """Stable human-readable identifier for audit metadata."""
        return f"{self.provider_key}/{self.plan_id}/{self.location_id}"


@dataclass(frozen=True, slots=True)
class OfferState:
    """Current visibility state of one offer.

    ``id`` is the catalog row id, required to pin a created server to the
    exact offer it was bought from.
    """

    id: UUID
    ref: OfferRef
    name: str
    enabled: bool
    price_per_quantum: int
    currency: str


class CatalogRepository(Protocol):
    """Port for persisting location-aware catalog rows."""

    async def upsert_entry(self, spec: CatalogEntrySpec) -> bool:
        """Create or update the row for (provider, plan, location).

        Returns True if the row was created, False if it was updated.
        """
        ...

    async def get_offer(self, ref: OfferRef) -> OfferState | None:
        """Current state of one offer, or None if the combination is unknown."""
        ...

    async def set_offer_enabled(self, ref: OfferRef, enabled: bool) -> None:
        """Change visibility of one offer.

        Raises:
            LookupError: If the combination does not exist.
        """
        ...


# ---------------------------------------------------------------------------
# Catalog sync job with lock (M04-006)
# ---------------------------------------------------------------------------


class CatalogSyncLock(Protocol):
    """Port for a lock that serializes catalog sync runs.

    ``guard`` yields True when the lock was acquired and False when another
    sync already holds it. The implementation must keep the lock held for the
    whole duration of the context and release it on exit (including errors).
    """

    def guard(self) -> AbstractAsyncContextManager[bool]:
        """Enter the sync critical section; yields whether the lock was won."""
        ...


@dataclass(frozen=True, slots=True)
class CatalogSyncStepReport:
    """Outcome of one sync step (e.g. locations, plans, images)."""

    name: str
    fetched: int = 0
    upserted: int = 0
    skipped: int = 0
    errors: tuple[str, ...] = ()
    error: str | None = None  # step-level failure, when the step raised


@dataclass(frozen=True, slots=True)
class CatalogSyncRunReport:
    """Outcome of one catalog sync job run."""

    ran: bool
    steps: tuple[CatalogSyncStepReport, ...] = ()
    reason: str | None = None  # set when the run was skipped (lock held)

    @property
    def skipped(self) -> bool:
        return not self.ran


@dataclass(frozen=True, slots=True)
class CatalogSyncStep:
    """One named sync unit the job executes while holding the lock."""

    name: str
    run: Callable[[], Awaitable[CatalogSyncStepReport]]


class CatalogSyncJob:
    """Runs catalog sync steps exclusively under a lock.

    The lock makes concurrent syncs serialize: the loser skips the whole run
    (no partial writes), so two syncs can never interleave their upserts and
    corrupt the catalog. A step that raises is recorded as a failed step
    report (the run continues with the remaining steps — the next scheduled
    run retries the failed unit) and the lock is always released.
    """

    def __init__(
        self,
        lock: CatalogSyncLock,
        steps: tuple[CatalogSyncStep, ...] | list[CatalogSyncStep],
    ) -> None:
        if not steps:
            raise ValueError("a catalog sync job needs at least one step")
        self._lock = lock
        self._steps = tuple(steps)

    async def run(self) -> CatalogSyncRunReport:
        """Execute the sync, or skip it when another sync holds the lock."""
        async with self._lock.guard() as acquired:
            if not acquired:
                return CatalogSyncRunReport(
                    ran=False,
                    reason="catalog sync lock is held by another sync",
                )
            reports: list[CatalogSyncStepReport] = []
            for step in self._steps:
                try:
                    reports.append(await step.run())
                except Exception as exc:
                    logger.exception("catalog sync step %r failed", step.name)
                    reports.append(CatalogSyncStepReport(name=step.name, error=str(exc)))
            return CatalogSyncRunReport(ran=True, steps=tuple(reports))
