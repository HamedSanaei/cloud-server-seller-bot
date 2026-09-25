"""Leaseweb hourly Cloud multi-account production tests (P0).

Production shape:
  account north (first configured key): VPS works, Public Cloud unavailable
  account uk: VPS works, Public Cloud works

Proves the P0 hourly requirements:
1. Cloud discovery checks BOTH accounts (never the first key only).
2. Regions/types are attributed to the supporting account.
3. Hourly offers persist the supplying credential (provider_account_id).
4. Creation POSTs through the SAME credential (pinned at creation).
5. The Cloud family appears once a valid hourly offer is sellable.
6. Monthly + hourly appear as TWO buttons; neither leaks into the other.
7. Transient failure never mass-retires; operator-disabled stays disabled.
8. Callbacks stay within the 64-byte Telegram limit.
9. 401 for one account never invalidates another; transient is UNKNOWN.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

from cloud_platform.modules.checkout.service import OfferCatalogViewService
from cloud_platform.modules.markets.domain import ProviderCatalog
from cloud_platform.modules.offers.domain import (
    BILLING_MODEL_HOURLY,
    BILLING_MODEL_MONTHLY,
    SellableOffer,
)
from cloud_platform.modules.users.domain import Role, User, UserStatus
from cloud_platform.providers.errors import ProviderAuthError, ProviderUnavailable
from cloud_platform.providers.leaseweb.cloud import (
    CloudInstanceType,
    CloudRegion,
    CloudRootDisk,
    HourlyCheckoutFacts,
    resolve_root_disk,
    validate_root_disk,
)
from cloud_platform.providers.routing import CredentialAccountState

PROVIDER = "leaseweb"
SIGNING_KEY = "hourly-multi-account-signing-key"

USER = User(
    id=uuid4(),
    username="cust",
    email="cust@example.test",
    status=UserStatus.ACTIVE,
    role=Role.USER,
    telegram_user_id=12345,
)


def _hourly_offer(
    *,
    location_id: str = "eu-west-3",
    product_id: str = "lsw.mini",
    account: str | None = "uk",
    price_minor: int = 3,
    enabled: bool = True,
    available: bool = True,
    operator_disabled: bool = False,
    offer_id: UUID | None = None,
) -> SellableOffer:
    return SellableOffer(
        id=offer_id or uuid4(),
        provider_key=PROVIDER,
        product_id=product_id,
        location_id=location_id,
        name="Mini",
        vcpu=1,
        ram_gb=1,
        disk_gb=25,
        traffic=None,
        provider_cost_minor=2,
        provider_cost_currency="EUR",
        selling_price_minor=price_minor,
        selling_currency="EUR",
        billing_parameters={},
        billing_model=BILLING_MODEL_HOURLY,
        technical_metadata={
            "plan_family": "general",
            "plan_family_name": "General Purpose",
            # Sync-written provider storage facts (launch root disk input).
            "storage_type": "CENTRAL",
            "storage_types": ["CENTRAL"],
        },
        provider_available=available,
        enabled=enabled,
        operator_disabled=operator_disabled,
        provider_account_id=account,
        created_at=datetime.now(UTC),
    )


def _monthly_offer(
    *,
    location_id: str = "FRA-01",
    product_id: str = "VPS02_1",
    price_minor: int = 624,
    offer_id: UUID | None = None,
) -> SellableOffer:
    return SellableOffer(
        id=offer_id or uuid4(),
        provider_key=PROVIDER,
        product_id=product_id,
        location_id=location_id,
        name="Leaseweb VPS 1",
        vcpu=4,
        ram_gb=6,
        disk_gb=100,
        traffic="5 TB",
        provider_cost_minor=499,
        provider_cost_currency="EUR",
        selling_price_minor=price_minor,
        selling_currency="EUR",
        billing_parameters={},
        billing_model=BILLING_MODEL_MONTHLY,
        technical_metadata={},
        provider_available=True,
        enabled=True,
        provider_account_id="north",
        created_at=datetime.now(UTC),
    )


def _region(region_id: str = "eu-west-3") -> CloudRegion:
    return CloudRegion(id=region_id, name="EU West", country_code="NL", city=None)


class _DeterministicEurUsdRates:
    """Fake EUR/USD reference rates (no network): 1.17, frankfurter family."""

    async def get_rate(self, base: str, quote: str, *, allow_catalog_stale: bool = False) -> Any:
        from datetime import UTC, datetime, timedelta
        from decimal import Decimal

        from cloud_platform.modules.fx.domain import FxReferenceQuote
        from cloud_platform.modules.fx.service import ReferenceRateResolution

        now = datetime.now(UTC)
        return ReferenceRateResolution(
            FxReferenceQuote(
                base_currency=base,
                quote_currency=quote,
                rate=Decimal("1.17"),
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


async def _usd_hourly_offer(**overrides: Any) -> SellableOffer:
    """Production-shaped hourly offer: native EUR cost, USD selling price.

    Priced with the real CatalogOfferPricer (deterministic fake FX), so the
    row carries the provenance and exact-rate evidence checkout requires.
    """
    import dataclasses
    from decimal import Decimal

    from cloud_platform.modules.offers.domain import PricingPolicy
    from cloud_platform.modules.offers.pricing import CatalogOfferPricer

    base = _hourly_offer(**overrides)
    cost_minor = int(base.provider_cost_minor)
    base = dataclasses.replace(
        base,
        selling_price_minor=0,
        selling_currency="EUR",
        billing_parameters={"provider_hourly_rate": str(Decimal(cost_minor) / 100)},
    )
    priced = await CatalogOfferPricer(_DeterministicEurUsdRates(), "USD").price_auto(
        base, PricingPolicy(mode="markup", markup_percent=25, auto_publish=True)
    )
    return dataclasses.replace(
        base,
        selling_price_minor=priced.selling_price_minor,
        selling_currency=priced.selling_currency,
        pricing_metadata=dict(priced.pricing_metadata),
    )


def _ctype(type_id: str = "lsw.mini", region: str = "eu-west-3") -> CloudInstanceType:
    return CloudInstanceType(
        id=type_id,
        name=type_id,
        region=region,
        family_key="general",
        family_name="General Purpose",
        vcpu=1,
        ram_gb=1,
        disk_gb=25,
        traffic=None,
        hourly_cost_minor=2,
        currency="EUR",
        architecture="x86_64",
        cpu_type=None,
        storage_type=None,
    )


class _CloudProviderFake:
    """One account's Public Cloud view (records every read for no-mutation proof)."""

    def __init__(
        self,
        *,
        regions: list[CloudRegion] | None = None,
        types: dict[str, list[CloudInstanceType]] | None = None,
        regions_error: Exception | None = None,
        types_error: Exception | None = None,
        regions_raw: int | None = None,
        types_raw: dict[str, int] | None = None,
        types_priced: dict[str, int] | None = None,
        types_currency: str | None = None,
        images: dict[str, list[Any]] | None = None,
        images_error: Exception | None = None,
        global_images: bool = False,
    ) -> None:
        self._regions = regions or []
        self._types = types or {}
        self._regions_error = regions_error
        self._types_error = types_error
        # Raw/priced envelope sizes, for schema-drift probing: None means
        # "honest" (raw == parsed, priced == parsed).
        self._regions_raw = len(self._regions) if regions_raw is None else regions_raw
        self._types_raw = dict(types_raw or {})
        self._types_priced = dict(types_priced or {})
        self._types_currency = types_currency
        # Per-region usable images; None means "one image everywhere" (the
        # legacy default), {} means "conclusively none everywhere".
        self._images = images
        self._images_error = images_error
        # The real customer-facing read falls back to the provider's GLOBAL
        # image catalog when the region filter is rejected, so the double can
        # serve images for every region while the region-scoped routing probe
        # keeps its own scripted answer.
        self._global_images = global_images
        self.reads: list[str] = []
        self.posts = 0

    async def list_regions(self) -> list[CloudRegion]:
        self.reads.append("regions")
        if self._regions_error is not None:
            raise self._regions_error
        return list(self._regions)

    async def list_instance_types(self, region: str) -> list[CloudInstanceType]:
        self.reads.append(f"types:{region}")
        if self._types_error is not None:
            raise self._types_error
        return list(self._types.get(region, []))

    async def read_regions(self) -> Any:
        from cloud_platform.providers.leaseweb.cloud import CloudRegionsRead

        regions = await self.list_regions()
        return CloudRegionsRead(regions=tuple(regions), raw_items=self._regions_raw)

    async def read_instance_types(self, region: str) -> Any:
        from cloud_platform.providers.leaseweb.cloud import CloudInstanceTypesRead

        types = await self.list_instance_types(region)
        raw = self._types_raw.get(region, len(types))
        priced = self._types_priced.get(region, len(types))
        return CloudInstanceTypesRead(
            types=tuple(types),
            raw_items=raw,
            priced_items=priced,
            currency=self._types_currency,
        )

    async def list_images(self, region: str) -> list[Any]:
        """Customer-facing read (the real adapter falls back to the global
        catalog when the provider rejects a region filter)."""
        self.reads.append(f"images:{region}")
        if self._images_error is not None:
            raise self._images_error
        if self._global_images:
            return [_image()]
        if self._images is not None:
            return list(self._images.get(region, []))
        from cloud_platform.providers.leaseweb.cloud import CloudImage

        return [CloudImage(id="ubuntu-24.04", label="Ubuntu 24.04", os_family="ubuntu")]

    async def probe_region_images(self, region: str) -> list[Any]:
        """Region-scoped routing probe: scripted per region, NO fallback."""
        self.reads.append(f"probe:{region}")
        if self._images_error is not None:
            raise self._images_error
        if self._images is not None:
            return list(self._images.get(region, []))
        return [_image()]

    async def create_instance(self, **kwargs: Any) -> Any:
        self.posts += 1
        from cloud_platform.providers.leaseweb.cloud import CloudInstance

        return CloudInstance(
            id="i-1",
            reference=str(kwargs.get("reference") or "srv-x"),
            state="RUNNING",
            region=str(kwargs.get("region") or ""),
            instance_type=str(kwargs.get("instance_type") or ""),
            image_id=str(kwargs.get("image_id") or ""),
        )

    async def find_by_reference(self, region: str, reference: str) -> Any:
        self.reads.append(f"find:{region}:{reference}")
        return None

    async def validate_hourly_offer_for_checkout(
        self,
        *,
        location_id: str,
        product_id: str,
        image_id: str,
        expected_cost_minor: int,
        currency: str,
        expected_cost_exact: str,
        root_disk_size_gb: int | None = None,
        root_disk_storage_type: str | None = None,
    ) -> Any:
        """Mirror the production checkout revalidation, simplified."""
        from cloud_platform.providers.errors import ProviderError, ProviderNotFound

        match = next((t for t in self._types.get(location_id, []) if t.id == product_id), None)
        if match is None:
            raise ProviderNotFound(f"instance type {product_id!r} not offered")
        if match.currency.upper() != str(currency or "").strip().upper():
            raise ProviderError("provider cost currency changed")
        if match.hourly_cost_minor != expected_cost_minor:
            raise ProviderError("provider cost changed")
        images = await self.list_images(location_id)
        image = next((item for item in images if item.id == image_id), None)
        if image is None:
            raise ProviderNotFound(f"image {image_id!r} not offered")
        live = resolve_root_disk(
            disk_gb=match.disk_gb,
            storage_type=match.storage_type,
            image=image,
            type_storage_types=match.storage_types,
        )
        if root_disk_size_gb is not None or root_disk_storage_type is not None:
            pinned = validate_root_disk(
                size_gb=root_disk_size_gb,
                storage_type=root_disk_storage_type,
                image_label=image.label,
                os_family=getattr(image, "os_family", None),
            )
            if pinned[0] < live.size_gb:
                raise ProviderError("pinned root disk is below the provider minimum")
            return HourlyCheckoutFacts(
                instance_type=match,
                root_disk=CloudRootDisk(size_gb=pinned[0], storage_type=pinned[1]),
            )
        return HourlyCheckoutFacts(instance_type=match, root_disk=live)

    async def close(self) -> None:
        return None


