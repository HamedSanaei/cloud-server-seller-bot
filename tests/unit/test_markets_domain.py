"""Market layer: markets and the config-driven provider listing.

The storefront groups providers into two customer-facing markets
(``iran`` / ``foreign``). Nothing here may know a concrete provider name, so
these tests drive everything through the catalog configuration.
"""

from __future__ import annotations

import pytest

from cloud_platform.modules.markets.domain import (
    MARKET_ORDER,
    Market,
    ProviderCatalog,
    ProviderListing,
    UnknownMarketError,
    parse_market,
)


class TestMarket:
    """The two markets and their message-catalog keys."""

    def test_values_are_the_storefront_identifiers(self) -> None:
        assert Market.IRAN.value == "iran"
        assert Market.FOREIGN.value == "foreign"

    def test_label_and_title_keys_are_derived(self) -> None:
        assert Market.IRAN.label_key == "store.market_iran"
        assert Market.FOREIGN.label_key == "store.market_foreign"
        assert Market.IRAN.title_key == "store.market_title_iran"

    def test_render_order_is_persian_first(self) -> None:
        assert MARKET_ORDER == (Market.IRAN, Market.FOREIGN)

    def test_parse_is_case_insensitive(self) -> None:
        assert parse_market(" Iran ") is Market.IRAN
        assert parse_market("FOREIGN") is Market.FOREIGN

    def test_parse_rejects_unknown_values(self) -> None:
        with pytest.raises(UnknownMarketError):
            parse_market("mars")


class TestProviderCatalog:
    """Configured metadata: market, display name and operator switch."""

    def test_market_lookup(self) -> None:
        catalog = ProviderCatalog(markets={"a": "iran", "b": "foreign"})
        assert catalog.market_of("a") is Market.IRAN
        assert catalog.market_of("b") is Market.FOREIGN

    def test_unconfigured_provider_has_no_market(self) -> None:
        assert ProviderCatalog().market_of("anything") is None

    def test_invalid_configured_market_is_treated_as_unconfigured(self) -> None:
        catalog = ProviderCatalog(markets={"a": "nonsense"})
        assert catalog.market_of("a") is None

    def test_display_name_falls_back_to_the_key(self) -> None:
        catalog = ProviderCatalog(display_names={"a": "Provider A"})
        assert catalog.display_name_of("a") == "Provider A"
        assert catalog.display_name_of("b") == "b"

    def test_enabled_defaults_to_true(self) -> None:
        catalog = ProviderCatalog(enabled={"a": False})
        assert catalog.is_enabled("a") is False
        assert catalog.is_enabled("b") is True

    def test_listing_requires_a_configured_market(self) -> None:
        catalog = ProviderCatalog(markets={"a": "iran"})
        assert catalog.listing("unknown", ordering_capable=True) is None

    def test_listing_carries_capability_and_config(self) -> None:
        catalog = ProviderCatalog(
            markets={"a": "iran"},
            display_names={"a": "Provider A"},
            enabled={"a": True},
        )
        listing = catalog.listing("a", ordering_capable=True)
        assert listing == ProviderListing(
            provider_key="a",
            market=Market.IRAN,
            display_name="Provider A",
            enabled=True,
            ordering_capable=True,
        )
        assert listing is not None and listing.buyable

    def test_listing_overrides_the_market_explicitly(self) -> None:
        catalog = ProviderCatalog(markets={"a": "iran"})
        listing = catalog.listing("a", ordering_capable=True, market=Market.FOREIGN)
        assert listing is not None and listing.market is Market.FOREIGN

    def test_not_buyable_without_ordering_capability(self) -> None:
        catalog = ProviderCatalog(markets={"a": "foreign"}, enabled={"a": True})
        listing = catalog.listing("a", ordering_capable=False)
        assert listing is not None
        assert listing.enabled is True
        assert listing.ordering_capable is False
        assert listing.buyable is False

    def test_not_buyable_when_disabled_by_the_operator(self) -> None:
        catalog = ProviderCatalog(markets={"a": "foreign"}, enabled={"a": False})
        listing = catalog.listing("a", ordering_capable=True)
        assert listing is not None and listing.buyable is False
