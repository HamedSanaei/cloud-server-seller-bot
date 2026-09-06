"""Tests for the LeaseWeb catalog/pricing mapper (Decimal-only, no float)."""

from decimal import Decimal

import pytest

from cloud_platform.providers.leaseweb.sync import plan_pricing_from_leaseweb


def test_mapper_preserves_decimal_prices() -> None:
    pricing = plan_pricing_from_leaseweb(
        {
            "id": "lsw.mini",
            "name": "Mini",
            "cpu": 1,
            "memoryMb": 1024,
            "disk": 25,
            "pricePerHour": "0.015",
            "pricePerMonth": "10.00",
            "currency": "EUR",
        },
        location_id="AMS-01",
    )
    assert pricing.plan_id == "lsw.mini"
    assert pricing.prices[0].location_id == "AMS-01"
    assert pricing.prices[0].hourly == Decimal("0.015")
    assert pricing.prices[0].monthly == Decimal("10.00")
    assert pricing.vcpu == 1


def test_mapper_requires_price() -> None:
    with pytest.raises(ValueError, match="neither hourly nor monthly"):
        plan_pricing_from_leaseweb({"id": "x", "cpu": 1}, location_id="AMS-01")


def test_mapper_requires_location() -> None:
    with pytest.raises(ValueError, match="location_id"):
        plan_pricing_from_leaseweb({"id": "x", "pricePerHour": "0.01"}, location_id=" ")


def test_mapper_rejects_negative_price() -> None:
    with pytest.raises(ValueError, match="neither hourly nor monthly"):
        plan_pricing_from_leaseweb({"id": "x", "pricePerHour": "-1"}, location_id="AMS-01")