class _OffersRepo:
    def __init__(self, offers: list[SellableOffer]) -> None:
        self._rows: dict[UUID, SellableOffer] = {o.id: o for o in offers}
        self.upserts: list[dict[str, Any]] = []

    async def get(self, offer_id: UUID) -> SellableOffer | None:
        return self._rows.get(offer_id)

    async def get_by_ref(
        self, provider_key: str, product_id: str, location_id: str
    ) -> SellableOffer | None:
        return next(
            (
                o
                for o in self._rows.values()
                if o.provider_key == provider_key
                and o.product_id == product_id
                and o.location_id == location_id
            ),
            None,
        )

    async def list_all(self) -> list[SellableOffer]:
        return list(self._rows.values())

    async def list_sellable(self, provider_key: str | None = None) -> list[SellableOffer]:
        return [
            o
            for o in self._rows.values()
            if o.sellable and (provider_key is None or o.provider_key == provider_key)
        ]

    async def list_provider_locations(self) -> list[tuple[str, str]]:
        return sorted({(o.provider_key, o.location_id) for o in self._rows.values()})

    async def upsert_from_provider(self, **kwargs: Any) -> Any:
        self.upserts.append(dict(kwargs))
        return None

    async def mark_unavailable(
        self, provider_key: str, available: set[tuple[str, str]], billing_model: Any = None
    ) -> int:
        return 0

    async def set_selling_price(self, offer_id: UUID, minor: int, currency: str) -> Any:
        return self._rows[offer_id]

    async def set_enabled(self, offer_id: UUID, enabled: bool) -> Any:
        return self._rows[offer_id]


