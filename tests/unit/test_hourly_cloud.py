"""Leaseweb hourly Cloud: adapter, sync and catalog (REWORK).

- Regions/instance types/images parse from official payloads only.
- Instance families come from provider fields or the explicit other
  bucket — never invented, never dropped.
- Hourly costs parse Decimal-only; missing price/currency fails closed.
- Sync is per-region isolated and retires hourly rows only.
- Creation always carries contractType=HOURLY; ambiguous outcomes never
  blindly re-POST (get-before-create correlation).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.providers.errors import ProviderOutcomeUnknown
from cloud_platform.providers.leaseweb.cloud import (
    CONTRACT_TYPE_HOURLY,
    LeasewebHourlyCloudProvider,
    build_create_body,
    classify_instance_family,
)
from cloud_platform.providers.leaseweb.errors import LeasewebValidationError


def _provider(**overrides: Any) -> LeasewebHourlyCloudProvider:
    provider = LeasewebHourlyCloudProvider.__new__(LeasewebHourlyCloudProvider)
    provider._transport = AsyncMock()
    for key, value in overrides.items():
        setattr(provider, key, value)
    return provider


def _regions_payload() -> dict[str, Any]:
    return {
        "regions": [
            {"name": "eu-west-3", "country": "DE", "displayName": "Frankfurt", "city": "Frankfurt"},
            {"name": "eu-west-1", "country": "NL", "displayName": "Amsterdam"},
            {"name": "xx-1", "country": "XX1"},
        ]
    }


def _types_payload() -> dict[str, Any]:
    # Official Public Cloud shape: nested resources, prices.hourly, envelope
    # _metadata.currency (no per-item currency exists in the real API).
    return {
        "instanceTypes": [
            {
                "name": "lsw.m4.large",
                "resources": {
                    "cpu": {"value": 2, "unit": "vCPU"},
                    "memory": {"value": 8, "unit": "GiB"},
                    "publicNetworkSpeed": {"value": 5, "unit": "Gbps"},
                    "privateNetworkSpeed": {"value": 1, "unit": "Gbps"},
                },
                "prices": {"hourly": "0.0395", "monthly": "26.0200"},
                "storageTypes": ["CENTRAL"],
                "minDiskSize": 5,
            },
            {
                "name": "lsw.c3.large",
                "resources": {
                    "cpu": {"value": 2, "unit": "vCPU"},
                    "memory": {"value": 3, "unit": "GiB"},
                },
                "prices": {"hourly": "0.0523"},
                "storageTypes": ["CENTRAL"],
                "minDiskSize": 5,
            },
            {
                "name": "lsw.noprice",
                "resources": {
                    "cpu": {"value": 2, "unit": "vCPU"},
                    "memory": {"value": 4, "unit": "GiB"},
                },
                "storageTypes": ["CENTRAL"],
                "minDiskSize": 5,
            },
        ],
        "_metadata": {"currency": "EUR", "currencySymbol": "€"},
    }


class TestRegions:
    async def test_regions_normalize_country_and_city(self) -> None:
        provider = _provider()
        provider._transport.request = AsyncMock(return_value=_regions_payload())
        regions = await provider.list_regions()
        assert [(r.id, r.country_code) for r in regions] == [
            ("eu-west-3", "DE"),
            ("eu-west-1", "NL"),
            ("xx-1", None),
        ]
        assert regions[0].city == "Frankfurt"

    async def test_region_without_code_is_skipped(self) -> None:
        provider = _provider()
        provider._transport.request = AsyncMock(return_value={"regions": [{"country": "DE"}]})
        assert await provider.list_regions() == []

    async def test_documented_regions_normalize_country_and_city(self) -> None:
        provider = _provider()
        provider._transport.request = AsyncMock(
            return_value={
                "regions": [
                    {"name": "eu-central-1"},
                    {"name": "eu-west-2"},
                    {"name": "ap-northeast-1"},
                ]
            }
        )
        regions = await provider.list_regions()
        assert [(r.id, r.country_code, r.city) for r in regions] == [
            ("eu-central-1", "DE", "Frankfurt"),
            ("eu-west-2", "GB", "London"),
            ("ap-northeast-1", "JP", "Tokyo"),
        ]

    async def test_payload_country_and_city_win_over_mapping(self) -> None:
        provider = _provider()
        provider._transport.request = AsyncMock(
            return_value={
                "regions": [
                    {
                        "name": "eu-central-1",
                        "country": "NL",
                        "city": "Amsterdam",
                        "displayName": "Amsterdam",
                    }
                ]
            }
        )
        (region,) = await provider.list_regions()
        assert (region.country_code, region.city) == ("NL", "Amsterdam")

    async def test_unmapped_region_stays_neutral(self) -> None:
        provider = _provider()
        provider._transport.request = AsyncMock(return_value={"regions": [{"name": "xx-new-9"}]})
        (region,) = await provider.list_regions()
        assert region.country_code is None
        assert region.city is None


class TestInstanceTypes:
    async def test_types_parse_specs_and_hourly_price(self) -> None:
        provider = _provider()
        provider._transport.request = AsyncMock(return_value=_types_payload())
        types = await provider.list_instance_types("eu-west-3")
        assert [t.id for t in types] == ["lsw.m4.large", "lsw.c3.large"]
        large = types[0]
        assert (large.vcpu, large.ram_gb, large.disk_gb) == (2, 8, 5)
        assert large.hourly_cost_minor == 4  # 0.0395 EUR, half-up to minor units
        assert large.hourly_rate_exact == "0.0395"
        assert large.monthly_cost_minor == 2602
        assert large.currency == "EUR"
        assert (large.family_key, large.family_name) == ("general", "General Purpose")
        assert large.storage_type == "CENTRAL"
        assert large.storage_types == ("CENTRAL",)
        assert large.memory_gb_exact == "8 GiB"
        assert large.network_public == "5 Gbps"
        assert large.network_private == "1 Gbps"

    async def test_missing_price_fails_closed(self) -> None:
        provider = _provider()
        provider._transport.request = AsyncMock(return_value=_types_payload())
        types = await provider.list_instance_types("eu-west-3")
        assert "lsw.noprice" not in [t.id for t in types]

    async def test_missing_envelope_currency_fails_closed(self) -> None:
        # No _metadata.currency anywhere: every item fails closed for
        # pricing, even with valid prices.hourly values.
        provider = _provider()
        payload = _types_payload()
        del payload["_metadata"]
        provider._transport.request = AsyncMock(return_value=payload)
        assert await provider.list_instance_types("eu-west-3") == []


class TestFamilyClassification:
    def test_explicit_family_fields_win(self) -> None:
        assert classify_instance_family({"family": "Compute Optimized"}) == (
            "compute-optimized",
            "Compute Optimized",
        )
        assert classify_instance_family({"category": "Memory"}) == ("memory", "Memory")

    def test_unclassifiable_goes_to_other(self) -> None:
        assert classify_instance_family({"name": "lsw.mini"}) == ("other", "Other")
        assert classify_instance_family({}) == ("other", "Other")

    def test_wire_safe_keys(self) -> None:
        key, _name = classify_instance_family({"family": "General Purpose!"})
        assert key == "general-purpose"


class TestImages:
    async def test_images_keep_label_and_id_separate(self) -> None:
        provider = _provider()
        provider._transport.request = AsyncMock(
            return_value={
                "images": [
                    {"id": "UBUNTU_24_04", "displayName": "Ubuntu 24.04", "os": "ubuntu"},
                    {"name": "Debian 12"},
                ]
            }
        )
        images = await provider.list_images("eu-west-3")
        assert [(i.id, i.label) for i in images] == [
            ("UBUNTU_24_04", "Ubuntu 24.04"),
            ("Debian 12", "Debian 12"),
        ]

    async def test_rejected_region_filter_falls_back_to_the_global_catalog(self) -> None:
        """The provider rejects every region but one and serves a GLOBAL list.

        Observed 2026-09-25: ``?region=eu-west-3`` (and every other sellable
        region) answers HTTP 400 "not valid region", while the endpoint's own
        payload states ``region: null`` for every image. A rejected request
        shape must fall back to the global read instead of reporting a sellable
        plan as having no operating system.
        """
        provider = _provider()
        calls: list[dict[str, Any]] = []

        async def _request(
            method: str, path: str, *, params: dict[str, Any] | None = None, **kw: Any
        ) -> Any:
            calls.append({"method": method, "path": path, "params": dict(params or {})})
            if params:
                raise LeasewebValidationError("errorCode=400; Validation Failed; HTTP 400")
            return {
                "images": [
                    {
                        "id": "UBUNTU_24_04_64BIT",
                        "name": "Ubuntu 24.04 LTS (x86_64)",
                        "family": "linux",
                        "architecture": "x86_64",
                        "region": None,
                        "state": "READY",
                    }
                ]
            }

        provider._transport.request = _request
        images = await provider.list_images("eu-west-3")
        assert [image.id for image in images] == ["UBUNTU_24_04_64BIT"]
        assert images[0].architecture == "x86_64"
        assert [call["params"] for call in calls] == [{"region": "eu-west-3"}, {}]

    async def test_region_scoped_probe_never_falls_back(self) -> None:
        """Ownership routing needs the rejection, not a global list.

        The region filter's accepted value identifies the credential's own
        Sales Organization location; the fallback that fixes the customer
        screen must therefore never be used for routing.
        """
        provider = _provider()
        provider._transport.request = AsyncMock(
            side_effect=LeasewebValidationError("errorCode=400; Validation Failed; HTTP 400")
        )
        with pytest.raises(LeasewebValidationError):
            await provider.probe_region_images("eu-west-3")
        assert provider._transport.request.await_count == 1
        assert provider._transport.request.await_args.args[1] == "/publicCloud/v1/images"

    async def test_other_read_failures_do_not_fall_back(self) -> None:
        """Only a rejected request SHAPE widens the read; nothing else does."""
        from cloud_platform.providers.leaseweb.errors import LeasewebAuthenticationError

        provider = _provider()
        provider._transport.request = AsyncMock(
            side_effect=LeasewebAuthenticationError("errorCode=401; HTTP 401")
        )
        with pytest.raises(LeasewebAuthenticationError):
            await provider.list_images("eu-west-3")
        assert provider._transport.request.await_count == 1


class TestInstanceIdentity:
    def test_identity_fields_parse_when_present(self) -> None:
        from cloud_platform.providers.leaseweb.cloud import _parse_instance

        parsed = _parse_instance(
            {
                "id": "i-1",
                "reference": "srv-abc",
                "state": "RUNNING",
                "region": "eu-west-3",
                "instanceType": "lsw.m4.large",
                "imageId": "UBUNTU_24_04",
            }
        )
        assert parsed is not None
        assert parsed.instance_type == "lsw.m4.large"
        assert parsed.image_id == "UBUNTU_24_04"
        assert parsed.account_id is None

    def test_missing_identity_fields_stay_absent(self) -> None:
        from cloud_platform.providers.leaseweb.cloud import _parse_instance

        parsed = _parse_instance({"id": "i-1", "state": "RUNNING"})
        assert parsed is not None
        assert parsed.instance_type is None
        assert parsed.image_id is None
        assert parsed.account_id is None


class TestCheckoutRevalidation:
    """The adapter revalidates offer+image facts live before any create."""

    def _validated_provider(self, types: Any, images: Any) -> LeasewebHourlyCloudProvider:
        provider = _provider()

        async def _request(method: str, path: str, **kwargs: Any) -> Any:
            assert method == "GET"
            if path == "/publicCloud/v1/instanceTypes":
                return types
            if path == "/publicCloud/v1/images":
                return images
            raise AssertionError(f"unexpected provider read: {path}")

        provider._transport.request = _request  # type: ignore[method-assign]
        return provider

    def _types_payload(self) -> Any:
        return {
            "instanceTypes": [
                {
                    "name": "lsw.m4.large",
                    "resources": {
                        "cpu": {"value": 2, "unit": "vCPU"},
                        "memory": {"value": 8, "unit": "GiB"},
                    },
                    "prices": {"hourly": "0.0395"},
                    "storageTypes": ["CENTRAL"],
                    "minDiskSize": 5,
                }
            ],
            "_metadata": {"currency": "EUR", "currencySymbol": "€"},
        }

    def _images_payload(self) -> Any:
        return {"images": [{"id": "UBUNTU_24_04", "displayName": "Ubuntu 24.04"}]}

    async def test_matching_facts_validate(self) -> None:
        provider = self._validated_provider(self._types_payload(), self._images_payload())
        match = await provider.validate_hourly_offer_for_checkout(
            location_id="eu-west-3",
            product_id="lsw.m4.large",
            image_id="UBUNTU_24_04",
            expected_cost_minor=4,
            currency="EUR",
            expected_cost_exact="0.0395",
        )
        assert match.id == "lsw.m4.large"

    async def test_changed_cost_or_currency_fails(self) -> None:
        from cloud_platform.providers.errors import ProviderError

        provider = self._validated_provider(self._types_payload(), self._images_payload())
        with pytest.raises(ProviderError):
            await provider.validate_hourly_offer_for_checkout(
                location_id="eu-west-3",
                product_id="lsw.m4.large",
                image_id="UBUNTU_24_04",
                expected_cost_minor=99,
                currency="EUR",
                expected_cost_exact="0.0395",
            )
        with pytest.raises(ProviderError):
            await provider.validate_hourly_offer_for_checkout(
                location_id="eu-west-3",
                product_id="lsw.m4.large",
                image_id="UBUNTU_24_04",
                expected_cost_minor=4,
                currency="USD",
                expected_cost_exact="0.0395",
            )

    async def test_missing_type_or_image_fails(self) -> None:
        from cloud_platform.providers.errors import ProviderNotFound

        provider = self._validated_provider(self._types_payload(), self._images_payload())
        with pytest.raises(ProviderNotFound):
            await provider.validate_hourly_offer_for_checkout(
                location_id="eu-west-3",
                product_id="lsw.nope",
                image_id="UBUNTU_24_04",
                expected_cost_minor=4,
                currency="EUR",
                expected_cost_exact="0.0395",
            )
        with pytest.raises(ProviderNotFound):
            await provider.validate_hourly_offer_for_checkout(
                location_id="eu-west-3",
                product_id="lsw.m4.large",
                image_id="DEBIAN_12",
                expected_cost_minor=4,
                currency="EUR",
                expected_cost_exact="0.0395",
            )


class TestCreateBody:
    def test_body_carries_hourly_contract(self) -> None:
        body = build_create_body(
            instance_type="lsw.mini",
            image_id="UBUNTU_24_04",
            region="eu-west-3",
            reference="srv-abc",
        )
        assert body["contractType"] == CONTRACT_TYPE_HOURLY == "HOURLY"
        assert body["type"] == "lsw.mini"
        assert body["imageId"] == "UBUNTU_24_04"
        assert body["region"] == "eu-west-3"
        assert body["reference"] == "srv-abc"
        assert "rootDiskSize" not in body  # provider default, never invented


class _AmbiguousError(ProviderOutcomeUnknown):
    pass


class TestCreateInstance:
    async def test_existing_reference_returns_without_post(self) -> None:
        provider = _provider()
        calls: list[str] = []

        async def _request(method: str, path: str, **kwargs: Any) -> Any:
            calls.append(f"{method} {path}")
            if method == "GET":
                return {"instances": [{"id": "i-1", "reference": "srv-abc", "state": "RUNNING"}]}
            raise AssertionError("POST must not happen when the reference exists")

        provider._transport.request = _request  # type: ignore[method-assign]
        created = await provider.create_instance(
            instance_type="lsw.mini",
            image_id="UBUNTU_24_04",
            region="eu-west-3",
            reference="srv-abc",
            idempotency_key=IdempotencyKey("op-1-12345678"),
        )
        assert created.id == "i-1"
        assert not any(call.startswith("POST") for call in calls)

    async def test_post_then_map(self) -> None:
        provider = _provider()
        seen: dict[str, Any] = {}

        async def _request(method: str, path: str, **kwargs: Any) -> Any:
            if method == "GET":
                return {"instances": []}
            seen.update(kwargs.get("json", {}))
            return {"instance": {"id": "i-9", "reference": "srv-xyz", "state": "CREATING"}}

        provider._transport.request = _request  # type: ignore[method-assign]
        created = await provider.create_instance(
            instance_type="lsw.mini",
            image_id="UBUNTU_24_04",
            region="eu-west-3",
            reference="srv-xyz",
            idempotency_key=IdempotencyKey("op-2-12345678"),
        )
        assert created.id == "i-9"
        assert seen["contractType"] == "HOURLY"

    async def test_ambiguous_error_propagates_for_the_ledger(self) -> None:
        provider = _provider()

        async def _request(method: str, path: str, **kwargs: Any) -> Any:
            if method == "GET":
                return {"instances": []}
            raise _AmbiguousError("timeout after transmit")

        provider._transport.request = _request  # type: ignore[method-assign]
        with pytest.raises(ProviderOutcomeUnknown):
            await provider.create_instance(
                instance_type="lsw.mini",
                image_id="UBUNTU_24_04",
                region="eu-west-3",
                reference="srv-zzz",
                idempotency_key=IdempotencyKey("op-3-12345678"),
            )

    async def test_unexpected_payload_fails_closed(self) -> None:
        from cloud_platform.providers.errors import ProviderError

        provider = _provider()

        async def _request(method: str, path: str, **kwargs: Any) -> Any:
            if method == "GET":
                return {"instances": []}
            return {"unexpected": True}

        provider._transport.request = _request  # type: ignore[method-assign]
        with pytest.raises(ProviderError):
            await provider.create_instance(
                instance_type="lsw.mini",
                image_id="UBUNTU_24_04",
                region="eu-west-3",
                reference="srv-zzz",
                idempotency_key=IdempotencyKey("op-4-12345678"),
            )
