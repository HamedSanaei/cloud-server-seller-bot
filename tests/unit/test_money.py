from decimal import Decimal

import pytest

from cloud_platform.core.money import Money


def test_money_addition_requires_same_currency() -> None:
    assert Money(Decimal("1.10"), "eur") + Money(Decimal("2.20"), "EUR") == Money(Decimal("3.30"), "EUR")
    with pytest.raises(ValueError, match="currency mismatch"):
        _ = Money(Decimal("1"), "EUR") + Money(Decimal("1"), "USD")