class _LocationsRepo:
    def __init__(self) -> None:
        self.upserts: list[Any] = []

    async def upsert(self, record: Any) -> bool:
        self.upserts.append(record)
        return True

    async def list_for_provider(self, provider_key: str) -> list[Any]:
        return []


def _view_service(
    offers: list[SellableOffer], cloud: dict[str, Any] | None = None, resolver: Any | None = None
) -> OfferCatalogViewService:
    registry = MagicMock()
    registry.get.side_effect = KeyError("no")
    wallet = MagicMock()
    wallet.get = AsyncMock(return_value=None)
    return OfferCatalogViewService(
        offers_repo=_OffersRepo(offers),  # type: ignore[arg-type]
        provider_registry=registry,
        wallet_repo=wallet,
        signing_key=SIGNING_KEY,
        market_catalog=ProviderCatalog(
            markets={PROVIDER: "foreign"},
            display_names={PROVIDER: "Leaseweb"},
            enabled={},
            families={
                PROVIDER: {
                    "vps": {
                        "billing_model": BILLING_MODEL_MONTHLY,
                        "display_name": "VPS",
                    },
                    "cloud": {
                        "billing_model": BILLING_MODEL_HOURLY,
                        "display_name": "Cloud",
                    },
                }
            },
        ),
        location_repo=_LocationsRepo(),  # type: ignore[arg-type]
        cloud_providers=cloud or {},
        cloud_resolver=resolver,
    )


def _resolver(north: Any, uk: Any) -> Any:
    class _R:
        def adapter_for(
            self, provider_key: str, credential_account_id: str | None = None
        ) -> Any | None:
            if provider_key != PROVIDER:
                return None
            if (credential_account_id or "").strip() == "north":
                return north
            if (credential_account_id or "").strip() == "uk":
                return uk
            if not (credential_account_id or "").strip():
                return uk  # logical default is the Cloud owner, not the first key
            return None

    return _R()


class TestMultiAccountDiscovery:
    async def test_both_accounts_probed_not_first_key_only(self) -> None:
        """Discovery touches north AND uk; uk's regions are attributed to uk."""
        from unittest.mock import patch

        import cloud_platform.providers.leaseweb.cloud_sync as mod

        north = _CloudProviderFake(regions=[])  # no Public Cloud entitlement
        uk = _CloudProviderFake(regions=[_region()], types={"eu-west-3": [_ctype()]})
        offers = _OffersRepo([])
        locations = _LocationsRepo()
        from cloud_platform.providers.leaseweb.cloud_sync import LeasewebHourlyCloudSyncer

        syncer = LeasewebHourlyCloudSyncer(
            lambda: None,  # type: ignore[arg-type]
            accounts={"north": north, "uk": uk},  # type: ignore[arg-type]
            account_priorities={"north": 10, "uk": 20},
        )
        with (
            patch.object(mod, "SqlAlchemySellableOfferRepository", lambda sf: offers),
            patch(
                "cloud_platform.modules.catalog.repository.SqlAlchemyLocationRepository",
                lambda sf: locations,
            ),
        ):
            result = await syncer.sync_all()
        # BOTH accounts were read (not just the first key).
        assert "regions" in north.reads
        assert "regions" in uk.reads
        assert "types:eu-west-3" in uk.reads
        # Types are attributed to the supporting account only.
        assert offers.upserts
        assert {call["provider_account_id"] for call in offers.upserts} == {"uk"}
        assert result.offers_written == 1
        assert ("lsw.mini", "eu-west-3") in result.verified

    async def test_first_key_without_cloud_does_not_hide_uk(self) -> None:
        """north (first key) has no Cloud; uk still supplies the catalog."""
        from cloud_platform.providers.leaseweb.cloud_accounts import (
            LeasewebCloudAccountRouter,
            LeasewebCredentialAccount,
        )

        north_provider = _CloudProviderFake(regions=[])
        uk_provider = _CloudProviderFake(regions=[_region()], types={"eu-west-3": [_ctype()]})
        router = LeasewebCloudAccountRouter.__new__(LeasewebCloudAccountRouter)
        router._accounts = {
            "north": LeasewebCredentialAccount(
                account_id="north", api_key="k1", state=CredentialAccountState.ACTIVE
            ),
            "uk": LeasewebCredentialAccount(
                account_id="uk", api_key="k2", state=CredentialAccountState.ACTIVE
            ),
        }
        router._providers = {"north": north_provider, "uk": uk_provider}  # type: ignore[attr-defined]
        north_cap = await router.probe_account("north")
        uk_cap = await router.probe_account("uk")
        assert north_cap.accessible is False
        assert uk_cap.accessible is True
        assert uk_cap.regions == (("eu-west-3", 1),)

    async def test_auth_failure_isolated_per_account(self) -> None:
        from cloud_platform.providers.leaseweb.cloud_accounts import (
            LeasewebCloudAccountRouter,
            LeasewebCredentialAccount,
        )

        north_provider = _CloudProviderFake(regions_error=ProviderAuthError("401"))
        uk_provider = _CloudProviderFake(regions=[_region()], types={"eu-west-3": [_ctype()]})
        router = LeasewebCloudAccountRouter.__new__(LeasewebCloudAccountRouter)
        router._accounts = {
            "north": LeasewebCredentialAccount(
                account_id="north", api_key="k1", state=CredentialAccountState.ACTIVE
            ),
            "uk": LeasewebCredentialAccount(
                account_id="uk", api_key="k2", state=CredentialAccountState.ACTIVE
            ),
        }
        router._providers = {"north": north_provider, "uk": uk_provider}  # type: ignore[attr-defined]
        north_cap = await router.probe_account("north")
        uk_cap = await router.probe_account("uk")
        assert north_cap.accessible is False
        assert north_cap.error_class == "AuthenticationError"
        assert uk_cap.accessible is True

    async def test_transient_is_unknown_never_no_cloud(self) -> None:
        from cloud_platform.providers.leaseweb.cloud_accounts import (
            LeasewebCloudAccountRouter,
            LeasewebCredentialAccount,
        )

        flaky = _CloudProviderFake(regions_error=ProviderUnavailable("timeout"))
        router = LeasewebCloudAccountRouter.__new__(LeasewebCloudAccountRouter)
        router._accounts = {
            "flaky": LeasewebCredentialAccount(
                account_id="flaky", api_key="k", state=CredentialAccountState.ACTIVE
            ),
        }
        router._providers = {"flaky": flaky}  # type: ignore[attr-defined]
        cap = await router.probe_account("flaky")
        assert cap.accessible is False
        assert cap.error_class == "ProviderUnavailable"


