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
    return {
        "instanceTypes": [
            {
                "name": "lsw.mini",
                "displayName": "Mini",
                "cpu": 1,
                "memoryMb": 1024,
                "rootDiskSize": 25,
                "pricePerHour": "0.015",
                "pricePerMonth": "10.00",
                "currency": "EUR",
                "architecture": "x86_64",
                "cpuType": "shared",
                "storageType": "ssd",
                "family": "General Purpose",
            },
            {
                "name": "lsw.big",
                "cpu": 4,
                "memoryMb": 16384,
                "disk": 200,
                "pricePerHour": "0.12",
                "currency": "EUR",
            },
            {"name": "lsw.noprice", "cpu": 2, "memoryMb": 2048},
            {"name": "lsw.nocurrency", "cpu": 2, "memoryMb": 2048, "pricePerHour": "0.05"},
        ]
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


class TestInstanceTypes:
    async def test_types_parse_specs_and_hourly_price(self) -> None:
        provider = _provider()
        provider._transport.request = AsyncMock(return_value=_types_payload())
        types = await provider.list_instance_types("eu-west-3")
        assert [t.id for t in types] == ["lsw.mini", "lsw.big"]
        mini = types[0]
        assert (mini.vcpu, mini.ram_gb, mini.disk_gb) == (1, 1, 25)
        assert mini.hourly_cost_minor == 2  # 0.015 EUR, half-up to minor units
        assert mini.currency == "EUR"
        assert mini.architecture == "x86_64"
        assert mini.cpu_type == "shared"
        assert mini.storage_type == "ssd"

    async def test_missing_price_or_currency_fails_closed(self) -> None:
        provider = _provider()
        provider._transport.request = AsyncMock(return_value=_types_payload())
        types = await provider.list_instance_types("eu-west-3")
        assert "lsw.noprice" not in [t.id for t in types]
        assert "lsw.nocurrency" not in [t.id for t in types]


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
