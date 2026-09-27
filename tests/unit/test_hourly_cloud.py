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

from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.providers.errors import ProviderError, ProviderOutcomeUnknown
from cloud_platform.providers.leaseweb.cloud import (
    CONTRACT_TYPE_HOURLY,
    ROOT_DISK_MAX_GB,
    CloudImage,
    CloudRootDisk,
    HourlyCheckoutFacts,
    LeasewebHourlyCloudProvider,
    build_create_body,
    classify_instance_family,
    image_root_disk_floor,
    resolve_root_disk,
    validate_root_disk,
)
from cloud_platform.providers.leaseweb.errors import LeasewebValidationError


def _provider(**overrides: Any) -> LeasewebHourlyCloudProvider:
    provider = LeasewebHourlyCloudProvider.__new__(LeasewebHourlyCloudProvider)
    provider._transport = AsyncMock()
    for key, value in overrides.items():
        setattr(provider, key, value)
    return provider


def _documented_region_rejection() -> LeasewebValidationError:
    """The DOCUMENTED 400 for a region this credential does not own.

    Built through the audited parser (``errorDetails.region``, exactly the
    envelope production sends), because that field is what the platform now
    reads to decide whether the *filter* — or the request itself — was rejected.
    """
    import httpx

    from cloud_platform.providers.leaseweb.errors import (
        error_for_response,
        parse_error_payload,
    )

    payload = parse_error_payload(
        httpx.Response(
            status_code=400,
            json={
                "errorCode": "400",
                "errorMessage": "Validation Failed",
                "errorDetails": {"region": ['The value "eu-west-3" is not valid region.']},
            },
        )
    )
    return error_for_response(payload, endpoint="/publicCloud/v1/images")


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
                raise _documented_region_rejection()
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

    async def test_a_non_region_400_never_widens_the_image_read(self) -> None:
        """A 400 about anything BUT the region filter must fail closed.

        The earlier behaviour widened the read on ANY validation error, which
        could hand a customer screen (and a checkout) the global catalog after
        an unrelated rejection. Only the documented region detail counts.
        """
        import httpx

        from cloud_platform.providers.leaseweb.errors import (
            error_for_response,
            parse_error_payload,
        )

        payload = parse_error_payload(
            httpx.Response(
                status_code=400,
                json={
                    "errorCode": "400",
                    "errorMessage": "Validation Failed",
                    "errorDetails": {"rootDiskSize": ["This value should be 5 or more."]},
                },
            )
        )
        rejection = error_for_response(payload, endpoint="/publicCloud/v1/images")
        provider = _provider()
        provider._transport.request = AsyncMock(side_effect=rejection)
        with pytest.raises(LeasewebValidationError):
            await provider.list_images("eu-west-3")
        assert provider._transport.request.await_count == 1

    async def test_one_verdict_for_doctor_sync_and_checkout(self) -> None:
        """The doctor, the catalog sync and the checkout revalidation answer the
        SAME question the same way.

        A rejected region filter is UNSERVED: the global catalog is legitimate
        for the customer OS screen and proves nothing about NEW-instance
        capability, so routing and a billable create must refuse it.
        """
        from cloud_platform.providers.leaseweb.cloud import RegionImagesState

        provider = _provider()
        calls: list[str] = []

        async def _request(
            method: str, path: str, *, params: dict[str, Any] | None = None, **kw: Any
        ) -> Any:
            calls.append(path)
            if params:
                raise _documented_region_rejection()
            return {
                "images": [{"id": "UBUNTU_24_04", "displayName": "Ubuntu 24.04", "region": None}]
            }

        provider._transport.request = _request

        verdict = await provider.region_images_verdict("eu-central-1")
        assert verdict.state is RegionImagesState.UNSERVED
        assert verdict.region_scoped is False
        assert verdict.proven is False
        assert [image.id for image in verdict.images] == ["UBUNTU_24_04"]
        assert "proves display only" in verdict.safe_note()

        # The customer OS screen may still show the global catalog...
        assert [image.id for image in await provider.list_images("eu-central-1")] == [
            "UBUNTU_24_04"
        ]
        # ...while routing (the sync's proof) and checkout (the create path)
        # must fail closed instead of treating display data as capability.
        with pytest.raises(LeasewebValidationError):
            await provider.probe_region_images("eu-central-1")
        with pytest.raises(LeasewebValidationError):
            await provider.installable_images("eu-central-1")
        # Every one of those reads went through the single implementation.
        assert calls == ["/publicCloud/v1/images"] * 8

    async def test_an_empty_scoped_read_is_a_verdict_not_a_proof(self) -> None:
        """A readable region with no usable image is EMPTY, never PROVEN."""
        from cloud_platform.providers.leaseweb.cloud import RegionImagesState

        provider = _provider()
        provider._transport.request = AsyncMock(return_value={"images": []})
        verdict = await provider.region_images_verdict("eu-west-3")
        assert verdict.state is RegionImagesState.EMPTY
        assert verdict.region_scoped is True
        assert verdict.proven is False
        assert "listed no usable image" in verdict.safe_note()


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
        # Official images shape: per-image minDiskSize + storageTypes facts.
        return {
            "images": [
                {
                    "id": "UBUNTU_24_04",
                    "displayName": "Ubuntu 24.04 LTS (x86_64)",
                    "family": "linux",
                    "architecture": "x86_64",
                    "region": None,
                    "state": "READY",
                    "minDiskSize": 5,
                    "storageTypes": ["LOCAL", "CENTRAL"],
                }
            ]
        }

    async def test_matching_facts_validate_and_pin_the_launch_root_disk(self) -> None:
        provider = self._validated_provider(self._types_payload(), self._images_payload())
        facts = await provider.validate_hourly_offer_for_checkout(
            location_id="eu-west-3",
            product_id="lsw.m4.large",
            image_id="UBUNTU_24_04",
            expected_cost_minor=4,
            currency="EUR",
            expected_cost_exact="0.0395",
        )
        assert isinstance(facts, HourlyCheckoutFacts)
        assert facts.instance_type.id == "lsw.m4.large"
        # The pinned disk satisfies both provider floors (type 5, image 5).
        assert facts.root_disk == CloudRootDisk(size_gb=5, storage_type="CENTRAL")

    async def test_pinned_root_disk_is_re_verified_against_live_facts(self) -> None:
        """A pinned disk the provider would now reject fails closed (no POST)."""
        provider = self._validated_provider(self._types_payload(), self._images_payload())
        facts = await provider.validate_hourly_offer_for_checkout(
            location_id="eu-west-3",
            product_id="lsw.m4.large",
            image_id="UBUNTU_24_04",
            expected_cost_minor=4,
            currency="EUR",
            expected_cost_exact="0.0395",
            root_disk_size_gb=5,
            root_disk_storage_type="CENTRAL",
        )
        assert facts.root_disk == CloudRootDisk(size_gb=5, storage_type="CENTRAL")
        # Below the provider floor: rejected before any billable call.
        with pytest.raises(ProviderError):
            await provider.validate_hourly_offer_for_checkout(
                location_id="eu-west-3",
                product_id="lsw.m4.large",
                image_id="UBUNTU_24_04",
                expected_cost_minor=4,
                currency="EUR",
                expected_cost_exact="0.0395",
                root_disk_size_gb=1,
                root_disk_storage_type="CENTRAL",
            )
        with pytest.raises(ProviderError):
            await provider.validate_hourly_offer_for_checkout(
                location_id="eu-west-3",
                product_id="lsw.m4.large",
                image_id="UBUNTU_24_04",
                expected_cost_minor=4,
                currency="EUR",
                expected_cost_exact="0.0395",
                root_disk_size_gb=5,
                root_disk_storage_type="LOCAL",
            )

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
    """The launch body must BE the documented create contract (no extras).

    Production observation 2026-09-25: the platform sent ``labels`` (which
    this endpoint does not document) and omitted the two REQUIRED root-disk
    fields, so Leaseweb answered ``400 Validation Failed`` before accepting
    the instance.
    """

    #: Fields the official ``launchInstanceOpts`` schema documents.
    DOCUMENTED_OPTIONAL: ClassVar[set[str]] = {
        "reference",
        "contractTerm",
        "billingFrequency",
        "sshKey",
        "userData",
        "marketAppId",
    }
    DOCUMENTED_REQUIRED: ClassVar[set[str]] = {
        "region",
        "imageId",
        "contractType",
        "rootDiskSize",
        "rootDiskStorageType",
        "type",
    }

    def _body(self, **overrides: Any) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "instance_type": "lsw.m3.large",
            "image_id": "UBUNTU_26_04_64BIT",
            "region": "eu-central-1",
            "reference": "srv-7dbdf058",
            "root_disk_size_gb": 5,
            "root_disk_storage_type": "CENTRAL",
        }
        kwargs.update(overrides)
        return build_create_body(**kwargs)

    def test_body_is_exactly_the_documented_contract(self) -> None:
        assert self._body() == {
            "type": "lsw.m3.large",
            "imageId": "UBUNTU_26_04_64BIT",
            "region": "eu-central-1",
            "reference": "srv-7dbdf058",
            "contractType": CONTRACT_TYPE_HOURLY,
            "rootDiskSize": 5,
            "rootDiskStorageType": "CENTRAL",
        }

    def test_every_required_provider_field_is_present(self) -> None:
        assert self.DOCUMENTED_REQUIRED <= set(self._body())

    def test_undocumented_fields_are_never_sent(self) -> None:
        assert "labels" not in self._body()
        assert set(self._body()) <= self.DOCUMENTED_REQUIRED | self.DOCUMENTED_OPTIONAL

    def test_reference_is_bounded_to_the_provider_limit(self) -> None:
        body = self._body(reference="r" * 200)
        assert body["reference"] == "r" * 64

    def test_ssh_key_is_sent_only_when_provided(self) -> None:
        assert "sshKey" not in self._body()
        assert self._body(ssh_key_id="ssh-ed25519 AAAA")["sshKey"] == "ssh-ed25519 AAAA"

    def test_an_invalid_root_disk_never_builds_a_body(self) -> None:
        for overrides in (
            {"root_disk_size_gb": 0},
            {"root_disk_size_gb": ROOT_DISK_MAX_GB + 1},
            {"root_disk_size_gb": "5"},
            {"root_disk_size_gb": True},
            {"root_disk_storage_type": ""},
            {"root_disk_storage_type": "SSD"},
            {"root_disk_size_gb": 5, "os_family": "windows"},
        ):
            with pytest.raises(ProviderError):
                self._body(**overrides)

    def test_the_incident_body_is_valid_now(self) -> None:
        """Regression: lsw.m3.large / eu-central-1 / Ubuntu with a 5 GB CENTRAL disk.

        This is the exact plan+region+image+disk of the production 400, and it
        must reproduce the documented request shape field for field.
        """
        body = self._body()
        assert body == {
            "type": "lsw.m3.large",
            "imageId": "UBUNTU_26_04_64BIT",
            "region": "eu-central-1",
            "reference": "srv-7dbdf058",
            "contractType": "HOURLY",
            "rootDiskSize": 5,
            "rootDiskStorageType": "CENTRAL",
        }