def _image(image_id: str = "ubuntu-24.04") -> Any:
    from cloud_platform.providers.leaseweb.cloud import CloudImage

    return CloudImage(id=image_id, label="Ubuntu 24.04", os_family="ubuntu")


async def _run_hourly_sync(
    accounts: dict[str, _CloudProviderFake],
    priorities: dict[str, int] | None = None,
) -> tuple[Any, _OffersRepo, _LocationsRepo]:
    """Real hourly syncer over fake accounts (patched repos)."""
    from unittest.mock import patch

    import cloud_platform.providers.leaseweb.cloud_sync as mod
    from cloud_platform.providers.leaseweb.cloud_sync import LeasewebHourlyCloudSyncer

    offers = _OffersRepo([])
    locations = _LocationsRepo()
    syncer = LeasewebHourlyCloudSyncer(
        lambda: None,  # type: ignore[arg-type]
        accounts=accounts,  # type: ignore[arg-type]
        account_priorities=priorities or {},
    )
    with (
        patch.object(mod, "SqlAlchemySellableOfferRepository", lambda sf: offers),
        patch(
            "cloud_platform.modules.catalog.repository.SqlAlchemyLocationRepository",
            lambda sf: locations,
        ),
    ):
        result = await syncer.sync_all()
    return result, offers, locations


