"""Provenance and visibility edge branches of the sellable-offer domain.

Foreign offers are priced through the real CatalogOfferPricer with
deterministic scripted FX rates (Decimal-only, no network), so every
provenance assertion runs against production-shaped pricing metadata.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from cloud_platform.modules.fx.domain import FxPurpose, FxReferenceQuote, major_to_minor
from cloud_platform.modules.fx.service import ReferenceRateResolution
from cloud_platform.modules.offers.domain import (
    GATE_CURRENCY,
    GATE_DEPRECATED,
    GATE_OPERATOR_DISABLED,
    GATE_PRICING_PENDING,
    GATE_PRICING_PROVENANCE,
    PricingPolicy,
    SellableOffer,
    blocking_gate,
    has_valid_pricing_provenance,
    is_sellable_in_currency,
    markup_unit_price,
    requires_currency_normalization,
    visibility_summary,
)
from cloud_platform.modules.offers.pricing import CatalogOfferPricer


class _Rates:
    """Deterministic scripted FX rates (no network, no float)."""

    def __init__(self, rate: Decimal) -> None:
        self.rate = rate

    async def get_rate(
        self, base: str, quote: str, *, allow_catalog_stale: bool = False
    ) -> ReferenceRateResolution:
        now = datetime.now(UTC)
        return ReferenceRateResolution(
            FxReferenceQuote(
                base_currency=base,
                quote_currency=quote,
                rate=self.rate,
                source="frankfurter",
                source_market=f"{base}/{quote}",
                provider_date=now.date(),
                observed_at=now,
                expires_at=now + timedelta(hours=1),
            ),
            stale=False,
        )


def _monthly_base(
    *,
    cost_currency: str,
    rate_text: str,
    selling_minor: int = 0,
    selling_currency: str | None = None,
) -> SellableOffer:
    cost_minor = major_to_minor(Decimal(rate_text), cost_currency, FxPurpose.DISPLAY)
    return SellableOffer(
        id=uuid4(),
        provider_key="leaseweb",
        product_id="PLAN",
        location_id="LOC",
        name="Plan",
        vcpu=2,
        ram_gb=4,
        disk_gb=50,
        traffic=None,
        provider_cost_minor=cost_minor,
        provider_cost_currency=cost_currency,
        selling_price_minor=selling_minor,
        selling_currency=selling_currency or cost_currency,
        billing_parameters={"provider_monthly_rate": rate_text},
        provider_available=True,
        enabled=True,
        created_at=datetime.now(UTC),
    )


async def _priced_usd(base: SellableOffer, rate: str = "1.17") -> SellableOffer:
    priced = await CatalogOfferPricer(_Rates(Decimal(rate)), "USD").price_auto(
        base, PricingPolicy(mode="markup", markup_percent=25)
    )
    return replace(
        base,
        selling_price_minor=priced.selling_price_minor,
        selling_currency=priced.selling_currency,
        pricing_metadata=dict(priced.pricing_metadata),
    )


@pytest.mark.parametrize(
    ("currency", "rate_text"),
    [
        ("GBP", "10.00"),
        ("SGD", "20.50"),
        ("AUD", "5.25"),
        ("CAD", "7.10"),
        ("KRW", "1300"),
    ],
)
async def test_foreign_currencies_carry_valid_provenance(currency: str, rate_text: str) -> None:
    offer = await _priced_usd(_monthly_base(cost_currency=currency, rate_text=rate_text))

    assert offer.selling_currency == "USD"
    assert has_valid_pricing_provenance(offer, "USD") is True
    assert is_sellable_in_currency(offer, "USD") is True
    assert blocking_gate(offer, "USD") is None


class TestExponentZeroCurrencies:
    async def test_jpy_same_currency_row_is_proven(self) -> None:
        base = _monthly_base(cost_currency="JPY", rate_text="691")
        priced = await CatalogOfferPricer(None, "JPY").price_auto(
            base, PricingPolicy(mode="markup", markup_percent=0)
        )
        offer = replace(
            base,
            selling_price_minor=priced.selling_price_minor,
            selling_currency=priced.selling_currency,
            pricing_metadata=dict(priced.pricing_metadata),
        )

        # Exponent-0: minor units ARE yen, so the price is 691, not 69100.
        assert major_to_minor(Decimal("691"), "JPY", FxPurpose.DISPLAY) == 691
        assert offer.selling_price_minor == 691
        assert has_valid_pricing_provenance(offer, "JPY") is True
        assert is_sellable_in_currency(offer, "JPY") is True

    async def test_jpy_treated_as_cents_fails_provenance(self) -> None:
        base = _monthly_base(cost_currency="JPY", rate_text="691")
        priced = await CatalogOfferPricer(None, "JPY").price_auto(
            base, PricingPolicy(mode="markup", markup_percent=0)
        )
        confused = replace(
            base,
            provider_cost_minor=69100,  # cents-style misreading of ¥691
            selling_price_minor=priced.selling_price_minor,
            selling_currency=priced.selling_currency,
            pricing_metadata=dict(priced.pricing_metadata),
        )

        assert has_valid_pricing_provenance(confused, "JPY") is False
        assert is_sellable_in_currency(confused, "JPY") is False


class TestProvenanceGates:
    async def test_legacy_price_without_metadata_has_no_provenance(self) -> None:
        offer = _monthly_base(
            cost_currency="EUR", rate_text="20.00", selling_minor=2500, selling_currency="EUR"
        )

        assert has_valid_pricing_provenance(offer, "EUR") is False
        assert is_sellable_in_currency(offer, "EUR") is False
        assert blocking_gate(offer, "EUR") == GATE_PRICING_PROVENANCE

    async def test_deprecated_operator_disabled_pending_gates(self) -> None:
        priced = await _priced_usd(_monthly_base(cost_currency="EUR", rate_text="20.00"))

        deprecated = replace(priced, technical_metadata={"deprecated": True})
        assert deprecated.sellable is False
        assert blocking_gate(deprecated, "USD") == GATE_DEPRECATED

        disabled = replace(priced, operator_disabled=True)
        assert disabled.sellable is False
        assert blocking_gate(disabled, "USD") == GATE_OPERATOR_DISABLED

        pending = replace(
            priced, pricing_metadata={**dict(priced.pricing_metadata), "fx_repricing_pending": True}
        )
        assert pending.sellable is False
        assert blocking_gate(pending, "USD") == GATE_PRICING_PENDING

    async def test_visibility_summary_counts_every_gate(self) -> None:
        priced = await _priced_usd(_monthly_base(cost_currency="EUR", rate_text="20.00"))
        summary = visibility_summary(
            [
                priced,
                replace(priced, technical_metadata={"deprecated": True}),
                replace(priced, operator_disabled=True),
                replace(
                    priced,
                    pricing_metadata={
                        **dict(priced.pricing_metadata),
                        "fx_repricing_pending": True,
                    },
                ),
                replace(priced, selling_price_minor=0),
            ],
            "USD",
        )

        assert summary["sellable"] == 1
        assert summary[GATE_DEPRECATED] == 1
        assert summary[GATE_OPERATOR_DISABLED] == 1
        assert summary[GATE_PRICING_PENDING] == 1
        assert summary["unpriced"] == 1


class TestCurrencyNormalization:
    def test_domestic_row_needs_no_normalization(self) -> None:
        offer = _monthly_base(
            cost_currency="IRT", rate_text="1000", selling_minor=1200, selling_currency="IRT"
        )

        assert requires_currency_normalization(offer, "USD") is False

    async def test_foreign_row_in_catalog_currency_is_canonical(self) -> None:
        offer = await _priced_usd(_monthly_base(cost_currency="EUR", rate_text="20.00"))

        assert requires_currency_normalization(offer, "USD") is False

    def test_foreign_row_in_native_currency_violates_catalog(self) -> None:
        offer = _monthly_base(
            cost_currency="EUR", rate_text="20.00", selling_minor=2500, selling_currency="EUR"
        )

        assert requires_currency_normalization(offer, "USD") is True
        assert blocking_gate(offer, "USD") == GATE_CURRENCY


class TestMarkupBounds:
    def test_exact_and_rounded_up_prices(self) -> None:
        assert markup_unit_price(100, 25) == 125
        assert markup_unit_price(100, 0) == 100
        # 1 minor at 1%: 1.01 rounds UP so a non-zero cost is never sold below cost.
        assert markup_unit_price(1, 1) == 2

    def test_overflow_above_int64_fails(self) -> None:
        with pytest.raises(ValueError, match="int64"):
            markup_unit_price(9_000_000_000_000_000_000, 100)

    @pytest.mark.parametrize("kwargs", [{"cost": 0}, {"cost": -5}, {"cost": True}])
    def test_non_positive_or_bool_cost_rejected(self, kwargs: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="positive minor"):
            markup_unit_price(kwargs["cost"], 10)

    def test_negative_markup_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            markup_unit_price(100, -1)
