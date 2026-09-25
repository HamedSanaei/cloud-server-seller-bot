"""Leaseweb Public Cloud official response schema (P0 production fix).

The adapter previously parsed ``pricePerHour``/per-item ``currency`` and
scalar resources — none of which the official ``/publicCloud/v1/``
responses contain — so every real instance type was dropped and hourly
offers stayed at zero while the doctor reported "types present but none
carry a usable hourly price/currency".

Proven here against a fixture shaped EXACTLY like the official API:

- one valid ``CloudInstanceType`` per priced entry (hourly from
  ``prices.hourly``, currency from envelope ``_metadata.currency``);
- the monthly provider price is never substituted for the hourly rate;
- nested resources (cpu/memory/network ``{value, unit}``), ``minDiskSize``
  and ``storageTypes`` normalize without invention;
- sub-cent rates keep their exact decimal text (no float anywhere);
- missing envelope currency fails closed for pricing;
- the documented ``lsw.*`` naming taxonomy classifies families
  (adapter-local; the storefront never sees these prefixes);
- the production-shaped response flows end-to-end (parse -> offer with
  account/provenance -> 25% pricing -> publish -> sellable -> families).
"""

from __future__ import annotations

from typing import Any
from unittest import mock
from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.offers.domain import SellableOffer
from cloud_platform.providers.leaseweb.cloud import (
    LeasewebHourlyCloudProvider,
    classify_instance_family,
)


def _official_types_payload() -> dict[str, Any]:
    """Official ``/publicCloud/v1/instanceTypes`` shape (verbatim layout)."""
    return {
        "instanceTypes": [
            {
                "name": "lsw.c3.large",
                "resources": {
                    "cpu": {"value": 2, "unit": "vCPU"},
                    "memory": {"value": 3, "unit": "GiB"},
                    "publicNetworkSpeed": {"value": 1, "unit": "Gbps"},
                    "privateNetworkSpeed": {"value": 100, "unit": "Mbps"},
                },
                "prices": {"hourly": "0.0395", "monthly": "26.0200"},
                "storageTypes": ["CENTRAL"],
                "minDiskSize": 5,
            }
        ],
        "_metadata": {"currency": "EUR", "currencySymbol": "€"},
    }


def _regions_payload() -> dict[str, Any]:
    return {
        "regions": [
            {
                "name": "eu-west-3",
                "country": "DE",
                "displayName": "Frankfurt",
                "city": "Frankfurt",
            }
        ]
    }


def _provider(payloads: dict[str, Any]) -> LeasewebHourlyCloudProvider:
    provider = LeasewebHourlyCloudProvider.__new__(LeasewebHourlyCloudProvider)

    class _Transport:
        async def request(
            self, method: str, path: str, params: dict[str, Any] | None = None, **kwargs: Any
        ) -> Any:
            assert method == "GET", "probe/sync reads only"
            return payloads[path]

        async def aclose(self) -> None:
            return None

    provider._transport = _Transport()
    return provider