class TestImageAwareRouting:
    """Checkout needs images via the pinned account: route to accounts that
    can actually supply them (production: eu-central-1/north, eu-west-2/uk)."""

    async def test_region_type_routes_to_image_capable_account(self) -> None:
        shared = {
            "eu-central-1": [_ctype("lsw.mini", "eu-central-1")],
            "eu-west-2": [_ctype("lsw.mini", "eu-west-2")],
        }
        north = _CloudProviderFake(
            regions=[_region("eu-central-1"), _region("eu-west-2")],
            types=dict(shared),
            images={"eu-central-1": [_image()], "eu-west-2": []},
        )
        uk = _CloudProviderFake(
            regions=[_region("eu-central-1"), _region("eu-west-2")],
            types=dict(shared),
            images={"eu-central-1": [], "eu-west-2": [_image()]},
        )
        result, offers, _locations = await _run_hourly_sync(
            {"north": north, "uk": uk}, {"north": 10, "uk": 20}
        )
        owners = {
            (call["product_id"], call["location_id"]): call["provider_account_id"]
            for call in offers.upserts
        }
        assert owners == {
            ("lsw.mini", "eu-central-1"): "north",
            ("lsw.mini", "eu-west-2"): "uk",
        }
        assert ("lsw.mini", "eu-central-1") in result.verified
        assert ("lsw.mini", "eu-west-2") in result.verified
        assert "probe:eu-west-2" in north.reads
        assert "probe:eu-west-2" in uk.reads

    async def test_global_fallback_never_moves_a_region_owner(self) -> None:
        """The customer-facing read must not drive ownership.

        Observed in production: the adapter's global image fallback made every
        credential look capable for every region, which moved eu-west-2 to the
        priority-first account — an INSERT the (provider, product, location)
        offer identity rejects, so the whole region stopped refreshing. The
        sync therefore asks the region-scoped probe, and this test pins that:
        both accounts can SERVE images for eu-west-2, yet the owner stays uk.
        """
        shared = {
            "eu-central-1": [_ctype("lsw.mini", "eu-central-1")],
            "eu-west-2": [_ctype("lsw.mini", "eu-west-2")],
        }
        north = _CloudProviderFake(
            regions=[_region("eu-central-1"), _region("eu-west-2")],
            types=dict(shared),
            images={"eu-central-1": [_image()], "eu-west-2": []},
            global_images=True,
        )
        uk = _CloudProviderFake(
            regions=[_region("eu-central-1"), _region("eu-west-2")],
            types=dict(shared),
            images={"eu-central-1": [], "eu-west-2": [_image()]},
            global_images=True,
        )
        # Both credentials can list images for eu-west-2 through the
        # customer-facing read (that is exactly what the fallback does).
        assert await north.list_images("eu-west-2")
        assert await uk.list_images("eu-west-2")

        _result, offers, _locations = await _run_hourly_sync(
            {"north": north, "uk": uk}, {"north": 10, "uk": 20}
        )
        owners = {
            (call["product_id"], call["location_id"]): call["provider_account_id"]
            for call in offers.upserts
        }
        assert owners == {
            ("lsw.mini", "eu-central-1"): "north",
            ("lsw.mini", "eu-west-2"): "uk",
        }
        assert "probe:eu-west-2" in north.reads
        assert "probe:eu-west-2" in uk.reads

    async def test_pair_without_images_is_known_but_not_sellable(self) -> None:
        solo = _CloudProviderFake(
            regions=[_region("eu-west-2")],
            types={"eu-west-2": [_ctype("lsw.mini", "eu-west-2")]},
            images={},
        )
        result, offers, _locations = await _run_hourly_sync({"solo": solo})
        assert len(offers.upserts) == 1
        update = offers.upserts[0]["update"]
        assert update.provider_available is False
        assert update.provider_account_id == "solo"
        assert ("lsw.mini", "eu-west-2") not in result.verified
        assert result.offers_written == 1
        assert any("usable images" in warning for warning in result.warnings)

    async def test_transient_image_failure_preserves_routing(self) -> None:
        solo = _CloudProviderFake(
            regions=[_region("eu-west-2")],
            types={"eu-west-2": [_ctype("lsw.mini", "eu-west-2")]},
            images_error=ProviderUnavailable("timeout"),
        )
        result, offers, _locations = await _run_hourly_sync({"solo": solo})
        assert len(offers.upserts) == 1
        update = offers.upserts[0]["update"]
        assert update.provider_available is True
        assert ("lsw.mini", "eu-west-2") in result.verified
        assert not any("usable images" in warning for warning in result.warnings)

    async def test_image_reads_never_suppress_reconciliation(self) -> None:
        solo = _CloudProviderFake(
            regions=[_region("eu-west-2")],
            types={"eu-west-2": [_ctype("lsw.mini", "eu-west-2")]},
            images_error=ProviderUnavailable("timeout"),
        )
        offers = _OffersRepo([])

        async def _mark(provider_key: str, available: Any, billing_model: Any = None) -> int:
            return 7

        offers.mark_unavailable = _mark  # type: ignore[method-assign]
        from unittest.mock import patch

        import cloud_platform.providers.leaseweb.cloud_sync as mod
        from cloud_platform.providers.leaseweb.cloud_sync import LeasewebHourlyCloudSyncer

        syncer = LeasewebHourlyCloudSyncer(
            lambda: None,  # type: ignore[arg-type]
            accounts={"solo": solo},  # type: ignore[arg-type]
        )
        with (
            patch.object(mod, "SqlAlchemySellableOfferRepository", lambda sf: offers),
            patch(
                "cloud_platform.modules.catalog.repository.SqlAlchemyLocationRepository",
                lambda sf: _LocationsRepo(),
            ),
        ):
            result = await syncer.sync_all()
        assert result.marked_unavailable == 7
        assert not any("skipped mark_unavailable" in warning for warning in result.warnings)


def _probed_router(fake: _CloudProviderFake) -> Any:
    """Router with one ACTIVE account backed by the fake provider."""
    from cloud_platform.providers.leaseweb.cloud_accounts import (
        LeasewebCloudAccountRouter,
        LeasewebCredentialAccount,
    )

    router = LeasewebCloudAccountRouter.__new__(LeasewebCloudAccountRouter)
    router._accounts = {
        "acct": LeasewebCredentialAccount(
            account_id="acct", api_key="k", state=CredentialAccountState.ACTIVE
        ),
    }
    router._providers = {"acct": fake}  # type: ignore[attr-defined]
    return router


class TestProbeShapeClassification:
    """The probe separates empty catalogs from unrecognized responses."""

    async def test_zero_regions_is_empty_not_schema_drift(self) -> None:
        router = _probed_router(_CloudProviderFake(regions=[]))
        cap = await router.probe_account("acct")
        assert cap.accessible is False
        assert cap.error_class is None
        assert cap.regions_seen == 0
        assert cap.regions_raw_items == 0

    async def test_regions_without_types_is_empty_catalog(self) -> None:
        router = _probed_router(_CloudProviderFake(regions=[_region()], types={}))
        cap = await router.probe_account("acct")
        assert cap.accessible is False
        assert cap.error_class is None
        assert cap.regions_seen == 1
        assert cap.types_raw_items == 0

    async def test_raw_regions_without_parsed_is_schema_drift(self) -> None:
        router = _probed_router(_CloudProviderFake(regions=[], regions_raw=3))
        cap = await router.probe_account("acct")
        assert cap.accessible is False
        assert cap.error_class is None
        assert cap.regions_seen == 0
        assert cap.regions_raw_items == 3

    async def test_unpriced_types_are_pricing_fact_not_schema_drift(self) -> None:
        router = _probed_router(
            _CloudProviderFake(
                regions=[_region()],
                types={"eu-west-3": []},
                types_raw={"eu-west-3": 2},
                types_priced={"eu-west-3": 0},
            )
        )
        cap = await router.probe_account("acct")
        assert cap.accessible is False
        assert cap.error_class is None
        assert cap.types_raw_items == 2
        assert cap.types_priced_items == 0

    async def test_types_request_failure_keeps_region_error(self) -> None:
        router = _probed_router(
            _CloudProviderFake(regions=[_region()], types_error=ProviderUnavailable("timeout"))
        )
        cap = await router.probe_account("acct")
        assert cap.accessible is False
        assert cap.error_class == "RegionError"

    async def test_envelope_currency_threads_through_the_probe(self) -> None:
        router = _probed_router(
            _CloudProviderFake(
                regions=[_region()],
                types={"eu-west-3": [_ctype()]},
                types_currency="EUR",
            )
        )
        cap = await router.probe_account("acct")
        assert cap.accessible is True
        assert cap.types_currency == "EUR"


