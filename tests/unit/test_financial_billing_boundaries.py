"""Money outcomes at exact quantum, subsecond and civil-clock boundaries."""

from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal

import pytest

from cloud_platform.core.money import Money
from cloud_platform.modules.billing.domain import (
    BillingPolicy,
    PricedPeriod,
    PriceSnapshot,
    calculate_usage_charge,
    calculate_usage_segments,
    sum_segments,
)


def price(selling="2", cost="1", *, quantum=3600, minimum=1):
    return PriceSnapshot(
        provider_cost_per_quantum=Money(Decimal(cost), "USD"),
        customer_price_per_quantum=Money(Decimal(selling), "USD"),
        policy=BillingPolicy(quantum_seconds=quantum, minimum_quanta=minimum),
    )


@pytest.mark.parametrize("microseconds,extra", [(0, 0), (1, 1), (999999, 1)])
def test_large_duration_preserves_microsecond_partial_quantum(microseconds, extra):
    start = datetime(1970, 1, 1, tzinfo=UTC)
    exact_end = datetime(2600, 1, 1, tzinfo=UTC)
    end = exact_end + timedelta(microseconds=microseconds)
    exact_quanta = (exact_end - start).days * 24
    charged = calculate_usage_charge(start, end, price())
    assert charged.quanta == exact_quanta + extra
    assert charged.customer_charge.amount == Decimal(2 * (exact_quanta + extra))
    assert charged.provider_cost.amount == Decimal(exact_quanta + extra)


@pytest.mark.parametrize("year", [1969, 2026])
@pytest.mark.parametrize("end_microseconds,segments", [(0, 1), (1, 2)])
def test_grid_boundary_just_before_end_uses_new_price(year, end_microseconds, segments):
    start = datetime(year, 1, 1, 0, 0, tzinfo=UTC)
    boundary = start + timedelta(hours=1)
    end = boundary + timedelta(microseconds=end_microseconds)
    schedule = (
        PricedPeriod(start, price(selling="2", cost="1")),
        PricedPeriod(boundary, price(selling="7", cost="3")),
    )
    result = calculate_usage_segments(start, end, schedule)
    assert len(result) == segments
    first = result[0]
    assert first.start == start and first.end == boundary and first.quanta == 1
    if end_microseconds:
        last = result[1]
        assert last.start == boundary and last.end == end and last.quanta == 1
        assert last.customer_charge.amount == Decimal("7")
    total = sum_segments(result)
    assert total.customer_charge.amount == Decimal("9" if end_microseconds else "2")
    assert total.provider_cost.amount == Decimal("4" if end_microseconds else "1")


class RepeatedCivilHour(tzinfo):
    """A deterministic fall-back clock; no host timezone-data dependency."""

    def utcoffset(self, dt):
        return timedelta(hours=-5 if dt is not None and dt.fold else -4)

    def dst(self, dt):
        return timedelta(0)

    def tzname(self, dt):
        return "repeated-civil-hour"


def test_repeated_civil_hour_bills_elapsed_instants_not_wallclock_labels():
    zone = RepeatedCivilHour()
    start = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
    end = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1)
    charge = calculate_usage_charge(start, end, price(quantum=1800))
    assert charge.quanta == 2
    assert charge.customer_charge.amount == Decimal("4")
    segments = calculate_usage_segments(start, end, (PricedPeriod(start, price(quantum=1800)),))
    assert len(segments) == 2
    assert sum_segments(segments).customer_charge.amount == Decimal("4")


@pytest.mark.parametrize(
    "field,value",
    [
        ("quantum_seconds", True),
        ("quantum_seconds", 1.5),
        ("quantum_seconds", 0),
        ("minimum_quanta", False),
        ("minimum_quanta", 0.5),
        ("minimum_quanta", -1),
    ],
)
def test_noninteger_policies_cannot_define_financial_periods(field, value):
    with pytest.raises(ValueError, match="integer"):
        BillingPolicy(**{field: value})


def test_empty_usage_respects_explicit_zero_minimum_without_a_charge():
    moment = datetime(2026, 10, 1, tzinfo=UTC)
    charged = calculate_usage_charge(moment, moment, price(minimum=0))
    assert charged.quanta == 0
    assert charged.customer_charge.amount == Decimal(0)
    assert charged.provider_cost.amount == Decimal(0)