class TestOfficialInstanceType:
    async def test_official_payload_produces_one_valid_type(self) -> None:
        provider = _provider({"/publicCloud/v1/instanceTypes": _official_types_payload()})
        read = await provider.read_instance_types("eu-west-3")
        assert read.raw_items == 1
        assert read.priced_items == 1
        assert read.currency == "EUR"
        assert read.currency_symbol == "€"
        (item,) = read.types
        # 1-3: identity, provider currency, hourly sourced from prices.hourly.
        assert item.id == "lsw.c3.large"
        assert item.currency == "EUR"
        assert item.hourly_rate_exact == "0.0395"
        assert item.hourly_cost_minor == 4  # 3.95 cents, HALF_UP per convention
        # 4: the monthly provider price is reference only, never the rate.
        assert item.monthly_cost_minor == 2602
        assert item.hourly_cost_minor != item.monthly_cost_minor
        # 5-10: nested resources without invention.
        assert item.vcpu == 2
        assert item.ram_gb == 3
        assert item.memory_gb_exact == "3 GiB"
        assert item.disk_gb == 5
        assert item.storage_type == "CENTRAL"
        assert item.storage_types == ("CENTRAL",)
        assert item.network_public == "1 Gbps"
        assert item.network_private == "100 Mbps"
        assert item.architecture is None
        assert item.ipv4 is None
        assert item.ipv6 is None
        # 11: no float money anywhere.
        assert isinstance(item.hourly_cost_minor, int)
        assert isinstance(item.hourly_rate_exact, str)

    async def test_missing_envelope_currency_fails_closed(self) -> None:
        provider = _provider({"/publicCloud/v1/instanceTypes": {"instanceTypes": []}})
        payload = _official_types_payload()
        del payload["_metadata"]
        provider = _provider({"/publicCloud/v1/instanceTypes": payload})
        read = await provider.read_instance_types("eu-west-3")
        assert read.raw_items == 1
        assert read.priced_items == 1  # a price IS present ...
        assert read.currency is None
        assert read.types == ()  # ... but nothing is sellable without currency

    async def test_invalid_envelope_currency_fails_closed(self) -> None:
        payload = _official_types_payload()
        payload["_metadata"] = {"currency": "EURO"}
        provider = _provider({"/publicCloud/v1/instanceTypes": payload})
        read = await provider.read_instance_types("eu-west-3")
        assert read.currency is None
        assert read.types == ()

    async def test_malformed_hourly_price_is_not_sellable(self) -> None:
        for bad in ("n/a", "", "-0.5", "0", None):
            payload = _official_types_payload()
            payload["instanceTypes"][0]["prices"]["hourly"] = bad
            provider = _provider({"/publicCloud/v1/instanceTypes": payload})
            read = await provider.read_instance_types("eu-west-3")
            assert read.types == (), bad

    async def test_multiple_types_and_fractional_memory(self) -> None:
        payload = _official_types_payload()
        payload["instanceTypes"].append(
            {
                "name": "lsw.r3.large",
                "resources": {
                    "cpu": {"value": 2, "unit": "vCPU"},
                    "memory": {"value": 15.25, "unit": "GiB"},
                    "publicNetworkSpeed": {"value": 1, "unit": "Gbps"},
                    "privateNetworkSpeed": {"value": 0.1, "unit": "Gbps"},
                },
                "prices": {"hourly": "0.0789", "monthly": "51.9000"},
                "storageTypes": ["CENTRAL"],
                "minDiskSize": 5,
            }
        )
        provider = _provider({"/publicCloud/v1/instanceTypes": payload})
        read = await provider.read_instance_types("eu-west-3")
        assert [item.id for item in read.types] == ["lsw.c3.large", "lsw.r3.large"]
        memory_box = read.types[1]
        assert memory_box.ram_gb == 15  # legacy coarse int field stays integer
        assert memory_box.memory_gb_exact == "15.25 GiB"  # exact value preserved
        assert memory_box.network_private == "0.1 Gbps"
        assert (memory_box.family_key, memory_box.family_name) == (
            "memory",
            "Memory Optimized",
        )


class TestOfficialFamilyTaxonomy:
    """Documented ``lsw.*`` naming (KB: instance naming table)."""

    def test_compute_memory_general_gpu_prefixes(self) -> None:
        assert classify_instance_family({"name": "lsw.c3.large"}) == (
            "compute",
            "Compute Optimized",
        )
        assert classify_instance_family({"name": "lsw.m4.large"}) == (
            "general",
            "General Purpose",
        )
        assert classify_instance_family({"name": "lsw.r5.xlarge"}) == (
            "memory",
            "Memory Optimized",
        )
        assert classify_instance_family({"name": "LSW.G6.XLARGE"}) == ("gpu", "GPU Optimized")
        assert classify_instance_family({"name": "lsw.gr6.4xlarge"}) == (
            "gpu",
            "GPU Optimized",
        )

    def test_lookalike_names_stay_other(self) -> None:
        # The vintage letter is always followed by a platform digit, so a
        # bare word starting with lsw.m must not classify as General Purpose.
        assert classify_instance_family({"name": "lsw.mini"}) == ("other", "Other")
        assert classify_instance_family({}) == ("other", "Other")

    def test_explicit_provider_category_still_wins(self) -> None:
        assert classify_instance_family({"name": "lsw.c3.large", "family": "Custom Line"}) == (
            "custom-line",
            "Custom Line",
        )