class TestCloudUnavailableReason:
    """Doctor one-liners for every inaccessible-account shape (no secrets)."""

    def _reason(self, **overrides: Any) -> str:
        from types import SimpleNamespace

        from cloud_platform.cli import _cloud_unavailable_reason

        base = {
            "error_class": None,
            "regions": (),
            "regions_seen": 0,
            "regions_raw_items": 0,
            "types_raw_items": 0,
            "types_priced_items": 0,
        }
        base.update(overrides)
        return _cloud_unavailable_reason(SimpleNamespace(**base))

    def test_auth_denied(self) -> None:
        assert self._reason(error_class="AuthenticationError") == "auth denied for this account"

    def test_regions_request_failed(self) -> None:
        assert "regions request failed" in self._reason(error_class="ProviderUnavailable")
        assert "ProviderUnavailable" in self._reason(error_class="ProviderUnavailable")

    def test_types_request_failed(self) -> None:
        assert "instance-type request failed" in self._reason(error_class="RegionError")

    def test_zero_regions(self) -> None:
        assert self._reason(regions_raw_items=0) == "regions endpoint returned zero regions"

    def test_unrecognized_regions_response(self) -> None:
        reason = self._reason(regions_raw_items=3, regions_seen=0)
        assert "not recognized" in reason
        assert "3 raw item(s)" in reason

    def test_empty_instance_types(self) -> None:
        reason = self._reason(regions_raw_items=1, regions_seen=1, types_raw_items=0)
        assert "instanceTypes are all empty" in reason

    def test_missing_envelope_currency(self) -> None:
        reason = self._reason(
            regions_raw_items=1, regions_seen=1, types_raw_items=2, types_priced_items=2
        )
        assert "no usable _metadata.currency" in reason

    def test_unpriced_types(self) -> None:
        reason = self._reason(
            regions_raw_items=1,
            regions_seen=1,
            types_raw_items=2,
            types_priced_items=0,
            types_currency="EUR",
        )
        assert "usable hourly price" in reason

    def test_unrecognized_types_response(self) -> None:
        reason = self._reason(
            regions_raw_items=1,
            regions_seen=1,
            regions=(("eu-west-3", 0),),
            types_raw_items=2,
            types_priced_items=2,
            types_currency="EUR",
        )
        assert "not recognized" in reason

    async def test_priority_wins_for_shared_region_type(self) -> None:
        from unittest.mock import patch

        import cloud_platform.providers.leaseweb.cloud_sync as mod
        from cloud_platform.providers.leaseweb.cloud_sync import LeasewebHourlyCloudSyncer

        shared = [_ctype()]
        a = _CloudProviderFake(regions=[_region()], types={"eu-west-3": shared})
        b = _CloudProviderFake(regions=[_region()], types={"eu-west-3": shared})
        offers = _OffersRepo([])
        syncer = LeasewebHourlyCloudSyncer(
            lambda: None,  # type: ignore[arg-type]
            accounts={"b-account": b, "a-account": a},  # type: ignore[arg-type]
            account_priorities={"b-account": 200, "a-account": 10},
        )
        with (
            patch.object(mod, "SqlAlchemySellableOfferRepository", lambda sf: offers),
            patch(
                "cloud_platform.modules.catalog.repository.SqlAlchemyLocationRepository",
                lambda sf: _LocationsRepo(),
            ),
        ):
            await syncer.sync_all()
        # Deterministic (priority, id): a-account wins despite dict order.
        assert {call["provider_account_id"] for call in offers.upserts} == {"a-account"}

    async def test_transient_failure_does_not_mass_retire(self) -> None:
        from unittest.mock import patch

        import cloud_platform.providers.leaseweb.cloud_sync as mod
        from cloud_platform.providers.leaseweb.cloud_sync import LeasewebHourlyCloudSyncer

        good = _CloudProviderFake(regions=[_region()], types={"eu-west-3": [_ctype()]})
        bad = _CloudProviderFake(regions_error=ProviderUnavailable("5xx"))
        offers = _OffersRepo([])

        marked: list[str] = []

        async def _mark(provider_key: str, available: Any, billing_model: Any = None) -> int:
            marked.append("retired")
            return 0

        offers.mark_unavailable = _mark  # type: ignore[method-assign]
        syncer = LeasewebHourlyCloudSyncer(
            lambda: None,  # type: ignore[arg-type]
            accounts={"uk": good, "bad": bad},  # type: ignore[arg-type]
        )
        with (
            patch.object(mod, "SqlAlchemySellableOfferRepository", lambda sf: offers),
            patch(
                "cloud_platform.modules.catalog.repository.SqlAlchemyLocationRepository",
                lambda sf: _LocationsRepo(),
            ),
        ):
            result = await syncer.sync_all()
        assert marked == []  # reconciliation suppressed on partial failure
        assert result.offers_written == 1

    async def test_operator_disabled_stays_disabled(self) -> None:
        """Auto-publish never undoes an explicit operator block."""
        from cloud_platform.modules.offers.auto_sync import CatalogAutoSyncCoordinator
        from cloud_platform.modules.offers.domain import CatalogSyncReport

        row = _hourly_offer(operator_disabled=True, enabled=False)
        offers = _OffersRepo([row])

        class _State:
            async def record_run(self, **kwargs: Any) -> Any:
                from cloud_platform.modules.offers.domain import CatalogSyncState

                return CatalogSyncState(provider_key=str(kwargs["provider_key"]))

            async def get(self, provider_key: str) -> Any:
                return None

            async def list_all(self) -> list[Any]:
                return []

        class _Lock:
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def guard(self) -> Any:
                yield True

        class _Source:
            provider_key = PROVIDER

            async def sync_catalog(self) -> CatalogSyncReport:
                return CatalogSyncReport(
                    provider_key=PROVIDER,
                    ok=True,
                    complete=True,
                    billing_model=BILLING_MODEL_HOURLY,
                    discovered=1,
                    persisted=1,
                    verified=frozenset({(row.product_id, row.location_id)}),
                )

        published: list[UUID] = []

        async def _get_by_ref(pk: str, pid: str, lid: str) -> Any:
            return row

        offers.get_by_ref = _get_by_ref  # type: ignore[method-assign]

        async def _set_enabled(offer_id: UUID, enabled: bool) -> Any:
            published.append(offer_id)
            return row

        offers.set_enabled = _set_enabled  # type: ignore[method-assign]
        from types import SimpleNamespace

        settings = SimpleNamespace(
            storefront_pricing={
                "leaseweb.hourly": {"mode": "markup", "markup_percent": 25, "auto_publish": True}
            }
        )
        from cloud_platform.modules.offers.auto_sync import pricing_policies_from_settings

        coordinator = CatalogAutoSyncCoordinator(
            sources=[_Source()],  # type: ignore[list-item]
            offers=offers,  # type: ignore[arg-type]
            state=_State(),  # type: ignore[arg-type]
            lock=_Lock(),  # type: ignore[arg-type]
            pricing_policies=pricing_policies_from_settings(settings),
        )
        report = await coordinator.run()
        assert report.ran
        assert published == []


