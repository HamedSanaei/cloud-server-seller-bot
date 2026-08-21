from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_CEILING

from cloud_platform.core.money import Money


@dataclass(frozen=True, slots=True)
class BillingPolicy:
    quantum_seconds: int = 3600
    minimum_quanta: int = 1

    def __post_init__(self) -> None:
        if self.quantum_seconds <= 0:
            raise ValueError("quantum_seconds must be positive")
        if self.minimum_quanta < 0:
            raise ValueError("minimum_quanta cannot be negative")


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


def calculate_usage_charge(started_at: datetime, ended_at: datetime, price: PriceSnapshot) -> UsageCharge:
    if ended_at < started_at:
        raise ValueError("ended_at cannot be before started_at")
    elapsed = Decimal(str((ended_at - started_at).total_seconds()))
    raw_quanta = elapsed / Decimal(price.policy.quantum_seconds)
    quanta = int(raw_quanta.to_integral_value(rounding=ROUND_CEILING))
    quanta = max(price.policy.minimum_quanta, quanta)
    return UsageCharge(
        quanta=quanta,
        provider_cost=Money(price.provider_cost_per_quantum.amount * quanta, price.provider_cost_per_quantum.currency),
        customer_charge=Money(price.customer_price_per_quantum.amount * quanta, price.customer_price_per_quantum.currency),
    )
