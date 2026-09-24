"""Fail-closed contracts for USD catalog pricing and offer provenance.

Every case here is a way the platform must REFUSE to produce a customer price:
a missing exact provider rate, a rounded-only cross-currency cost, an
unbounded stale reference rate, a relabelled manual price, or metadata that no
longer matches the stored money columns. None of these may silently fall back
to a guess, a 1:1 conversion, or the provider's native currency.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from cloud_platform.modules.fx.domain import (
    FxPurpose,
    FxReferenceQuote,
    FxUnavailableError,
    major_to_minor,
)
from cloud_platform.modules.fx.service import ReferenceRateResolution
from cloud_platform.modules.offers.domain import (
    BILLING_MODEL_HOURLY,
    BILLING_MODEL_PREPAID_MONTHLY,
    OfferSpecUpdate,
    PricingPolicy,
    SellableOffer,
    has_valid_pricing_provenance,
    is_sellable_in_currency,
)
from cloud_platform.modules.offers.pricing import (
    CatalogOfferPricer,
    OfferPricingError,
)


def _reference_quote(
    base: str,
    quote: str,
    rate: str,
    *,
    source: str = "frankfurter",
    source_market: str | None = None,
    age_seconds: int = 0,
) -> FxReferenceQuote:
    observed_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return FxReferenceQuote(
        base_currency=base,
        quote_currency=quote,
        rate=Decimal(rate),
        source=source,
        source_market=source_market or f"{base}/{quote}",
        provider_date=observed_at.date(),
        observed_at=observed_at,
        expires_at=observed_at + timedelta(hours=1),
    )


class _Rates:
    """Scripted exact-rate resolver used as the catalog pricing port."""

    def __init__(
        self,
        rate: str = "1.10",
        *,
        stale: bool = False,
        catalog_stale_limit: int | None = None,
        base: str | None = None,
        source: str = "frankfurter",
        source_market: str | None = None,
        age_seconds: int = 0,
    ) -> None:
        self.rate = rate
        self.stale = stale
        self.base = base
        self.source = source
        self.source_market = source_market
        self.age_seconds = age_seconds
        if catalog_stale_limit is not None:
            self.catalog_stale_limit = catalog_stale_limit
        self.calls: list[tuple[str, str]] = []

    async def get_rate(
        self, base: str, quote: str, *, allow_catalog_stale: bool = False
    ) -> ReferenceRateResolution:
        self.calls.append((base, quote))
        return ReferenceRateResolution(
            _reference_quote(
                self.base or base,
                quote,
                self.rate,
                source=self.source,
                source_market=self.source_market,
                age_seconds=self.age_seconds,
            ),
            stale=self.stale,
        )


def _cost_minor_from(exact_rate: object, currency: str) -> int:
    """Durable minor-unit projection of an exact provider rate (or 0)."""
    if not isinstance(exact_rate, str):
        return 0
    try:
        projected = major_to_minor(Decimal(exact_rate), currency, FxPurpose.DISPLAY)
    except (InvalidOperation, ValueError, ArithmeticError):
        return 0
    return projected if projected > 0 else 0


def _offer(
    *,
    cost_currency: str = "EUR",
    cost_minor: int | None = None,
    exact_rate_key: str = "provider_monthly_rate",
    exact_rate: object = "10.00",
    billing_model: str = BILLING_MODEL_PREPAID_MONTHLY,
    selling_minor: int = 0,
    selling_currency: str | None = None,
    auto_priced: bool = True,
    pricing_metadata: dict[str, object] | None = None,
) -> SellableOffer:
    if cost_minor is None:
        cost_minor = _cost_minor_from(exact_rate, cost_currency)
    parameters: dict[str, object] = {exact_rate_key: exact_rate}
    return SellableOffer(
        id=uuid4(),
        provider_key="leaseweb",
        product_id="PLAN",
        location_id="LOC",
        name="Plan",
        vcpu=1,
        ram_gb=2,
        disk_gb=20,
        traffic=None,
        provider_cost_minor=cost_minor,
        provider_cost_currency=cost_currency,
        selling_price_minor=selling_minor,
        selling_currency=selling_currency or cost_currency,
        billing_parameters=parameters,
        billing_model=billing_model,
        provider_available=True,
        enabled=True,
        auto_priced=auto_priced,
        pricing_metadata=dict(pricing_metadata or {}),
    )


def _hourly_offer(exact_rate: object = "0.0453", *, cost_minor: int | None = None) -> SellableOffer:
    return _offer(
        cost_currency="EUR",
        cost_minor=cost_minor,
        exact_rate_key="provider_hourly_rate",
        exact_rate=exact_rate,
        billing_model=BILLING_MODEL_HOURLY,
    )


class TestExactProviderRateIsMandatory:
    """A cross-currency price may never be derived from rounded minor units."""

    async def test_hourly_offer_without_exact_rate_is_refused(self) -> None:
        offer = _hourly_offer(exact_rate=None)

        with pytest.raises(OfferPricingError, match="requires exact provider_hourly_rate"):
            await CatalogOfferPricer(_Rates(), "USD").price_auto(offer, PricingPolicy())

    async def test_monthly_offer_without_exact_rate_is_refused(self) -> None:
        offer = _offer(exact_rate_key="provider_monthly_rate", exact_rate=None)

        with pytest.raises(OfferPricingError, match="requires exact provider_monthly_rate"):
            await CatalogOfferPricer(_Rates(), "USD").price_auto(offer, PricingPolicy())

    @pytest.mark.parametrize("bad_rate", [0.0453, True])
    async def test_float_or_boolean_hourly_rate_is_refused(self, bad_rate: object) -> None:
        offer = _hourly_offer(exact_rate=bad_rate)

        with pytest.raises(OfferPricingError, match="must be Decimal text"):
            await CatalogOfferPricer(_Rates(), "USD").price_auto(offer, PricingPolicy())

    @pytest.mark.parametrize("bad_rate", ["not-a-rate", "", "1e"])
    async def test_unparsable_hourly_rate_is_refused(self, bad_rate: str) -> None:
        offer = _hourly_offer(exact_rate=bad_rate)

        with pytest.raises(OfferPricingError, match="not valid Decimal text"):
            await CatalogOfferPricer(_Rates(), "USD").price_auto(offer, PricingPolicy())

    @pytest.mark.parametrize("bad_rate", ["0", "-1", "NaN", "Infinity"])
    async def test_non_positive_or_non_finite_hourly_rate_is_refused(self, bad_rate: str) -> None:
        offer = _hourly_offer(exact_rate=bad_rate)

        with pytest.raises(OfferPricingError, match="positive and finite"):
            await CatalogOfferPricer(_Rates(), "USD").price_auto(offer, PricingPolicy())

    async def test_float_monthly_rate_is_refused(self) -> None:
        offer = _offer(exact_rate=10.0)

        with pytest.raises(OfferPricingError, match="must be Decimal text"):
            await CatalogOfferPricer(_Rates(), "USD").price_auto(offer, PricingPolicy())

    async def test_unparsable_monthly_rate_is_refused(self) -> None:
        offer = _offer(exact_rate="n/a")

        with pytest.raises(OfferPricingError, match="not valid Decimal text"):
            await CatalogOfferPricer(_Rates(), "USD").price_auto(offer, PricingPolicy())

    async def test_exact_rate_must_match_the_stored_minor_observation(self) -> None:
        # 10.00 EUR would round to 1000 minor; 999 is a corrupt/mismatched row.
        offer = _offer(cost_minor=999, exact_rate="10.00")

        with pytest.raises(OfferPricingError, match="does not match the provider_cost_minor"):
            await CatalogOfferPricer(_Rates(), "USD").price_auto(offer, PricingPolicy())


class TestFxResolutionFailClosed:
    async def test_missing_resolver_refuses_a_foreign_pair(self) -> None:
        with pytest.raises(FxUnavailableError, match="FX unavailable for EUR->USD"):
            await CatalogOfferPricer(None, "USD").price_auto(_offer(), PricingPolicy())

    async def test_usd_to_usd_identity_needs_no_resolver_and_no_network(self) -> None:
        priced = await CatalogOfferPricer(None, "USD").price_auto(
            _offer(cost_currency="USD", exact_rate="10.00"), PricingPolicy(markup_percent=10)
        )

        assert priced.selling_price_minor == 1100
        assert priced.selling_currency == "USD"
        assert priced.pricing_metadata["fx_provider"] == "identity"
        assert priced.pricing_metadata["fx_rate"] == "1"

    @pytest.mark.parametrize(
        "rates",
        [
            _Rates(base="GBP"),
            _Rates(source="abantether"),
            _Rates(source_market="EUR/JPY"),
        ],
    )
    async def test_invalid_resolver_provenance_is_refused(self, rates: _Rates) -> None:
        with pytest.raises(OfferPricingError):
            await CatalogOfferPricer(rates, "USD").price_auto(_offer(), PricingPolicy())

    async def test_prefetched_rate_is_used_without_a_resolver(self) -> None:
        resolution = ReferenceRateResolution(_reference_quote("EUR", "USD", "1.10"))
        pricer = CatalogOfferPricer(None, "USD", prefetched_rates={("EUR", "USD"): resolution})

        priced = await pricer.price_auto(_offer(), PricingPolicy())

        assert priced.selling_price_minor == 1100
        assert priced.selling_currency == "USD"

    async def test_stale_rate_without_a_bounded_limit_is_refused(self) -> None:
        rates = _Rates(stale=True, age_seconds=120)

        with pytest.raises(OfferPricingError, match="no bounded validity limit"):
            await CatalogOfferPricer(rates, "USD").price_auto(_offer(), PricingPolicy())

    async def test_stale_rate_with_a_bounded_limit_is_audited(self) -> None:
        rates = _Rates(stale=True, age_seconds=120, catalog_stale_limit=3600)

        priced = await CatalogOfferPricer(rates, "USD").price_auto(_offer(), PricingPolicy())

        assert priced.pricing_metadata["fx_stale"] is True
        assert priced.pricing_metadata["fx_stale_limit_seconds"] == 3600
        assert "catalog_valid_until" in priced.pricing_metadata


class TestAutomaticPricingPolicyGuard:
    async def test_unvalidated_policy_mode_is_refused(self) -> None:
        # Defense in depth: a policy object that bypassed PricingPolicy's own
        # constructor validation must still never reach the money path.
        rogue = SimpleNamespace(mode="manual", markup_percent=0)

        with pytest.raises(OfferPricingError, match="non-negative markup"):
            await CatalogOfferPricer(_Rates(), "USD").price_auto(_offer(), rogue)  # type: ignore[arg-type]


class TestManualNormalizationNeverRelabels:
    async def test_manual_eur_price_is_converted_and_audited(self) -> None:
        offer = _offer(
            cost_currency="EUR",
            selling_minor=809,
            selling_currency="EUR",
            auto_priced=False,
        )

        priced = await CatalogOfferPricer(_Rates("1.25"), "USD").price_manual(offer)

        # 8.09 EUR * 1.25 = 10.1125 USD -> ceiling to 1012 minor (NOT 809).
        assert priced.selling_price_minor == 1012
        assert priced.selling_currency == "USD"
        metadata = priced.pricing_metadata
        assert metadata["original_selling_currency"] == "EUR"
        assert metadata["original_selling_price_minor"] == 809
        assert metadata["markup_percent"] == "0"
        assert metadata["price_source"] == "manual"
        # Provider-native cost keeps its own currency; it is never relabelled.
        assert metadata["provider_cost_currency"] == "EUR"
        assert metadata["target_currency"] == "USD"

    async def test_manual_usd_price_needs_no_conversion(self) -> None:
        offer = _offer(
            cost_currency="USD",
            exact_rate="10.00",
            selling_minor=999,
            selling_currency="USD",
            auto_priced=False,
        )

        priced = await CatalogOfferPricer(None, "USD").price_manual(offer)

        assert priced.selling_price_minor == 999
        assert priced.pricing_metadata["fx_provider"] == "identity"

    async def test_manual_offer_without_a_price_is_refused(self) -> None:
        offer = _offer(
            cost_currency="EUR", selling_minor=0, selling_currency="EUR", auto_priced=False
        )

        with pytest.raises(OfferPricingError, match="manual selling price must be positive"):
            await CatalogOfferPricer(_Rates(), "USD").price_manual(offer)


class TestProvenanceRejections:
    """Rows that cannot prove their canonical conversion are never sellable."""

    @staticmethod
    async def _priced(rate: str = "1.17") -> SellableOffer:
        base = _offer(cost_currency="EUR", exact_rate="20.00")
        pricer = CatalogOfferPricer(_Rates(rate), "USD")
        # price_auto is async; drive it explicitly in the calling coroutine.
        priced = await pricer.price_auto(base, PricingPolicy(markup_percent=25))
        return replace(
            base,
            selling_price_minor=priced.selling_price_minor,
            selling_currency=priced.selling_currency,
            pricing_metadata=dict(priced.pricing_metadata),
        )

    async def test_priced_row_is_proven(self) -> None:
        offer = await self._priced()

        assert has_valid_pricing_provenance(offer, "USD") is True
        assert is_sellable_in_currency(offer, "USD") is True

    async def test_row_without_any_currency_cannot_be_proven(self) -> None:
        offer = await self._priced()
        legacy = replace(offer, selling_currency="", legacy_invalid=True)

        assert has_valid_pricing_provenance(legacy, "USD") is False

    async def test_manual_same_currency_row_needs_the_exact_provider_rate(self) -> None:
        base = _offer(
            cost_currency="EUR",
            exact_rate="20.00",
            selling_minor=2500,
            selling_currency="EUR",
            auto_priced=False,
        )

        without_rate = replace(base, billing_parameters={})
        assert has_valid_pricing_provenance(without_rate, "EUR") is False

        with_rate = replace(base, pricing_metadata={})
        assert has_valid_pricing_provenance(with_rate, "EUR") is True

    async def test_domestic_row_in_a_foreign_currency_is_not_proven(self) -> None:
        offer = _offer(
            cost_currency="IRT",
            cost_minor=1_000_000,
            exact_rate="1000000",
            selling_minor=1200,
            selling_currency="USD",
        )

        assert has_valid_pricing_provenance(offer, "IRT") is False

    async def test_foreign_row_left_in_its_native_currency_is_not_proven(self) -> None:
        offer = await self._priced()
        native = replace(offer, selling_currency="EUR")

        assert has_valid_pricing_provenance(native, "USD") is False

    @pytest.mark.parametrize(
        "mutation",
        [
            {"pricing_schema_version": "2"},
            {"target_currency": "EUR"},
            {"provider_cost_currency": "GBP"},
            {"source_currency": "GBP"},
            {"provider_cost_minor": "1"},
            {"final_selling_price_minor": 1},
            {"fx_rate": "9.99"},
            {"converted_cost_target_exact": "1"},
            {"markup_percent": "-1"},
            {"pricing_mode": "guess"},
            {"rounding": "ROUND_DOWN"},
            {"fx_purpose": "display"},
            {"fx_stale": "yes"},
            {"fx_provider": ""},
            {"fx_source_market": ""},
            {"fx_rate": "not-a-number"},
            {"fx_provider_date": "not-a-date"},
            {"catalog_valid_until": "not-a-date"},
        ],
    )
    async def test_damaged_pricing_metadata_loses_provenance(
        self, mutation: dict[str, object]
    ) -> None:
        offer = await self._priced()
        damaged = replace(offer, pricing_metadata={**offer.pricing_metadata, **mutation})

        assert has_valid_pricing_provenance(damaged, "USD") is False
        assert is_sellable_in_currency(damaged, "USD") is False

    async def test_stale_metadata_must_declare_its_bounded_validity(self) -> None:
        offer = await self._priced()
        metadata = {**offer.pricing_metadata, "fx_stale": True}
        without_limit = replace(offer, pricing_metadata=metadata)

        assert has_valid_pricing_provenance(without_limit, "USD") is False

        observed_at = datetime.now(UTC) - timedelta(seconds=120)
        bounded = replace(
            offer,
            pricing_metadata={
                **metadata,
                "fx_observed_at": observed_at.isoformat(),
                "fx_stale_limit_seconds": 3600,
                "catalog_valid_until": (observed_at + timedelta(seconds=3600)).isoformat(),
            },
        )
        assert has_valid_pricing_provenance(bounded, "USD") is True

        overlong = replace(
            offer,
            pricing_metadata={
                **bounded.pricing_metadata,
                "catalog_valid_until": (observed_at + timedelta(days=365)).isoformat(),
            },
        )
        assert has_valid_pricing_provenance(overlong, "USD") is False
        assert (
            has_valid_pricing_provenance(overlong, "USD", catalog_stale_limit_seconds=60) is False
        )

    async def test_identity_metadata_must_declare_the_identity_provider(self) -> None:
        base = _offer(cost_currency="USD", exact_rate="10.00")
        pricer = CatalogOfferPricer(None, "USD")
        priced = await pricer.price_auto(base, PricingPolicy(markup_percent=10))
        auto = replace(
            base,
            selling_price_minor=priced.selling_price_minor,
            selling_currency="USD",
            pricing_metadata=dict(priced.pricing_metadata),
        )

        assert has_valid_pricing_provenance(auto, "USD") is True

        mislabelled = replace(
            auto,
            pricing_metadata={**auto.pricing_metadata, "fx_provider": "frankfurter"},
        )
        assert has_valid_pricing_provenance(mislabelled, "USD") is False

    async def test_foreign_metadata_must_declare_frankfurter(self) -> None:
        offer = await self._priced()
        mislabelled = replace(
            offer, pricing_metadata={**offer.pricing_metadata, "fx_provider": "identity"}
        )

        assert has_valid_pricing_provenance(mislabelled, "USD") is False


class TestOfferSpecUpdateValidation:
    """Catalog sync observations must fail closed instead of writing bad rows."""

    @staticmethod
    def _spec(**overrides: Any) -> dict[str, Any]:
        spec: dict[str, Any] = {
            "name": "Plan",
            "vcpu": 1,
            "ram_gb": 2,
            "disk_gb": 20,
            "traffic": None,
            "provider_cost_minor": 1000,
            "provider_cost_currency": "EUR",
            "billing_parameters": {"provider_monthly_rate": "10.00"},
        }
        spec.update(overrides)
        return spec

    @pytest.mark.parametrize(
        ("field", "value"),
        [("vcpu", -1), ("vcpu", True), ("ram_gb", 2**31), ("disk_gb", "20")],
    )
    def test_non_postgres_integer_spec_values_are_refused(self, field: str, value: object) -> None:
        with pytest.raises(ValueError, match="non-negative PostgreSQL integer"):
            OfferSpecUpdate(**self._spec(**{field: value}))

    @pytest.mark.parametrize("cost", [-1, True, 9_223_372_036_854_775_808])
    def test_invalid_provider_cost_is_refused(self, cost: object) -> None:
        with pytest.raises(ValueError, match="provider_cost_minor"):
            OfferSpecUpdate(**self._spec(provider_cost_minor=cost))

    def test_unsupported_billing_model_is_refused(self) -> None:
        with pytest.raises(ValueError, match="billing_model is not supported"):
            OfferSpecUpdate(**self._spec(billing_model="per_second"))

    def test_blank_provider_account_id_is_refused(self) -> None:
        with pytest.raises(ValueError, match="provider_account_id"):
            OfferSpecUpdate(**self._spec(provider_account_id="  "))

    def test_non_mapping_payload_fields_are_refused(self) -> None:
        with pytest.raises(ValueError, match="billing_parameters must be a mapping"):
            OfferSpecUpdate(**self._spec(billing_parameters=["not", "a", "mapping"]))

        with pytest.raises(ValueError, match="technical_metadata must be a mapping"):
            OfferSpecUpdate(**self._spec(technical_metadata="facts"))

    def test_unaudited_provider_cost_currency_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not an audited currency"):
            OfferSpecUpdate(**self._spec(provider_cost_currency="XYZ"))

    def test_currency_and_account_are_normalized(self) -> None:
        spec = OfferSpecUpdate(
            **self._spec(provider_cost_currency=" eur ", provider_account_id=" a ")
        )

        assert spec.provider_cost_currency == "EUR"
        assert spec.provider_account_id == "a"


class TestPricingPolicyValidation:
    def test_unknown_mode_is_refused(self) -> None:
        with pytest.raises(ValueError, match="mode must be 'markup'"):
            PricingPolicy(mode="formula")

    @pytest.mark.parametrize("percent", [-1, True, "10"])
    def test_invalid_markup_is_refused(self, percent: object) -> None:
        with pytest.raises(ValueError, match="markup_percent must be a non-negative integer"):
            PricingPolicy(markup_percent=percent)  # type: ignore[arg-type]
