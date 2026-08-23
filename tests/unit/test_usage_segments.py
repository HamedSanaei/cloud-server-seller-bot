"""Tests for the usage segment calculator (M06-004).

Acceptance: boundary/rounding tests cover partial quantum.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import pytest

from cloud_platform.core.money import Money
from cloud_platform.modules.billing.domain import (
    BillingPolicy,
    PricedPeriod,
    PriceSnapshot,
    UsageSegment,
    calculate_usage_charge,
    calculate_usage_segments,
    sum_segments,
)

HOUR = BillingPolicy(quantum_seconds=3600)
DAY = BillingPolicy(quantum_seconds=86400)
MINUTE = BillingPolicy(quantum_seconds=60)


def _price(policy: BillingPolicy, cost: str = "107", selling: str = "200") -> PriceSnapshot:
    return PriceSnapshot(
        provider_cost_per_quantum=Money(Decimal(cost), "EUR"),
        customer_price_per_quantum=Money(Decimal(selling), "EUR"),
        policy=policy,
    )


def _schedule(*prices: PriceSnapshot, at: datetime) -> tuple[PricedPeriod, ...]:
    return (PricedPeriod(effective_from=at, price=prices[0]),)


T0 = datetime(2026, 8, 23, 0, 0, tzinfo=UTC)


class TestPartialQuantum:
    """The acceptance: partial quanta are billed as full quanta."""

    async def test_sub_quantum_slice_bills_one_full_quantum(self) -> None:
        # 30 minutes of an hourly quantum -> 1 quantum, not 0 or 0.5.
        segments = calculate_usage_segments(
            T0 + timedelta(minutes=30), T0 + timedelta(hours=1), _schedule(_price(HOUR), at=T0)
        )
        assert len(segments) == 1
        assert segments[0].quanta == 1
        assert segments[0].customer_charge == Money(Decimal(200), "EUR")
        assert segments[0].provider_cost == Money(Decimal(107), "EUR")

    async def test_one_minute_over_boundary_bills_two_quanta(self) -> None:
        # 10:30 -> 12:01 crosses grid hours at 11:00 and 12:00.
        # Slices: [10:30,11:00) partial -> 1, [11:00,12:00) full -> 1,
        # [12:00,12:01) partial -> 1.
        segments = calculate_usage_segments(
            T0 + timedelta(hours=10, minutes=30),
            T0 + timedelta(hours=12, minutes=1),
            _schedule(_price(HOUR), at=T0),
        )
        assert [s.quanta for s in segments] == [1, 1, 1]
        total = sum_segments(segments)
        assert total.quanta == 3
        assert total.customer_charge == Money(Decimal(600), "EUR")
        assert total.provider_cost == Money(Decimal(321), "EUR")

    async def test_exact_grid_window_bills_exact_quanta(self) -> None:
        # 10:00 -> 12:00 aligned to the hour grid -> exactly 2 quanta.
        segments = calculate_usage_segments(
            T0 + timedelta(hours=10), T0 + timedelta(hours=12), _schedule(_price(HOUR), at=T0)
        )
        assert len(segments) == 2
        assert all(s.quanta == 1 for s in segments)
        assert sum_segments(segments).quanta == 2


class TestBoundaries:
    async def test_start_exactly_on_grid_has_no_zero_prefix(self) -> None:
        segments = calculate_usage_segments(
            T0 + timedelta(hours=10), T0 + timedelta(hours=11), _schedule(_price(HOUR), at=T0)
        )
        assert [s.start for s in segments] == [T0 + timedelta(hours=10)]
        assert [s.end for s in segments] == [T0 + timedelta(hours=11)]

    async def test_zero_length_window_has_no_segments(self) -> None:
        assert calculate_usage_segments(T0, T0, _schedule(_price(HOUR), at=T0)) == []

    async def test_end_exactly_on_grid_includes_boundary_slice(self) -> None:
        # 10:30 -> 12:00: slices [10:30,11:00) and [11:00,12:00); the closing
        # grid instant belongs to the last slice, not to a zero-length tail.
        segments = calculate_usage_segments(
            T0 + timedelta(hours=10, minutes=30),
            T0 + timedelta(hours=12),
            _schedule(_price(HOUR), at=T0),
        )
        assert [s.quanta for s in segments] == [1, 1]

    async def test_day_quantum_midnight_span_bills_two_days(self) -> None:
        # 23:50 -> 00:10 crosses the UTC day grid -> 2 day quanta (20 minutes
        # of wall time is not billed as a single partial day).
        segments = calculate_usage_segments(
            T0 + timedelta(hours=23, minutes=50),
            T0 + timedelta(hours=24, minutes=10),
            _schedule(_price(DAY), at=T0),
        )
        assert len(segments) == 2
        assert sum_segments(segments).quanta == 2

    async def test_minute_quantum_partial_bills_full(self) -> None:
        # 45 seconds of a minute quantum -> 1 quantum.
        segments = calculate_usage_segments(
            T0, T0 + timedelta(seconds=45), _schedule(_price(MINUTE), at=T0)
        )
        assert [s.quanta for s in segments] == [1]

    async def test_segments_are_contiguous_and_cover_window(self) -> None:
        start = T0 + timedelta(hours=10, minutes=30)
        end = T0 + timedelta(hours=12, minutes=1)
        segments = calculate_usage_segments(start, end, _schedule(_price(HOUR), at=T0))
        assert segments[0].start == start
        assert segments[-1].end == end
        for a, b in pairwise(segments):
            assert a.end == b.start

    async def test_single_price_total_matches_single_interval_charge(self) -> None:
        # For a grid-aligned window the segment sum must equal the flat
        # single-interval calculation (same price, same quanta).
        start = T0 + timedelta(hours=10)
        end = T0 + timedelta(hours=13)
        price = _price(HOUR, cost="107", selling="200")
        segments = calculate_usage_segments(start, end, _schedule(price, at=T0))
        flat = calculate_usage_charge(start, end, price)
        total = sum_segments(segments)
        assert total.quanta == flat.quanta
        assert total.customer_charge == flat.customer_charge
        assert total.provider_cost == flat.provider_cost


class TestPriceSchedule:
    async def test_price_change_applies_from_next_grid_boundary(self) -> None:
        # Price A until 12:00, price B from 12:00. Window 10:30 -> 13:30:
        # [10:30,11:00) A, [11:00,12:00) A, [12:00,13:00) B, [13:00,13:30) B.
        price_a = _price(HOUR, cost="100", selling="100")
        price_b = _price(HOUR, cost="200", selling="500")
        change = T0 + timedelta(hours=12)
        schedule = (
            PricedPeriod(effective_from=T0, price=price_a),
            PricedPeriod(effective_from=change, price=price_b),
        )
        segments = calculate_usage_segments(
            T0 + timedelta(hours=10, minutes=30), T0 + timedelta(hours=13, minutes=30), schedule
        )
        assert [(s.start, s.customer_charge.amount) for s in segments] == [
            (T0 + timedelta(hours=10, minutes=30), Decimal(100)),
            (T0 + timedelta(hours=11), Decimal(100)),
            (T0 + timedelta(hours=12), Decimal(500)),
            (T0 + timedelta(hours=13), Decimal(500)),
        ]
        total = sum_segments(segments)
        assert total.customer_charge == Money(Decimal(1200), "EUR")
        assert total.provider_cost == Money(Decimal(600), "EUR")

    async def test_schedule_change_inside_partial_slice_never_mid_slices(self) -> None:
        # A price step at 11:30 (inside the [11:00,12:00) grid slice) still
        # only takes effect from the 12:00 boundary: segments are grid-aligned.
        price_a = _price(HOUR, cost="100", selling="100")
        price_b = _price(HOUR, cost="200", selling="500")
        schedule = (
            PricedPeriod(effective_from=T0, price=price_a),
            PricedPeriod(effective_from=T0 + timedelta(hours=11, minutes=30), price=price_b),
        )
        segments = calculate_usage_segments(
            T0 + timedelta(hours=10), T0 + timedelta(hours=13), schedule
        )
        assert [(s.start, s.customer_charge.amount) for s in segments] == [
            (T0 + timedelta(hours=10), Decimal(100)),
            (T0 + timedelta(hours=11), Decimal(100)),
            (T0 + timedelta(hours=12), Decimal(500)),
        ]


class TestMinimumQuanta:
    async def test_minimum_applies_per_segment(self) -> None:
        policy = BillingPolicy(quantum_seconds=3600, minimum_quanta=2)
        segments = calculate_usage_segments(
            T0 + timedelta(minutes=30),
            T0 + timedelta(hours=2, minutes=30),
            _schedule(_price(policy), at=T0),
        )
        # Slices [0:30,1:00), [1:00,2:00), [2:00,2:30): even the full middle
        # slice is floored to the 2-quantum minimum.
        assert [s.quanta for s in segments] == [2, 2, 2]
        assert sum_segments(segments).quanta == 6

    async def test_zero_window_ignores_minimum(self) -> None:
        policy = BillingPolicy(quantum_seconds=3600, minimum_quanta=1)
        assert calculate_usage_segments(T0, T0, _schedule(_price(policy), at=T0)) == []


class TestValidation:
    async def test_ended_before_started_raises(self) -> None:
        with pytest.raises(ValueError, match="ended_at cannot be before started_at"):
            calculate_usage_segments(T0 + timedelta(hours=1), T0, _schedule(_price(HOUR), at=T0))

    async def test_naive_started_raises(self) -> None:
        with pytest.raises(ValueError, match="started_at must be timezone-aware"):
            calculate_usage_segments(T0.replace(tzinfo=None), T0, _schedule(_price(HOUR), at=T0))

    async def test_naive_ended_raises(self) -> None:
        with pytest.raises(ValueError, match="ended_at must be timezone-aware"):
            calculate_usage_segments(T0, T0.replace(tzinfo=None), _schedule(_price(HOUR), at=T0))

    async def test_empty_schedule_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one priced period"):
            calculate_usage_segments(T0, T0 + timedelta(hours=1), [])

    async def test_unsorted_schedule_raises(self) -> None:
        price = _price(HOUR)
        schedule = (
            PricedPeriod(effective_from=T0 + timedelta(hours=2), price=price),
            PricedPeriod(effective_from=T0, price=price),
        )
        with pytest.raises(ValueError, match="ascending"):
            calculate_usage_segments(T0, T0 + timedelta(hours=1), schedule)

    async def test_window_before_first_period_raises(self) -> None:
        with pytest.raises(ValueError, match="no priced period covers"):
            calculate_usage_segments(
                T0, T0 + timedelta(hours=1), _schedule(_price(HOUR), at=T0 + timedelta(hours=2))
            )

    async def test_non_utc_offset_is_normalized(self) -> None:
        # Tehran (UTC+3:30) window [10:00 T, 11:00 T] = [06:30 UTC, 07:30
        # UTC]; the only UTC hour boundary inside is 07:00 UTC.
        from datetime import timezone

        tehran = timezone(timedelta(hours=3, minutes=30))
        start = datetime(2026, 8, 23, 10, 0, tzinfo=tehran)
        end = datetime(2026, 8, 23, 11, 0, tzinfo=tehran)
        segments = calculate_usage_segments(start, end, _schedule(_price(HOUR), at=T0))
        assert [s.quanta for s in segments] == [1, 1]
        assert segments[0].end == T0 + timedelta(hours=7)


class TestSumSegments:
    async def test_empty_sum_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            sum_segments([])

    async def test_mixed_currency_raises(self) -> None:
        a = UsageSegment(
            start=T0,
            end=T0 + timedelta(hours=1),
            quanta=1,
            provider_cost=Money(Decimal(1), "EUR"),
            customer_charge=Money(Decimal(1), "EUR"),
        )
        b = UsageSegment(
            start=T0 + timedelta(hours=1),
            end=T0 + timedelta(hours=2),
            quanta=1,
            provider_cost=Money(Decimal(1), "USD"),
            customer_charge=Money(Decimal(1), "USD"),
        )
        with pytest.raises(ValueError, match="currency"):
            sum_segments([a, b])