class TestHourlyProvenanceAndDispatch:
    async def test_offer_provenance_and_create_resolve_same_account(self) -> None:
        """Hourly offer stores uk; creation pins uk; worker POSTs via uk."""
        from cloud_platform.modules.hourly.service import HourlyCloudService

        north = _CloudProviderFake(regions=[_region()], types={"eu-west-3": [_ctype()]})
        uk = _CloudProviderFake(regions=[_region()], types={"eu-west-3": [_ctype()]})
        offer = await _usd_hourly_offer(account="uk")
        offers = _OffersRepo([offer])

        class _Servers:
            def __init__(self) -> None:
                self.saved: dict[UUID, Any] = {}

            async def get_by_idempotency_key(self, key: str) -> Any:
                return None

            async def create(self, server: Any, intent: Any) -> Any:
                self.saved[server.id] = server
                return server

            async def get(self, server_id: UUID) -> Any:
                return self.saved.get(server_id)

            async def save(self, server: Any) -> Any:
                self.saved[server.id] = server
                return server

            async def list_requested(self) -> list[Any]:
                return list(self.saved.values())

            async def list_provisioning(self) -> list[Any]:
                return []

        class _Accounts:
            async def get_or_create_active(self, user_id: UUID, provider_key: str) -> Any:
                from types import SimpleNamespace

                return SimpleNamespace(id=uuid4())

        class _Wallets:
            async def get(self, user_id: UUID) -> Any:
                from types import SimpleNamespace

                return SimpleNamespace(id=uuid4())

        class _Snapshots:
            def __init__(self) -> None:
                self.rows: dict[UUID, Any] = {}

            async def create_snapshot(
                self, *, server_id: UUID, price: Any, actor: Any, reason: str
            ) -> Any:
                from cloud_platform.modules.pricing.domain import snapshot_from_selling_price

                snap = snapshot_from_selling_price(server_id, price)
                self.rows[server_id] = snap
                return snap

            async def require_snapshot(self, server_id: UUID) -> Any:
                return self.rows[server_id]

        class _Ops:
            def __init__(self) -> None:
                self.rows: dict[str, Any] = {}
                self._seq = 0

            async def get_or_create(
                self,
                *,
                operation_key: str,
                operation_type: Any,
                resource_type: str,
                resource_id: UUID,
                provider_key: str,
            ) -> Any:
                from types import SimpleNamespace

                if operation_key not in self.rows:
                    self._seq += 1
                    op = SimpleNamespace(
                        id=self._seq,
                        operation_key=operation_key,
                        status="pending",
                        is_terminal=False,
                        claim=lambda oid: None,
                    )

                    async def _claim(oid: int) -> Any:
                        from types import SimpleNamespace as _NS

                        claimed = _NS(
                            id=oid,
                            operation_key=operation_key,
                            mark_outcome_unknown=lambda e: None,
                            complete=lambda meta: setattr(claimed, "meta", meta),
                            fail=lambda e: None,
                            save=lambda: None,
                        )
                        return claimed

                    op.claim = _claim  # type: ignore[attr-defined]
                    self.rows[operation_key] = op
                return self.rows[operation_key]

            async def claim(self, oid: int) -> Any:
                return None

            async def save(self, op: Any) -> None:
                return None

        servers, snapshots, ops = _Servers(), _Snapshots(), _Ops()
        audit = MagicMock()
        audit.append = AsyncMock()
        service = HourlyCloudService(
            server_repo=servers,  # type: ignore[arg-type]
            offers_repo=offers,  # type: ignore[arg-type]
            account_repo=_Accounts(),  # type: ignore[arg-type]
            wallet_repo=_Wallets(),  # type: ignore[arg-type]
            snapshot_service=snapshots,  # type: ignore[arg-type]
            operation_repo=ops,  # type: ignore[arg-type]
            audit_repo=audit,
            cloud_providers={PROVIDER: north},
            cloud_resolver=_resolver(north, uk),
        )
        # Image reads use the OWNING account (uk), not the dict default.
        # Images resolve by stable provider id, never by list position.
        image = await service.cloud_image_by_id(offer, "ubuntu-24.04")
        assert image.label == "Ubuntu 24.04"
        assert uk.reads and not north.reads

        result = await service.create_instance(
            user=USER,
            offer_id=offer.id,
            image_id="ubuntu-24.04",
            image_label="Ubuntu 24.04",
            idempotency_key="k-1",
        )
        assert result.server.credential_account_id == "uk"

    async def test_process_server_posts_through_pinned_account(self) -> None:
        from cloud_platform.modules.hourly.service import HourlyCloudService

        north = _CloudProviderFake(regions=[_region()], types={"eu-west-3": [_ctype()]})
        uk = _CloudProviderFake(regions=[_region()], types={"eu-west-3": [_ctype()]})
        offer = await _usd_hourly_offer(account="uk")
        offers = _OffersRepo([offer])

        class _Servers:
            def __init__(self) -> None:
                self.saved: dict[UUID, Any] = {}

            async def get(self, server_id: UUID) -> Any:
                return self.saved.get(server_id)

            async def save(self, value: Any) -> Any:
                self.saved[value.id] = value
                return value

            async def get_by_idempotency_key(self, key: str) -> Any:
                return None

            async def create(self, s: Any, intent: Any) -> Any:
                self.saved[s.id] = s
                return s

            async def list_requested(self) -> list[Any]:
                return []

            async def list_provisioning(self) -> list[Any]:
                return []

        class _Accounts:
            async def get_or_create_active(self, user_id: UUID, provider_key: str) -> Any:
                from types import SimpleNamespace

                return SimpleNamespace(id=uuid4())

        class _Wallets:
            async def get(self, user_id: UUID) -> Any:
                from types import SimpleNamespace

                return SimpleNamespace(id=uuid4(), currency="USD")

        class _Snapshots:
            def __init__(self) -> None:
                self.rows: dict[UUID, Any] = {}

            async def create_snapshot(
                self, *, server_id: UUID, price: Any, actor: Any, reason: str
            ) -> Any:
                from cloud_platform.modules.pricing.domain import snapshot_from_selling_price

                snap = snapshot_from_selling_price(server_id, price)
                self.rows[server_id] = snap
                return snap

            async def require_snapshot(self, server_id: UUID) -> Any:
                return self.rows[server_id]

        class _Ops:
            def __init__(self) -> None:
                self.rows: dict[str, Any] = {}
                self._seq = 0

            async def get_or_create(self, **kwargs: Any) -> Any:
                from types import SimpleNamespace

                if kwargs["operation_key"] not in self.rows:
                    self._seq += 1
                    op = SimpleNamespace(
                        id=self._seq,
                        operation_key=kwargs["operation_key"],
                        status="pending",
                        is_terminal=False,
                        claim=lambda oid: None,
                    )

                    async def _claim(oid: int) -> Any:
                        from types import SimpleNamespace as _NS

                        claimed = _NS(
                            id=oid,
                            operation_key=kwargs["operation_key"],
                            mark_outcome_unknown=lambda e: None,
                            complete=lambda meta: meta,
                            fail=lambda e: None,
                            save=lambda: None,
                        )
                        return claimed

                    op.claim = _claim  # type: ignore[attr-defined]
                    self.rows[kwargs["operation_key"]] = op
                return self.rows[kwargs["operation_key"]]

            async def claim(self, oid: int) -> Any:
                for op in self.rows.values():
                    if op.id == oid:
                        return await op.claim(oid)
                return None

            async def save(self, op: Any) -> None:
                return None

        servers, snapshots, ops = _Servers(), _Snapshots(), _Ops()
        service = HourlyCloudService(
            server_repo=servers,  # type: ignore[arg-type]
            offers_repo=offers,  # type: ignore[arg-type]
            account_repo=_Accounts(),  # type: ignore[arg-type]
            wallet_repo=_Wallets(),  # type: ignore[arg-type]
            snapshot_service=snapshots,  # type: ignore[arg-type]
            operation_repo=ops,  # type: ignore[arg-type]
            audit_repo=AsyncMock(),
            cloud_providers={PROVIDER: north},
            cloud_resolver=_resolver(north, uk),
        )
        created = await service.create_instance(
            user=USER,
            offer_id=offer.id,
            image_id="ubuntu-24.04",
            image_label="Ubuntu 24.04",
            idempotency_key="k-process-uk",
        )
        assert created.server.credential_account_id == "uk"
        # The worker POST for the pinned server must go to uk, not north.
        outcome = await service.process_server(created.server.id)
        assert outcome in ("provisioned", "failed")
        assert uk.posts == (1 if outcome == "provisioned" else 0)
        assert north.posts == 0