class _BookOffersRepo:
    """Minimal price-book double storing real domain offers."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], SellableOffer] = {}
        self.upserts: list[dict[str, Any]] = []

    async def upsert_from_provider(self, **kwargs: Any) -> Any:
        from cloud_platform.modules.offers.domain import OfferSpecUpdate

        update: OfferSpecUpdate = kwargs["update"]
        key = (kwargs["provider_key"], kwargs["product_id"], kwargs["location_id"])
        self.upserts.append(dict(kwargs))
        existing = self.rows.get(key)
        self.rows[key] = SellableOffer(
            id=existing.id if existing is not None else uuid4(),
            provider_key=kwargs["provider_key"],
            product_id=kwargs["product_id"],
            location_id=kwargs["location_id"],
            name=update.name,
            vcpu=update.vcpu,
            ram_gb=update.ram_gb,
            disk_gb=update.disk_gb,
            traffic=update.traffic,
            provider_cost_minor=update.provider_cost_minor,
            provider_cost_currency=update.provider_cost_currency,
            selling_price_minor=(existing.selling_price_minor if existing is not None else 0),
            selling_currency=(
                existing.selling_currency if existing is not None else update.provider_cost_currency
            ),
            billing_parameters=dict(update.billing_parameters),
            billing_model=update.billing_model or "hourly",
            provider_available=update.provider_available,
            enabled=existing.enabled if existing is not None else False,
            provider_account_id=kwargs.get("provider_account_id"),
            technical_metadata=dict(update.technical_metadata or {}),
        )
        return None

    async def get_by_ref(
        self,
        provider_key: str,
        product_id: str,
        location_id: str,
        provider_account_id: str | None = None,
    ) -> SellableOffer | None:
        row = self.rows.get((provider_key, product_id, location_id))
        if row is not None and provider_account_id is not None:
            if (row.provider_account_id or "") != provider_account_id:
                return None
        return row

    async def set_selling_price(self, offer_id: UUID, minor: int, currency: str) -> Any:
        for key, row in self.rows.items():
            if row.id == offer_id:
                import dataclasses

                self.rows[key] = dataclasses.replace(
                    row, selling_price_minor=minor, selling_currency=currency
                )
                return self.rows[key]
        raise KeyError(offer_id)

    def _by_id(self, offer_id: UUID) -> tuple[Any, SellableOffer]:
        for key, row in self.rows.items():
            if row.id == offer_id:
                return key, row
        raise KeyError(offer_id)

    async def set_auto_price_if_current(
        self,
        offer_id: UUID,
        *,
        expected_cost_minor: int,
        expected_cost_currency: str,
        selling_price_minor: int,
        selling_currency: str,
        pricing_metadata: dict[str, object],
        expected_provider_rate: str | None = None,
    ) -> Any:
        import dataclasses

        key, row = self._by_id(offer_id)
        if (
            not row.auto_priced
            or row.operator_disabled
            or row.provider_cost_minor != expected_cost_minor
            or row.provider_cost_currency != expected_cost_currency
        ):
            return None
        self.rows[key] = dataclasses.replace(
            row,
            selling_price_minor=selling_price_minor,
            selling_currency=selling_currency,
            pricing_metadata=dict(pricing_metadata),
        )
        return self.rows[key]

    async def record_auto_pricing_failure_if_current(self, offer_id: UUID, **kwargs: Any) -> Any:
        return self._by_id(offer_id)[1]

    async def publish_if_current(
        self,
        offer_id: UUID,
        *,
        expected_price_minor: int,
        expected_currency: str,
        expected_cost_minor: int,
        expected_cost_currency: str,
        expected_provider_rate: str | None = None,
    ) -> Any:
        import dataclasses

        key, row = self._by_id(offer_id)
        if (
            row.operator_disabled
            or not row.provider_available
            or row.selling_price_minor != expected_price_minor
            or row.selling_currency != expected_currency
            or row.provider_cost_minor != expected_cost_minor
            or row.provider_cost_currency != expected_cost_currency
        ):
            return None
        self.rows[key] = dataclasses.replace(row, enabled=True)
        return self.rows[key]

    async def set_enabled(self, offer_id: UUID, enabled: bool) -> Any:
        for key, row in self.rows.items():
            if row.id == offer_id:
                import dataclasses

                self.rows[key] = dataclasses.replace(row, enabled=enabled)
                return self.rows[key]
        raise KeyError(offer_id)

    async def list_sellable(self, provider_key: str | None = None) -> list[SellableOffer]:
        return [
            row
            for row in self.rows.values()
            if row.sellable and (provider_key is None or row.provider_key == provider_key)
        ]

    async def list_all(self) -> list[SellableOffer]:
        return list(self.rows.values())

    async def mark_unavailable(self, *args: Any, **kwargs: Any) -> int:
        return 0


class _BookLocationsRepo:
    def __init__(self) -> None:
        self.records: list[Any] = []

    async def upsert(self, record: Any) -> bool:
        self.records.append(record)
        return True

    async def list_for_provider(self, provider_key: str) -> list[Any]:
        return [r for r in self.records if r.provider_key == provider_key]


class _BookStateRepo:
    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []

    async def record_run(self, **kwargs: Any) -> Any:
        self.runs.append(dict(kwargs))
        from cloud_platform.modules.offers.domain import CatalogSyncState

        return CatalogSyncState(provider_key=str(kwargs["provider_key"]))

    async def list_all(self) -> list[Any]:
        return []


class _AllowLock:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def guard(self) -> Any:
        yield True


class _DeterministicEurUsdRates:
    """Fake EUR/USD reference rates (no network)."""

    def __init__(self, rate: str = "1.17") -> None:
        from decimal import Decimal

        self._rate = Decimal(rate)

    async def get_rate(self, base: str, quote: str, *, allow_catalog_stale: bool = False) -> Any:
        from datetime import UTC, datetime, timedelta

        from cloud_platform.modules.fx.domain import FxReferenceQuote
        from cloud_platform.modules.fx.service import ReferenceRateResolution

        now = datetime.now(UTC)
        return ReferenceRateResolution(
            FxReferenceQuote(
                base_currency=base,
                quote_currency=quote,
                rate=self._rate,
                source="frankfurter",
                source_market=f"{base}/{quote}",
                provider_date=now.date(),
                observed_at=now,
                expires_at=now + timedelta(hours=1),
            ),
            stale=False,
        )

    async def get_catalog_rate(self, base: str, quote: str) -> Any:
        return await self.get_rate(base, quote, allow_catalog_stale=True)

    async def close(self) -> None:
        return None


class TestOfficialHourlySyncAcceptance:
    """Official payload -> offer -> 25% pricing -> publish -> sellable."""

    async def test_end_to_end_hourly_sellability(self) -> None:
        import cloud_platform.providers.leaseweb.cloud_sync as cloud_sync_mod
        from cloud_platform.modules.markets.domain import ProviderCatalog
        from cloud_platform.modules.offers.auto_sync import (
            CatalogAutoSyncCoordinator,
            PricingPolicy,
        )
        from cloud_platform.providers.leaseweb.cloud_auto_sync import (
            LeasewebHourlyCloudSyncSource,
        )
        from cloud_platform.providers.leaseweb.cloud_sync import LeasewebHourlyCloudSyncer

        provider = _provider(
            {
                "/publicCloud/v1/regions": _regions_payload(),
                "/publicCloud/v1/instanceTypes": _official_types_payload(),
            }
        )
        offers = _BookOffersRepo()
        locations = _BookLocationsRepo()
        state = _BookStateRepo()
        syncer = LeasewebHourlyCloudSyncer(
            lambda: None,  # type: ignore[arg-type]
            accounts={"north": provider},  # type: ignore[arg-type]
        )
        with (
            mock.patch.object(
                cloud_sync_mod, "SqlAlchemySellableOfferRepository", lambda sf: offers
            ),
            mock.patch(
                "cloud_platform.modules.catalog.repository.SqlAlchemyLocationRepository",
                lambda sf: locations,
            ),
        ):
            coordinator = CatalogAutoSyncCoordinator(
                sources=[LeasewebHourlyCloudSyncSource(syncer)],
                offers=offers,  # type: ignore[arg-type]
                state=state,  # type: ignore[arg-type]
                lock=_AllowLock(),  # type: ignore[arg-type]
                pricing_policies={
                    "leaseweb.hourly": PricingPolicy(
                        mode="markup", markup_percent=25, auto_publish=True
                    )
                },
                reference_rates=_DeterministicEurUsdRates(),
            )
            report = await coordinator.run()
        assert report.ran is True
        # Normalized offer with provenance, provider cost and availability.
        assert len(offers.upserts) == 1
        call = offers.upserts[0]
        assert call["provider_account_id"] == "north"
        update = call["update"]
        assert update.provider_cost_minor == 4  # 0.0395 EUR, HALF_UP
        assert update.provider_cost_currency == "EUR"
        assert update.provider_available is True
        assert update.billing_parameters["provider_hourly_rate"] == "0.0395"
        # 25% pricing + auto-publish -> enabled, sellable, exact integer money:
        # 0.0395 EUR * 1.17 * 1.25 = 0.05776875 USD -> 6 ceiling USD cents.
        stored = await offers.get_by_ref("leaseweb", "lsw.c3.large", "eu-west-3")
        assert stored is not None
        assert stored.selling_price_minor == 6
        assert stored.selling_currency == "USD"
        assert stored.enabled is True
        assert stored.sellable is True
        # Sync state recorded for the hourly product line.
        assert [run["provider_key"] for run in state.runs] == ["leaseweb.hourly"]
        # Family screen: Cloud available, monthly still listed but unavailable.
        from cloud_platform.modules.checkout.service import OfferCatalogViewService

        service = OfferCatalogViewService(
            offers_repo=offers,  # type: ignore[arg-type]
            provider_registry=_NoRegistry(),
            wallet_repo=_NoWallet(),
            signing_key="official-acceptance-key",
            market_catalog=ProviderCatalog(
                markets={"leaseweb": "foreign"},
                display_names={"leaseweb": "Leaseweb"},
                enabled={},
                families={
                    "leaseweb": {
                        "vps": {"billing_model": "prepaid_monthly_fixed", "display_name": "VPS"},
                        "cloud": {"billing_model": "hourly", "display_name": "Cloud"},
                    }
                },
            ),
            location_repo=locations,  # type: ignore[arg-type]
        )
        families, _, _ = await service.families_screen("leaseweb")
        assert [(f.family_key, f.available) for f in families] == [
            ("vps", False),
            ("cloud", True),
        ]
        cities = await service.cities_screen("leaseweb", "cloud", 1)
        assert [(g.country_code, g.city) for g in cities.items] == [("DE", "Frankfurt")]
        for button in [cities.items[0].select_callback]:
            assert len(button.encode("utf-8")) <= 64


_JPY_REGION = "ap-northeast-1"
_KRW_REGION = "ap-northeast-2"


def _single_region_payload(region_id: str, country: str, city: str) -> dict[str, Any]:
    return {"regions": [{"name": region_id, "country": country, "displayName": city, "city": city}]}


def _priced_types_payload(currency: str, *, name: str, hourly: str, monthly: str) -> dict[str, Any]:
    """Official ``instanceTypes`` shape in one envelope currency."""
    return {
        "instanceTypes": [
            {
                "name": name,
                "resources": {
                    "cpu": {"value": 2, "unit": "vCPU"},
                    "memory": {"value": 3, "unit": "GiB"},
                },
                "prices": {"hourly": hourly, "monthly": monthly},
                "storageTypes": ["CENTRAL"],
                "minDiskSize": 5,
            }
        ],
        "_metadata": {"currency": currency, "currencySymbol": ""},
    }


class TestZeroDecimalProviderCurrency:
    """P0 production fix: the provider's own exponent owns minor units.

    The adapter previously multiplied every provider price by 100. For a
    zero-decimal currency (JPY/KRW) that stored ``rate * 100`` while
    ``CatalogOfferPricer`` correctly re-derived ``rate`` itself, so every
    ap-northeast-1 hourly offer died with "exact provider rate does not
    match the provider_cost_minor observation" — 50 production rows.
    """

    async def test_jpy_hourly_and_monthly_use_the_zero_decimal_exponent(self) -> None:
        provider = _provider(
            {
                "/publicCloud/v1/instanceTypes": _priced_types_payload(
                    "JPY", name="lsw.c3.large", hourly="150", monthly="25000"
                )
            }
        )
        read = await provider.read_instance_types(_JPY_REGION)
        (item,) = read.types
        assert read.currency == "JPY"
        # JPY 150 is ¥150 exactly; the old *100 stored 15000 minor units.
        assert item.hourly_cost_minor == 150
        assert item.hourly_cost_minor != 15000
        assert item.monthly_cost_minor == 25000
        assert item.monthly_cost_minor != 2500000
        assert item.hourly_rate_exact == "150"
        assert item.currency == "JPY"

    async def test_krw_hourly_uses_the_zero_decimal_exponent(self) -> None:
        provider = _provider(
            {
                "/publicCloud/v1/instanceTypes": _priced_types_payload(
                    "KRW", name="lsw.c3.large", hourly="250", monthly="180000"
                )
            }
        )
        read = await provider.read_instance_types(_KRW_REGION)
        (item,) = read.types
        assert item.hourly_cost_minor == 250
        assert item.monthly_cost_minor == 180000
        assert item.currency == "KRW"

    @pytest.mark.parametrize(
        ("currency", "hourly", "expected_minor"),
        [
            ("EUR", "0.0395", 4),
            ("USD", "0.0395", 4),
            ("GBP", "0.0099", 1),
            ("EUR", "26.0200", 2602),
            ("JPY", "691", 691),
            ("KRW", "691", 691),
        ],
    )
    def test_conversion_matches_the_canonical_money_helper(
        self, currency: str, hourly: str, expected_minor: int
    ) -> None:
        from decimal import Decimal

        from cloud_platform.modules.fx.domain import FxPurpose, major_to_minor
        from cloud_platform.providers.leaseweb.cloud import _provider_minor

        assert _provider_minor(hourly, currency) == expected_minor
        # The observation is exactly what the pricer re-derives, by
        # construction, for every audited currency.
        assert _provider_minor(hourly, currency) == major_to_minor(
            Decimal(hourly), currency, FxPurpose.DISPLAY
        )

    def test_unaudited_currency_fails_closed_instead_of_assuming_cents(self) -> None:
        from cloud_platform.providers.leaseweb.cloud import _provider_minor

        assert _provider_minor("12.50", "CHF") is None
        assert _provider_minor("12.50", "XYZ") is None

    async def test_unaudited_envelope_currency_drops_types_without_deleting_facts(
        self,
    ) -> None:
        provider = _provider(
            {
                "/publicCloud/v1/instanceTypes": _priced_types_payload(
                    "CHF", name="lsw.c3.large", hourly="12.50", monthly="2000"
                )
            }
        )
        read = await provider.read_instance_types("eu-west-3")
        assert read.currency == "CHF"
        assert read.raw_items == 1
        # The price IS present (diagnostic), but nothing is convertible.
        assert read.priced_items == 1
        assert read.types == ()

    @pytest.mark.parametrize(
        ("currency", "region", "country", "city", "hourly", "rate", "expected_usd_minor"),
        [
            ("JPY", _JPY_REGION, "JP", "Tokyo", "150", "0.0068", 128),
            ("KRW", _KRW_REGION, "KR", "Seoul", "250", "0.00072", 23),
        ],
    )
    async def test_zero_decimal_offer_prices_to_usd_with_valid_provenance(
        self,
        currency: str,
        region: str,
        country: str,
        city: str,
        hourly: str,
        rate: str,
        expected_usd_minor: int,
    ) -> None:
        """End-to-end: parse -> offer -> 25% pricing -> publish -> sellable."""
        import cloud_platform.providers.leaseweb.cloud_sync as cloud_sync_mod
        from cloud_platform.modules.offers.auto_sync import (
            CatalogAutoSyncCoordinator,
            PricingPolicy,
        )
        from cloud_platform.modules.offers.domain import has_valid_pricing_provenance
        from cloud_platform.providers.leaseweb.cloud_auto_sync import (
            LeasewebHourlyCloudSyncSource,
        )
        from cloud_platform.providers.leaseweb.cloud_sync import LeasewebHourlyCloudSyncer

        provider = _provider(
            {
                "/publicCloud/v1/regions": _single_region_payload(region, country, city),
                "/publicCloud/v1/instanceTypes": _priced_types_payload(
                    currency, name="lsw.c3.large", hourly=hourly, monthly="100000"
                ),
            }
        )
        offers = _BookOffersRepo()
        locations = _BookLocationsRepo()
        state = _BookStateRepo()
        syncer = LeasewebHourlyCloudSyncer(
            lambda: None,  # type: ignore[arg-type]
            accounts={"north": provider},  # type: ignore[arg-type]
        )
        with (
            mock.patch.object(
                cloud_sync_mod, "SqlAlchemySellableOfferRepository", lambda sf: offers
            ),
            mock.patch(
                "cloud_platform.modules.catalog.repository.SqlAlchemyLocationRepository",
                lambda sf: locations,
            ),
        ):
            coordinator = CatalogAutoSyncCoordinator(
                sources=[LeasewebHourlyCloudSyncSource(syncer)],
                offers=offers,  # type: ignore[arg-type]
                state=state,  # type: ignore[arg-type]
                lock=_AllowLock(),  # type: ignore[arg-type]
                pricing_policies={
                    "leaseweb.hourly": PricingPolicy(
                        mode="markup", markup_percent=25, auto_publish=True
                    )
                },
                reference_rates=_DeterministicEurUsdRates(rate),
            )
            report = await coordinator.run()

        assert report.ran is True
        run = report.providers[0]
        # No pricing failure: this is the bug that killed all 50 JP rows.
        assert run.ok is True, (run.errors, run.warnings)
        assert run.prices_updated == 1
        assert run.published == 1
        stored = await offers.get_by_ref("leaseweb", "lsw.c3.large", region)
        assert stored is not None
        assert stored.provider_cost_currency == currency
        assert stored.selling_currency == "USD"
        assert stored.selling_price_minor == expected_usd_minor
        assert stored.enabled is True
        assert stored.sellable is True
        assert has_valid_pricing_provenance(stored, "USD") is True
        metadata = stored.pricing_metadata
        assert metadata["markup_percent"] == "25"
        assert metadata["source_currency"] == currency
        # The exact provider rate — never the rounded minor observation —
        # is what the FX/markup arithmetic consumed.
        assert metadata["source_amount"] == hourly
        assert metadata["source_amount_basis"] == "exact_provider_rate_major"
        assert metadata["fx_provider"] == "frankfurter"
        # Both parametrized currencies are zero-decimal, so the stored
        # observation is the major amount itself.
        assert metadata["provider_cost_minor"] == int(hourly) == stored.provider_cost_minor
        # Verbatim provider rate still carried, and the monthly provider
        # price stayed a reference fact — it never priced the offer.
        assert stored.billing_parameters["provider_hourly_rate"] == hourly
        assert stored.billing_parameters["monthly_estimate_source"] == "hourly_rate"
        assert stored.billing_parameters["provider_monthly_cost_minor"] == 100000


class _NoRegistry:
    def get(self, key: str) -> Any:
        raise KeyError(key)


class _NoWallet:
    async def get(self, user_id: Any) -> Any:
        raise AssertionError("no wallet reads here")
