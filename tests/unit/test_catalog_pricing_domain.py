"""Tests for catalog pricing domain: Decimal math, location-awareness, validation."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from cloud_platform.modules.catalog.domain import (
    HOURS_PER_MONTH,
    IngestedPrice,
    IngestedPricing,
    PlanPricing,
    ProviderPriceEntry,
    hourly_minor,
    to_minor_units,
)


class TestToMinorUnits:
    def test_exact_conversion(self) -> None:
        assert to_minor_units(Decimal("15.87")) == 1587
        assert to_minor_units(Decimal("0")) == 0

    def test_half_up_rounding(self) -> None:
        assert to_minor_units(Decimal("0.005")) == 1  # 0.5 -> 1
        assert to_minor_units(Decimal("0.004")) == 0  # 0.4 -> 0

    def test_decimals_do_not_suffer_float_error(self) -> None:
        # 2.675 * 100 is exactly 267.5 in Decimal -> 268 half-up.
        # In float, 2.675 * 100 == 267.49999999999997 -> would give 267.
        assert to_minor_units(Decimal("2.675")) == 268

    def test_custom_factor(self) -> None:
        assert to_minor_units(Decimal("1.5"), factor=1000) == 1500

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            to_minor_units(Decimal("-0.01"))

    def test_bad_factor_rejected(self) -> None:
        with pytest.raises(ValueError, match="factor"):
            to_minor_units(Decimal("1"), factor=0)


class TestHourlyMinor:
    def test_provider_hourly_price_is_authoritative(self) -> None:
        entry = ProviderPriceEntry(
            location_id="fsn1",
            currency="EUR",
            hourly=Decimal("0.03"),
            monthly=Decimal("15.87"),
        )
        # 0.03 -> 3 minor, NOT derived from monthly (which would give 2).
        assert hourly_minor(entry) == 3

    def test_derived_from_monthly_when_hourly_absent(self) -> None:
        entry = ProviderPriceEntry(
            location_id="nbg1",
            currency="EUR",
            monthly=Decimal("15.87"),
        )
        # 15.87 / 720 = 0.0220416... -> 2.20... minor -> 2
        assert hourly_minor(entry) == 2

    def test_derivation_rounds_half_up(self) -> None:
        entry = ProviderPriceEntry(
            location_id="hel1",
            currency="EUR",
            monthly=Decimal("5.83"),
        )
        # 5.83 / 720 * 100 = 0.8097... -> 1 (half-up).
        # The old float+truncation code (int(5.83*100/720)) yielded 0.
        assert hourly_minor(entry) == 1

    def test_hours_per_month_basis(self) -> None:
        assert HOURS_PER_MONTH == Decimal(720)


class TestProviderPriceEntryValidation:
    def test_requires_location(self) -> None:
        with pytest.raises(ValueError, match="location_id"):
            ProviderPriceEntry(location_id=" ", currency="EUR", hourly=Decimal("1"))

    def test_requires_currency(self) -> None:
        with pytest.raises(ValueError, match="currency"):
            ProviderPriceEntry(location_id="fsn1", currency="", hourly=Decimal("1"))

    def test_requires_at_least_one_price(self) -> None:
        with pytest.raises(ValueError, match="neither hourly nor monthly"):
            ProviderPriceEntry(location_id="fsn1", currency="EUR")

    def test_negative_prices_rejected(self) -> None:
        with pytest.raises(ValueError, match="hourly"):
            ProviderPriceEntry(location_id="l", currency="EUR", hourly=Decimal("-0.01"))
        with pytest.raises(ValueError, match="monthly"):
            ProviderPriceEntry(location_id="l", currency="EUR", monthly=Decimal("-1"))


class TestPlanPricingValidation:
    def _entry(self, location: str = "fsn1") -> ProviderPriceEntry:
        return ProviderPriceEntry(location_id=location, currency="EUR", hourly=Decimal("0.02"))

    def _plan(self, **overrides: object) -> PlanPricing:
        defaults: dict[str, object] = {
            "plan_id": "cx22",
            "name": "CX22",
            "architecture": "x86",
            "vcpu": 2,
            "memory_mb": 4096,
            "disk_gb": 40,
            "prices": (self._entry(),),
        }
        defaults.update(overrides)
        return PlanPricing(**defaults)  # type: ignore[arg-type]

    def test_valid_plan(self) -> None:
        plan = self._plan()
        assert plan.plan_id == "cx22"
        assert len(plan.prices) == 1

    def test_requires_plan_id(self) -> None:
        with pytest.raises(ValueError, match="plan_id"):
            self._plan(plan_id="")

    def test_requires_name(self) -> None:
        with pytest.raises(ValueError, match="name"):
            self._plan(name="  ")

    def test_requires_prices(self) -> None:
        with pytest.raises(ValueError, match="at least one price"):
            self._plan(prices=())

    def test_negative_specs_rejected(self) -> None:
        for field in ("vcpu", "memory_mb", "disk_gb"):
            with pytest.raises(ValueError, match=field):
                self._plan(**{field: -1})

    def test_multiple_locations_preserved(self) -> None:
        plan = self._plan(
            prices=(
                self._entry("fsn1"),
                self._entry("nbg1"),
                self._entry("hel1"),
            )
        )
        assert [e.location_id for e in plan.prices] == ["fsn1", "nbg1", "hel1"]


class TestResultTypes:
    def test_ingested_price_is_frozen(self) -> None:
        price = IngestedPrice(location_id="fsn1", currency="EUR", hourly_minor=2)
        with pytest.raises(FrozenInstanceError):
            price.hourly_minor = 3  # type: ignore[misc]

    def test_ingested_pricing_carries_plan(self) -> None:
        result = IngestedPricing(
            plan_id="cx22",
            prices=(
                IngestedPrice(location_id="fsn1", currency="EUR", hourly_minor=2, created=True),
            ),
        )
        assert result.plan_id == "cx22"
        assert result.prices[0].created is True