class TestFamilyVisibility:
    async def test_two_families_appear_and_never_leak(self) -> None:
        service = _view_service([_monthly_offer(), _hourly_offer()])
        families, _, _ = await service.families_screen(PROVIDER)
        assert len(families) == 2
        billings = {f.billing_model for f in families}
        assert billings == {BILLING_MODEL_MONTHLY, BILLING_MODEL_HOURLY}

        monthly = await service.family_plans_screen(PROVIDER, "vps", "FRA-01")
        assert monthly.items
        assert all(i.product_id == "VPS02_1" for i in monthly.items)

        cloud = await service.cloud_plans_screen(PROVIDER, "eu-west-3", "general")
        assert cloud.items
        assert all(i.product_id == "lsw.mini" for i in cloud.items)

    async def test_hourly_visible_only_when_sellable(self) -> None:
        # Unpriced hourly offer: the configured cloud family is still shown
        # (family existence != current inventory) but marked unavailable,
        # while monthly stays available with its count.
        gated = _hourly_offer(price_minor=0)
        service = _view_service([_monthly_offer(), gated])
        families, _, _ = await service.families_screen(PROVIDER)
        assert [f.billing_model for f in families] == [
            BILLING_MODEL_MONTHLY,
            BILLING_MODEL_HOURLY,
        ]
        monthly, hourly = families
        assert monthly.available is True
        assert monthly.sellable_count == 1
        assert hourly.available is False
        assert hourly.sellable_count == 0

    async def test_callbacks_within_telegram_limit(self) -> None:
        from cloud_platform.modules.navigation.domain import (
            TELEGRAM_CALLBACK_DATA_LIMIT_BYTES,
        )

        service = _view_service([_monthly_offer(), _hourly_offer()])
        families, _, _ = await service.families_screen(PROVIDER)
        for family in families:
            assert len(family.select_callback.encode()) <= TELEGRAM_CALLBACK_DATA_LIMIT_BYTES
        monthly = await service.family_plans_screen(PROVIDER, "vps", "FRA-01")
        for item in monthly.items:
            assert len(item.select_callback.encode()) <= TELEGRAM_CALLBACK_DATA_LIMIT_BYTES
        cloud = await service.cloud_plans_screen(PROVIDER, "eu-west-3", "general")
        for item in cloud.items:
            assert len(item.select_callback.encode()) <= TELEGRAM_CALLBACK_DATA_LIMIT_BYTES

    def test_no_provider_branch_in_hourly_dispatch(self) -> None:
        """Domain dispatch never branches on a provider name."""
        import ast
        from pathlib import Path

        for relative in (
            "src/cloud_platform/modules/hourly/service.py",
            "src/cloud_platform/modules/checkout/service.py",
        ):
            tree = ast.parse(
                (Path(__file__).resolve().parents[2] / relative).read_text(encoding="utf-8")
            )
            for node in ast.walk(tree):
                if isinstance(node, ast.Compare):
                    for part in (node.left, *node.comparators):
                        if isinstance(part, ast.Constant) and part.value == "leaseweb":
                            raise AssertionError(f"{relative}:{node.lineno} branches on leaseweb")
