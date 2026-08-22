from datetime import UTC, datetime, timedelta
from decimal import Decimal

from cloud_platform.core.money import Money
from cloud_platform.modules.billing.domain import (
    BillingPolicy,
    PriceSnapshot,
    calculate_usage_charge,
)


def test_hourly_policy_rounds_partial_hour_up() -> None:
    start = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)
    price = PriceSnapshot(
        provider_cost_per_quantum=Money(Decimal("0.01"), "EUR"),
        customer_price_per_quantum=Money(Decimal("0.02"), "EUR"),
        policy=BillingPolicy(quantum_seconds=3600, minimum_quanta=1),
    )
    charge = calculate_usage_charge(start, start + timedelta(hours=1, seconds=1), price)
    assert charge.quanta == 2
    assert charge.provider_cost.amount == Decimal("0.02")
    assert charge.customer_charge.amount == Decimal("0.04")


def test_minimum_one_quantum_even_for_short_lived_server() -> None:
    start = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)
    price = PriceSnapshot(
        provider_cost_per_quantum=Money(Decimal("0.01"), "EUR"),
        customer_price_per_quantum=Money(Decimal("0.02"), "EUR"),
        policy=BillingPolicy(),
    )
    charge = calculate_usage_charge(start, start + timedelta(seconds=5), price)
    assert charge.quanta == 1
