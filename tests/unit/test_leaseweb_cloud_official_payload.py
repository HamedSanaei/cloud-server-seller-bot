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
        self, provider_key: str, product_id: str, location_id: str
    ) -> SellableOffer | None:
        return self.rows.get((provider_key, product_id, location_id))

    async def set_selling_price(self, offer_id: UUID, minor: int, currency: str) -> Any:
        for key, row in self.rows.items():
            if row.id == offer_id:
                import dataclasses

                self.rows[key] = dataclasses.replace(
                    row, selling_price_minor=minor, selling_currency=currency
                )
                return self.rows[key]
        raise KeyError(offer_id)

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
        # 25% pricing + auto-publish -> enabled, sellable, exact integer money.
        stored = await offers.get_by_ref("leaseweb", "lsw.c3.large", "eu-west-3")
        assert stored is not None
        assert stored.selling_price_minor == 5  # ceil(4 * 1.25), integer cents
        assert stored.selling_currency == "EUR"
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


class _NoRegistry:
    def get(self, key: str) -> Any:
        raise KeyError(key)


class _NoWallet:
    async def get(self, user_id: Any) -> Any:
        raise AssertionError("no wallet reads here")