class TestLaunchRootDiskFacts:
    """Root disk is derived from provider facts, never hardcoded."""

    def _image(self, **overrides: Any) -> CloudImage:
        kwargs: dict[str, Any] = {
            "id": "UBUNTU_26_04_64BIT",
            "label": "Ubuntu 26.04 LTS (x86_64)",
            "os_family": "linux",
            "architecture": "x86_64",
            "min_disk_size_gb": 5,
            "storage_types": ("LOCAL", "CENTRAL"),
        }
        kwargs.update(overrides)
        return CloudImage(**kwargs)

    def test_the_type_minimum_is_the_floor_when_the_image_is_smaller(self) -> None:
        disk = resolve_root_disk(
            disk_gb=20,
            storage_type="CENTRAL",
            image=self._image(min_disk_size_gb=5),
            type_storage_types=("CENTRAL", "LOCAL"),
        )
        assert disk == CloudRootDisk(size_gb=20, storage_type="CENTRAL")

    def test_the_image_minimum_wins_when_it_is_higher(self) -> None:
        # ALMALINUX_10_64BIT declares minDiskSize 10 while the type allows 5.
        disk = resolve_root_disk(
            disk_gb=5,
            storage_type="CENTRAL",
            image=self._image(id="ALMALINUX_10_64BIT", min_disk_size_gb=10),
            type_storage_types=("CENTRAL", "LOCAL"),
        )
        assert disk == CloudRootDisk(size_gb=10, storage_type="CENTRAL")

    def test_windows_images_need_fifty_gb_and_central_only(self) -> None:
        windows = self._image(
            id="WINDOWS_SERVER_2025_STANDARD_64BIT",
            label="Windows Server 2025 Standard (x86_64)",
            os_family="windows",
            min_disk_size_gb=50,
            storage_types=("CENTRAL",),
        )
        disk = resolve_root_disk(
            disk_gb=5,
            storage_type="LOCAL",
            image=windows,
            type_storage_types=("CENTRAL", "LOCAL"),
        )
        assert disk == CloudRootDisk(size_gb=50, storage_type="CENTRAL")

    def test_an_image_without_declared_facts_falls_back_to_the_type_minimum(self) -> None:
        disk = resolve_root_disk(
            disk_gb=5,
            storage_type="CENTRAL",
            image=self._image(min_disk_size_gb=None, storage_types=()),
            type_storage_types=(),
        )
        assert disk == CloudRootDisk(size_gb=5, storage_type="CENTRAL")

    def test_a_windows_style_label_raises_the_floor_without_declared_minimum(self) -> None:
        image = self._image(
            label="Windows Server 2019 Standard (x86_64)",
            os_family="windows",
            min_disk_size_gb=None,
        )
        assert image_root_disk_floor(image) == 50
        assert image_root_disk_floor(self._image()) == 5

    def test_no_common_storage_type_fails_closed(self) -> None:
        with pytest.raises(ProviderError):
            resolve_root_disk(
                disk_gb=5,
                storage_type="LOCAL",
                image=self._image(storage_types=("CENTRAL",)),
                type_storage_types=("LOCAL",),
            )

    def test_an_unknown_storage_type_is_never_invented(self) -> None:
        with pytest.raises(ProviderError):
            resolve_root_disk(
                disk_gb=5,
                storage_type="NVME",
                image=self._image(),
                type_storage_types=(),
            )

    def test_a_size_beyond_the_provider_maximum_fails(self) -> None:
        with pytest.raises(ProviderError):
            resolve_root_disk(
                disk_gb=ROOT_DISK_MAX_GB + 1,
                storage_type="CENTRAL",
                image=self._image(),
                type_storage_types=("CENTRAL",),
            )

    def test_validate_root_disk_normalizes_and_rejects(self) -> None:
        assert validate_root_disk(size_gb=50, storage_type="central") == (50, "CENTRAL")
        with pytest.raises(ProviderError):
            validate_root_disk(size_gb=5, storage_type="CENTRAL", os_family="windows")

    def test_official_image_payload_carries_disk_and_storage_facts(self) -> None:
        from cloud_platform.providers.leaseweb.cloud import _parse_image

        parsed = _parse_image(
            {
                "id": "WINDOWS_SERVER_2025_STANDARD_64BIT",
                "name": "Windows Server 2025 Standard (x86_64)",
                "family": "windows",
                "architecture": "x86_64",
                "region": None,
                "state": "READY",
                "minDiskSize": 50,
                "storageTypes": ["CENTRAL"],
            }
        )
        assert parsed is not None
        assert parsed.min_disk_size_gb == 50
        assert parsed.storage_types == ("CENTRAL",)
        legacy = _parse_image({"id": "DEBIAN_13_64BIT", "displayName": "Debian 13"})
        assert legacy is not None
        assert legacy.min_disk_size_gb is None
        assert legacy.storage_types == ()


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
            root_disk_size_gb=5,
            root_disk_storage_type="CENTRAL",
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
            root_disk_size_gb=5,
            root_disk_storage_type="CENTRAL",
            idempotency_key=IdempotencyKey("op-2-12345678"),
        )
        assert created.id == "i-9"
        assert seen["contractType"] == "HOURLY"
        # The transmitted body carries the required disk fields and no
        # undocumented extras (a real POST with ``labels`` was rejected 400).
        assert seen["rootDiskSize"] == 5
        assert seen["rootDiskStorageType"] == "CENTRAL"
        assert "labels" not in seen

    async def test_the_post_body_is_not_sent_when_the_disk_is_invalid(self) -> None:
        provider = _provider()
        posted: list[Any] = []

        async def _request(method: str, path: str, **kwargs: Any) -> Any:
            if method == "GET":
                return {"instances": []}
            posted.append(kwargs.get("json"))
            return {"instance": {"id": "i-1", "state": "CREATING"}}

        provider._transport.request = _request  # type: ignore[method-assign]
        with pytest.raises(ProviderError):
            await provider.create_instance(
                instance_type="lsw.mini",
                image_id="UBUNTU_24_04",
                region="eu-west-3",
                reference="srv-bad",
                root_disk_size_gb=0,
                root_disk_storage_type="CENTRAL",
                idempotency_key=IdempotencyKey("op-5-12345678"),
            )
        assert posted == []

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
                root_disk_size_gb=5,
                root_disk_storage_type="CENTRAL",
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
                root_disk_size_gb=5,
                root_disk_storage_type="CENTRAL",
                idempotency_key=IdempotencyKey("op-4-12345678"),
            )
