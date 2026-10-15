from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from cloud_platform.core.money import Money


@dataclass(frozen=True, slots=True)
class BillingPolicy:
    quantum_seconds: int = 3600
    minimum_quanta: int = 1

    def __post_init__(self) -> None:
        if type(self.quantum_seconds) is not int or self.quantum_seconds <= 0:
            raise ValueError("quantum_seconds must be a positive integer")
        if type(self.minimum_quanta) is not int or self.minimum_quanta < 0:
            raise ValueError("minimum_quanta must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class PriceSnapshot:
    provider_cost_per_quantum: Money
    customer_price_per_quantum: Money
    policy: BillingPolicy


@dataclass(frozen=True, slots=True)
class UsageCharge:
    quanta: int
    provider_cost: Money
    customer_charge: Money


def calculate_usage_charge(
    started_at: datetime, ended_at: datetime, price: PriceSnapshot
) -> UsageCharge:
    if started_at.utcoffset() is not None and ended_at.utcoffset() is not None:
        started_at = started_at.astimezone(UTC)
        ended_at = ended_at.astimezone(UTC)
    if ended_at < started_at:
        raise ValueError("ended_at cannot be before started_at")
    duration = ended_at - started_at
    elapsed_microseconds = (duration.days * 86_400 + duration.seconds) * 1_000_000
    elapsed_microseconds += duration.microseconds
    quantum_microseconds = price.policy.quantum_seconds * 1_000_000
    quanta = -(-elapsed_microseconds // quantum_microseconds)
    quanta = max(price.policy.minimum_quanta, quanta)
    return UsageCharge(
        quanta=quanta,
        provider_cost=Money(
            price.provider_cost_per_quantum.amount * quanta,
            price.provider_cost_per_quantum.currency,
        ),
        customer_charge=Money(
            price.customer_price_per_quantum.amount * quanta,
            price.customer_price_per_quantum.currency,
        ),
    )


@dataclass(frozen=True, slots=True)
class PricedPeriod:
    """One (effective_from, price) step of a usage window's price schedule.

    The schedule is ascending and non-overlapping: from ``effective_from``
    (inclusive) until the next period's ``effective_from`` (exclusive).
    """

    effective_from: datetime
    price: PriceSnapshot


@dataclass(frozen=True, slots=True)
class UsageSegment:
    """One grid-aligned slice of a usage window, charged at its own price.

    ``quanta`` is the ceiling of the slice's duration in quanta (a partial
    quantum is billed as a full one), floored by the policy minimum; the
    slice therefore always carries at least one quantum of charge.
    """

    start: datetime
    end: datetime
    quanta: int
    provider_cost: Money
    customer_charge: Money


def _require_aware(dt: datetime, name: str) -> None:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"{name} must be timezone-aware")


def _grid_boundaries(
    started_at: datetime, ended_at: datetime, quantum_seconds: int
) -> list[datetime]:
    """Quantum-grid instants strictly inside the window (UTC-aligned)."""
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    start = started_at.astimezone(UTC)
    end = ended_at.astimezone(UTC)
    duration = start - epoch
    start_seconds = duration.days * 86_400 + duration.seconds
    first_seconds = (start_seconds // quantum_seconds + 1) * quantum_seconds
    boundary = epoch + timedelta(seconds=first_seconds)
    boundaries: list[datetime] = []
    while boundary < end:
        boundaries.append(boundary)
        boundary += timedelta(seconds=quantum_seconds)
    return boundaries


def _price_at(schedule: tuple[PricedPeriod, ...], at: datetime) -> PriceSnapshot:
    price: PriceSnapshot | None = None
    for period in schedule:
        if period.effective_from <= at:
            price = period.price
        else:
            break
    if price is None:
        raise ValueError("no priced period covers the window start")
    return price


def calculate_usage_segments(
    started_at: datetime,
    ended_at: datetime,
    schedule: tuple[PricedPeriod, ...] | list[PricedPeriod],
) -> list[UsageSegment]:
    """Split a usage window into quantum-grid segments and price each one.

    Boundaries: the window start, every quantum-grid instant (UTC-aligned,
    i.e. ``floor(epoch / quantum) * quantum``) strictly inside the window,
    and the window end. Each slice is charged with ``calculate_usage_charge``
    at the price whose period covers the slice's start, so a partial slice is
    billed as a full quantum and a price change mid-window applies from the
    next grid boundary on.

    A zero-length window produces no segments (no usage, no charge); the
    minimum-quantum floor never fires on an empty window.
    """
    _require_aware(started_at, "started_at")
    _require_aware(ended_at, "ended_at")
    started_at = started_at.astimezone(UTC)
    ended_at = ended_at.astimezone(UTC)
    if ended_at < started_at:
        raise ValueError("ended_at cannot be before started_at")
    if not schedule:
        raise ValueError("schedule must have at least one priced period")
    for period in schedule:
        _require_aware(period.effective_from, "PricedPeriod.effective_from")
    schedule = tuple(
        PricedPeriod(period.effective_from.astimezone(UTC), period.price) for period in schedule
    )
    for earlier, later in pairwise(schedule):
        if later.effective_from < earlier.effective_from:
            raise ValueError("schedule must be ascending by effective_from")

    segments: list[UsageSegment] = []
    if ended_at == started_at:
        return segments
    quantum = schedule[0].price.policy.quantum_seconds
    cut_points = [started_at, *_grid_boundaries(started_at, ended_at, quantum), ended_at]
    for a, b in pairwise(cut_points):
        if b <= a:
            continue
        charge = calculate_usage_charge(a, b, _price_at(schedule, a))
        segments.append(
            UsageSegment(
                start=a,
                end=b,
                quanta=charge.quanta,
                provider_cost=charge.provider_cost,
                customer_charge=charge.customer_charge,
            )
        )
    return segments


def sum_segments(segments: list[UsageSegment]) -> UsageCharge:
    """Aggregate segment charges (currency must be consistent)."""
    if not segments:
        raise ValueError("cannot sum an empty segment list")
    quanta = sum(s.quanta for s in segments)
    provider_cost = segments[0].provider_cost
    customer_charge = segments[0].customer_charge
    for s in segments[1:]:
        provider_cost = provider_cost + s.provider_cost
        customer_charge = customer_charge + s.customer_charge
    return UsageCharge(
        quanta=quanta,
        provider_cost=provider_cost,
        customer_charge=customer_charge,
    )
