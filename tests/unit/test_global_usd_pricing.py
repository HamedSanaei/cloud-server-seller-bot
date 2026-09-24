"""P0 global catalog pricing invariants."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from cloud_platform.modules.fx.domain import FxReferenceQuote
from cloud_platform.modules.fx.service import ReferenceRateResolution
from cloud_platform.modules.offers.domain import PricingPolicy, SellableOffer
from cloud_platform.modules.offers.pricing import CatalogOfferPricer, OfferPricingError


class _Rates:
    def __init__(self, rate: Decimal) -> None:
        self.rate = rate
        self.calls: list[tuple[str, str]] = []

    async def get_rate(
        self, base: str, quote: str, *, allow_catalog_stale: bool = False
    ) -> ReferenceRateResolution:
        self.calls.append((base, quote))
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


def _offer() -> SellableOffer:
    return SellableOffer(
        id=uuid4(),
        provider_key="leaseweb",
        product_id="plan",
        location_id="eu-west-3",
        name="Plan",
        vcpu=1,
        ram_gb=1,
        disk_gb=10,
        traffic=None,
        provider_cost_minor=5,
        provider_cost_currency="EUR",
        selling_price_minor=0,
        selling_currency="EUR",
        billing_parameters={"provider_hourly_rate": "0.0453"},
        billing_model="hourly",
        provider_available=True,
        enabled=True,
        auto_priced=True,
    )


@pytest.mark.asyncio
async def test_exact_hourly_eur_usd_markup_and_ceiling() -> None:
    rates = _Rates(Decimal("1.17"))
    priced = await CatalogOfferPricer(rates, "USD").price_auto(
        _offer(), PricingPolicy(markup_percent=25)
    )

    # 0.0453 EUR * 1.17 * 1.25 = 0.06625125 USD; only the final customer
    # boundary is rounded, upward, to the USD minor unit.
    assert priced.selling_price_minor == 7
    assert priced.selling_currency == "USD"
    assert Decimal(str(priced.pricing_metadata["source_amount"])) == Decimal("0.0453")
    assert Decimal(str(priced.pricing_metadata["fx_rate"])) == Decimal("1.17")
    assert Decimal(str(priced.pricing_metadata["converted_cost_target_exact"])) == Decimal(
        "0.053001"
    )
    assert rates.calls == [("EUR", "USD")]


@pytest.mark.asyncio
async def test_identity_usd_does_not_call_global_fx() -> None:
    rates = _Rates(Decimal("999"))
    offer = replace(
        _offer(),
        provider_cost_currency="USD",
        selling_currency="USD",
        billing_parameters={"provider_hourly_rate": "0.0453"},
    )
    priced = await CatalogOfferPricer(rates, "USD").price_auto(
        offer, PricingPolicy(markup_percent=25)
    )
    assert priced.selling_price_minor == 6
    assert rates.calls == []
    assert priced.pricing_metadata["fx_provider"] == "identity"


def _manual_offer() -> SellableOffer:
    return SellableOffer(
        id=uuid4(),
        provider_key="leaseweb",
        product_id="plan",
        location_id="eu-west-3",
        name="Plan",
        vcpu=1,
        ram_gb=1,
        disk_gb=10,
        traffic=None,
        provider_cost_minor=809,
        provider_cost_currency="EUR",
        selling_price_minor=809,
        selling_currency="EUR",
        billing_parameters={"provider_monthly_rate": "8.09"},
        billing_model="prepaid_monthly_fixed",
        provider_available=True,
        enabled=True,
        auto_priced=False,
    )


@pytest.mark.asyncio
async def test_price_manual_converts_without_markup() -> None:
    rates = _Rates(Decimal("1.17"))
    priced = await CatalogOfferPricer(rates, "USD").price_manual(_manual_offer())

    # 8.09 EUR * 1.17 = 9.4653 USD -> ceiling 947. Markup math (25%) would be
    # 1184, so an exact 947 proves no second markup was applied.
    assert priced.selling_price_minor == 947
    assert priced.selling_currency == "USD"
    assert priced.pricing_metadata["pricing_mode"] == "manual"
    assert priced.pricing_metadata["price_source"] == "manual"
    assert str(priced.pricing_metadata["markup_percent"]) == "0"
    assert priced.pricing_metadata["original_selling_price_minor"] == 809
    assert priced.pricing_metadata["original_selling_currency"] == "EUR"
    assert rates.calls == [("EUR", "USD")]


class _StaleRates:
    """Catalog resolver serving one bounded stale quote, with a stale limit."""

    catalog_stale_limit = 3600

    def __init__(self) -> None:
        now = datetime.now(UTC)
        observed_at = now - timedelta(seconds=120)
        self.quote = FxReferenceQuote(
            base_currency="EUR",
            quote_currency="USD",
            rate=Decimal("1.17"),
            source="frankfurter",
            source_market="EUR/USD",
            provider_date=observed_at.date(),
            observed_at=observed_at,
            expires_at=observed_at + timedelta(seconds=60),
        )

    async def get_rate(
        self, base: str, quote: str, *, allow_catalog_stale: bool = False
    ) -> ReferenceRateResolution:
        assert (base, quote) == ("EUR", "USD")
        return ReferenceRateResolution(self.quote, stale=True)


class _StaleRatesWithoutLimit(_StaleRates):
    catalog_stale_limit = None  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_price_auto_stale_carries_bounded_validity() -> None:
    rates = _StaleRates()
    priced = await CatalogOfferPricer(rates, "USD").price_auto(
        _offer(), PricingPolicy(markup_percent=25)
    )

    assert priced.selling_price_minor == 7
    assert priced.pricing_metadata["fx_stale"] is True
    assert priced.pricing_metadata["fx_stale_limit_seconds"] == 3600
    observed_at = datetime.fromisoformat(str(priced.pricing_metadata["fx_observed_at"]))
    assert (
        priced.pricing_metadata["catalog_valid_until"]
        == (observed_at + timedelta(seconds=3600)).isoformat()
    )


@pytest.mark.asyncio
async def test_price_auto_stale_without_limit_fails() -> None:
    with pytest.raises(OfferPricingError):
        await CatalogOfferPricer(_StaleRatesWithoutLimit(), "USD").price_auto(
            _offer(), PricingPolicy(markup_percent=25)
        )
