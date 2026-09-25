"""Hourly Cloud flow: sync, screens, creation, worker (REWORK).

- Regions/types sync into hourly offers with metadata; per-region
  isolation; retirement scoped to hourly rows only.
- Screens: locations -> plan families -> plans -> detail -> images ->
  confirm; hourly-labeled prices, monthly estimate display-only, every
  callback within budget.
- Creation persists intent + hourly snapshot without charging; the worker
  POSTs once per claimed operation; ambiguous outcomes never re-POST
  blindly (reference correlation + reconcile-or-review).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from cloud_platform.bot.monthly_ui import MonthlyBotUi
from cloud_platform.modules.catalog.domain import LocationRecord
from cloud_platform.modules.checkout.service import OfferCatalogViewService
from cloud_platform.modules.fx.domain import FxReferenceQuote
from cloud_platform.modules.fx.service import ReferenceRateResolution
from cloud_platform.modules.markets.domain import ProviderCatalog
from cloud_platform.modules.navigation.domain import (
    TELEGRAM_CALLBACK_DATA_LIMIT_BYTES,
    Callback,
    decode_callback,
    decode_offer_ref,
    encode_offer_ref,
)
from cloud_platform.modules.offers.domain import (
    BILLING_MODEL_HOURLY,
    OfferSpecUpdate,
    PricingPolicy,
    SellableOffer,
)
from cloud_platform.modules.offers.pricing import CatalogOfferPricer
from cloud_platform.modules.users.domain import Role, User, UserStatus
from cloud_platform.providers.errors import (
    ProviderError,
    ProviderNotFound,
    ProviderOutcomeUnknown,
)
from cloud_platform.providers.leaseweb.cloud import (
    CloudImage,
    CloudInstanceType,
    CloudRegion,
)

SIGNING_KEY = "hourly-flow-signing-key"
PROVIDER = "leaseweb"

USER = User(
    id=uuid4(),
    username="cust",
    email="cust@example.test",
    status=UserStatus.ACTIVE,
    role=Role.USER,
    telegram_user_id=12345,
)


def _offer(
    *,
    location_id: str = "eu-west-3",
    product_id: str = "lsw.mini",
    name: str = "Mini",
    price_minor: int = 2,
    currency: str = "EUR",
    billing_model: str = BILLING_MODEL_HOURLY,
    offer_id: UUID | None = None,
    technical_metadata: dict[str, object] | None = None,
) -> SellableOffer:
    return SellableOffer(
        id=offer_id or uuid4(),
        provider_key=PROVIDER,
        product_id=product_id,
        location_id=location_id,
        name=name,
        vcpu=1,
        ram_gb=1,
        disk_gb=25,
        traffic=None,
        provider_cost_minor=2,
        provider_cost_currency=currency,
        selling_price_minor=price_minor,
        selling_currency=currency,
        billing_parameters={},
        billing_model=billing_model,
        technical_metadata=dict(
            technical_metadata
            if technical_metadata is not None
            else {"plan_family": "general", "plan_family_name": "General Purpose"}
        ),
        provider_available=True,
        enabled=True,
        created_at=datetime.now(UTC),
    )


class _UsdRates:
    """Deterministic in-test EUR->USD reference rates (no network, no float)."""

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


async def _usd_offer(
    *,
    location_id: str = "eu-west-3",
    product_id: str = "lsw.mini",
    name: str = "Mini",
    offer_id: UUID | None = None,
    technical_metadata: dict[str, object] | None = None,
) -> SellableOffer:
    """Production-shaped hourly offer: EUR provider cost + USD selling price.

    The EUR ``_offer()`` defaults stay for rendering/listing tests; anything
    going through ``HourlyCloudService.create_instance``/``process_server``
    must use this helper so the offer is sellable in the configured catalog
    currency (USD) with valid FX pricing provenance from the real
    :class:`CatalogOfferPricer`.
    """
    base = _offer(
        location_id=location_id,
        product_id=product_id,
        name=name,
        price_minor=0,
        currency="EUR",
        billing_model=BILLING_MODEL_HOURLY,
        offer_id=offer_id,
        technical_metadata=technical_metadata,
    )
    base = replace(
        base,
        provider_cost_minor=2,
        provider_cost_currency="EUR",
        billing_parameters={"provider_hourly_rate": "0.02"},
    )
    priced = await CatalogOfferPricer(_UsdRates(Decimal("1.17")), "USD").price_auto(
        base, PricingPolicy(mode="markup", markup_percent=25)
    )
    return replace(
        base,
        selling_price_minor=priced.selling_price_minor,
        selling_currency=priced.selling_currency,
        pricing_metadata=dict(priced.pricing_metadata),
    )


class FakeOffersRepo:
    def __init__(self, offers: list[SellableOffer] | None = None) -> None:
        self._rows: dict[tuple[str, str, str], SellableOffer] = {}
        for offer in offers or []:
            self._rows[(offer.provider_key, offer.product_id, offer.location_id)] = offer

    async def get(self, offer_id: UUID) -> SellableOffer | None:
        return next((o for o in self._rows.values() if o.id == offer_id), None)

    async def get_by_ref(
        self, provider_key: str, product_id: str, location_id: str
    ) -> SellableOffer | None:
        return self._rows.get((provider_key, product_id, location_id))

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

    async def upsert_from_provider(
        self,
        *,
        provider_key: str,
        product_id: str,
        location_id: str,
        update: OfferSpecUpdate,
        provider_account_id: str | None = None,
    ) -> SellableOffer:
        from dataclasses import replace

        key = (provider_key, product_id, location_id)
        existing = self._rows.get(key)
        if existing is None:
            offer = SellableOffer(
                id=uuid4(),
                provider_key=provider_key,
                product_id=product_id,
                location_id=location_id,
                name=update.name,
                vcpu=update.vcpu,
                ram_gb=update.ram_gb,
                disk_gb=update.disk_gb,
                traffic=update.traffic,
                provider_cost_minor=update.provider_cost_minor,
                provider_cost_currency=update.provider_cost_currency,
                selling_price_minor=0,
                selling_currency=update.provider_cost_currency,
                billing_parameters=dict(update.billing_parameters),
                billing_model=update.billing_model or "hourly",
                technical_metadata=dict(update.technical_metadata or {}),
                provider_available=update.provider_available,
                enabled=False,
                provider_account_id=provider_account_id,
                created_at=None,
            )
            self._rows[key] = offer
            return offer
        updated = replace(
            existing,
            name=update.name,
            vcpu=update.vcpu,
            ram_gb=update.ram_gb,
            disk_gb=update.disk_gb,
            traffic=update.traffic,
            provider_cost_minor=update.provider_cost_minor,
            provider_cost_currency=update.provider_cost_currency,
            provider_available=update.provider_available,
        )
        self._rows[key] = updated
        return updated

    async def mark_unavailable(
        self, provider_key: str, available: set[tuple[str, ...]], billing_model: Any = None
    ) -> int:
        # Mirrors production: scoped rows need their exact account triple;
        # legacy unscoped rows keep pair semantics.
        qualified = {tuple(item) for item in available if len(tuple(item)) == 3}
        legacy = {tuple(item) for item in available if len(tuple(item)) == 2}
        changed = 0
        for key, offer in list(self._rows.items()):
            if key[0] != provider_key:
                continue
            if billing_model is not None and offer.billing_model != billing_model:
                continue
            account = str(offer.provider_account_id or "")
            triple = (account, key[1], key[2])
            if account:
                is_available = triple in qualified
            else:
                is_available = triple in qualified or (key[1], key[2]) in legacy
            if not is_available and offer.provider_available:
                from dataclasses import replace

                self._rows[key] = replace(offer, provider_available=False)
                changed += 1
        return changed

    async def set_enabled(self, offer_id: UUID, enabled: bool) -> SellableOffer:
        from dataclasses import replace

        offer = await self.get(offer_id)
        assert offer is not None
        updated = replace(offer, enabled=enabled)
        self._rows[(offer.provider_key, offer.product_id, offer.location_id)] = updated
        return updated

    async def set_operator_disabled(self, offer_id: UUID, disabled: bool) -> SellableOffer:
        from dataclasses import replace

        offer = await self.get(offer_id)
        assert offer is not None
        updated = replace(offer, operator_disabled=disabled)
        self._rows[(offer.provider_key, offer.product_id, offer.location_id)] = updated
        return updated

    async def set_auto_priced(self, offer_id: UUID, auto_priced: bool) -> SellableOffer:
        from dataclasses import replace

        offer = await self.get(offer_id)
        assert offer is not None
        updated = replace(offer, auto_priced=auto_priced)
        self._rows[(offer.provider_key, offer.product_id, offer.location_id)] = updated
        return updated

    async def set_selling_price(
        self, offer_id: UUID, selling_price_minor: int, currency: str
    ) -> SellableOffer:
        from dataclasses import replace

        offer = await self.get(offer_id)
        assert offer is not None
        updated = replace(offer, selling_price_minor=selling_price_minor, selling_currency=currency)
        self._rows[(offer.provider_key, offer.product_id, offer.location_id)] = updated
        return updated


class FakeLocationsRepo:
    def __init__(self) -> None:
        self.rows: list[LocationRecord] = []

    async def upsert(self, record: LocationRecord) -> bool:
        self.rows.append(record)
        return True

    async def list_for_provider(self, provider_key: str) -> list[LocationRecord]:
        return [r for r in self.rows if r.provider_key == provider_key]


def _cloud_type(**overrides: Any) -> CloudInstanceType:
    values: dict[str, Any] = dict(
        id="lsw.mini",
        name="Mini",
        region="eu-west-3",
        family_key="general",
        family_name="General Purpose",
        vcpu=1,
        ram_gb=1,
        disk_gb=25,
        traffic=None,
        hourly_cost_minor=2,
        currency="EUR",
        architecture="x86_64",
        cpu_type="shared",
        storage_type="ssd",
        hourly_rate_exact="0.02",
    )
    values.update(overrides)
    return CloudInstanceType(**values)


class FakeHourlyProvider:
    """Scripted hourly adapter (no network, records mutations)."""

    def __init__(
        self,
        regions: list[CloudRegion] | None = None,
        types: dict[str, list[CloudInstanceType]] | None = None,
        images: dict[str, list[CloudImage]] | None = None,
        region_error: Exception | None = None,
        type_errors: dict[str, Exception] | None = None,
    ) -> None:
        self._regions = (
            regions
            if regions is not None
            else [CloudRegion("eu-west-3", "Frankfurt", "DE", "Frankfurt")]
        )
        self._types = types or {}
        self._images = images or {}
        self._region_error = region_error
        self._type_errors = type_errors or {}
        self.posts: list[dict[str, Any]] = []

    async def list_regions(self) -> list[CloudRegion]:
        if self._region_error is not None:
            raise self._region_error
        return list(self._regions)

    async def list_instance_types(self, region: str) -> list[CloudInstanceType]:
        if region in self._type_errors:
            raise self._type_errors[region]
        return list(self._types.get(region, [_cloud_type()]))

    async def list_images(self, region: str) -> list[CloudImage]:
        return list(
            self._images.get(
                region, [CloudImage("UBUNTU_24_04", "Ubuntu 24.04", "ubuntu", "x86_64")]
            )
        )

    async def validate_hourly_offer_for_checkout(
        self,
        *,
        location_id: str,
        product_id: str,
        image_id: str,
        expected_cost_minor: int,
        currency: str,
        expected_cost_exact: str,
    ) -> CloudInstanceType:
        """Fail-closed checkout revalidation over the scripted state."""
        types = await self.list_instance_types(location_id)
        match = next((item for item in types if item.id == product_id), None)
        if match is None:
            raise ProviderNotFound(
                f"instance type {product_id!r} is not offered in {location_id!r}"
            )
        if match.currency.upper() != str(currency or "").strip().upper():
            raise ProviderError(
                f"provider cost currency changed for {product_id!r} in {location_id!r}"
            )
        if match.hourly_cost_minor != expected_cost_minor:
            raise ProviderError(f"provider cost changed for {product_id!r} in {location_id!r}")
        wanted_exact = str(expected_cost_exact or "").strip()
        if wanted_exact:
            try:
                wanted = Decimal(wanted_exact)
                live_exact = Decimal(match.hourly_rate_exact)
            except Exception:
                raise ProviderError(
                    f"provider rate for {product_id!r} is not valid Decimal text"
                ) from None
            if not wanted.is_finite() or wanted <= 0 or live_exact != wanted:
                raise ProviderError(
                    f"exact provider rate changed for {product_id!r} in {location_id!r}"
                )
        images = await self.list_images(location_id)
        if not any(image.id == image_id for image in images):
            raise ProviderNotFound(f"image {image_id!r} is not offered in {location_id!r}")
        return match

    async def find_by_reference(self, region: str, reference: str) -> Any:
        return None

    async def create_instance(self, **kwargs: Any) -> Any:
        from types import SimpleNamespace

        from cloud_platform.providers.leaseweb.cloud import build_create_body

        body = build_create_body(
            instance_type=kwargs["instance_type"],
            image_id=kwargs["image_id"],
            region=kwargs["region"],
            reference=kwargs["reference"],
        )
        self.posts.append(body)
        # The production contract requires the provider response to echo the
        # stable creation identity (type/image/region/reference); the scripted
        # response carries exactly what was requested, like a real DTO.
        return SimpleNamespace(
            id="i-1",
            reference=kwargs.get("reference", ""),
            state="RUNNING",
            region=kwargs.get("region", ""),
            instance_type=kwargs.get("instance_type", ""),
            image_id=kwargs.get("image_id", ""),
        )

    async def close(self) -> None:
        return None


def _catalog() -> ProviderCatalog:
    return ProviderCatalog(
        markets={PROVIDER: "foreign"},
        display_names={PROVIDER: "Leaseweb"},
        enabled={},
        families={
            PROVIDER: {
                "vps": {"billing_model": "prepaid_monthly_fixed", "display_name": "VPS"},
                "cloud": {"billing_model": "hourly", "display_name": "Cloud"},
            }
        },
    )


class FakeRegistry:
    """Registry double: the hourly provider is compute-capable.

    Sellability is a provider PORT capability, so the storefront must learn it
    from the adapter and not from a provider name.
    """

    def get(self, key: str) -> Any:
        from cloud_platform.providers.base import Capability

        if key != PROVIDER:
            raise KeyError(key)

        class _Provider:
            capabilities = frozenset({Capability.COMPUTE})

        provider = _Provider()
        provider.key = key  # type: ignore[attr-defined]
        return provider


class FakeWalletRepo:
    async def get(self, user_id: UUID) -> Any:
        return type("W", (), {"balance": 50_000})()


def _service(
    offers: list[SellableOffer],
    cloud: FakeHourlyProvider | None = None,
    locations: FakeLocationsRepo | None = None,
) -> OfferCatalogViewService:
    return OfferCatalogViewService(
        offers_repo=FakeOffersRepo(offers),
        provider_registry=FakeRegistry(),
        wallet_repo=FakeWalletRepo(),
        signing_key=SIGNING_KEY,
        market_catalog=_catalog(),
        location_repo=locations or FakeLocationsRepo(),
        cloud_providers={PROVIDER: cloud or FakeHourlyProvider()},
    )


class FakeView:
    def __init__(self, service: OfferCatalogViewService) -> None:
        self._service = service

    def provider_display_name(self, provider_key: str) -> str:
        return self._service.provider_display_name(provider_key)

    def markets_screen(self) -> list[Any]:
        return self._service.markets_screen()

    async def providers_screen(self, market: str) -> tuple[list[Any], str]:
        return await self._service.providers_screen(market)

    async def families_screen(self, provider_key: str) -> tuple[list[Any], str, str]:
        return await self._service.families_screen(provider_key)

    async def family_locations_screen(
        self, provider_key: str, family_key: str, page: int = 1
    ) -> Any:
        return await self._service.family_locations_screen(provider_key, family_key, page)

    async def cities_screen(self, provider_key: str, family_key: str, page: int = 1) -> Any:
        return await self._service.cities_screen(provider_key, family_key, page)

    async def city_locations_screen(
        self,
        provider_key: str,
        family_key: str,
        country_arg: str,
        city_slug: str,
        page: int = 1,
    ) -> Any:
        return await self._service.city_locations_screen(
            provider_key, family_key, country_arg, city_slug, page
        )

    async def cloud_locations_screen(
        self, provider_key: str, family_key: str, page: int = 1
    ) -> Any:
        return await self._service.cloud_locations_screen(provider_key, family_key, page)

    async def cloud_plan_families_screen(
        self, provider_key: str, family_key: str, location_id: str
    ) -> Any:
        return await self._service.cloud_plan_families_screen(provider_key, family_key, location_id)

    async def cloud_plans_screen(
        self,
        provider_key: str,
        family_key: str,
        location_id: str,
        plan_family: str,
        page: int = 1,
    ) -> Any:
        return await self._service.cloud_plans_screen(
            provider_key, family_key, location_id, plan_family, page
        )

    async def cloud_detail_screen(
        self, provider_key: str, location_id: str, product_id: str
    ) -> Any:
        return await self._service.cloud_detail_screen(provider_key, location_id, product_id)

    async def cloud_images_screen(self, offer_id: UUID) -> Any:
        return await self._service.cloud_images_screen(offer_id)

    async def cloud_image_by_index(self, offer: SellableOffer, index: int) -> Any:
        return await self._service.cloud_image_by_index(offer, index)

    async def cloud_confirmation(self, *, user_id: UUID, offer_id: UUID, image_index: int) -> Any:
        return await self._service.cloud_confirmation(
            user_id=user_id, offer_id=offer_id, image_index=image_index
        )

    async def products_screen(self, provider_key: str) -> tuple[list[Any], str, str]:
        return await self._service.products_screen(provider_key)

    async def product_locations_screen(
        self,
        provider_key: str,
        product_id: str,
        price_minor: int | None = None,
        currency: str | None = None,
    ) -> tuple[list[Any], str, str]:
        return await self._service.product_locations_screen(
            provider_key, product_id, price_minor, currency
        )

    async def plans_screen(
        self, location_id: str, provider_key: str | None = None
    ) -> tuple[list[Any], str, str]:
        return await self._service.plans_screen(location_id, provider_key)

    async def os_screen(self, *, offer_id: UUID) -> tuple[Any, list[Any], str, str]:
        return await self._service.os_screen(offer_id=offer_id)

    async def os_by_index(self, offer: SellableOffer, index: int) -> str:
        return await self._service.os_by_index(offer, index)

    async def confirmation(
        self,
        *,
        user_id: UUID,
        offer_id: UUID,
        os_index: int,
        panel_index: int | None = None,
    ) -> Any:
        return await self._service.confirmation(
            user_id=user_id, offer_id=offer_id, os_index=os_index, panel_index=panel_index
        )

    async def os_options(self, offer: SellableOffer) -> list[Any]:
        return await self._service.os_options(offer)


class FakeCheckout:
    async def create_order(self, **kwargs: Any) -> Any:
        raise AssertionError("monthly checkout unused in cloud flow")


class FakeServers:
    async def list_by_user(self, user_id: UUID) -> list[Any]:
        return []

    async def get(self, server_id: UUID) -> None:
        return None


class FakeOrders:
    async def get_by_server(self, server_id: UUID) -> None:
        return None


class FakeRenewals:
    async def get(self, server_id: UUID) -> None:
        return None


class FakeWalletHistory:
    async def balance(self, user_id: UUID) -> Any:
        return type(
            "View",
            (),
            {
                "has_wallet": True,
                "balance_minor": 50_000,
                "currency": "EUR",
                "formatted": "€500.00",
            },
        )()

    async def history(self, user_id: UUID, limit: int = 20) -> Any:
        return type("Page", (), {"items": []})()


class FakeHourly:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create_instance(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return type("Result", (), {"server": type("S", (), {"id": uuid4()})(), "replayed": False})()


def _ui(service: OfferCatalogViewService, hourly: FakeHourly | None = None) -> MonthlyBotUi:
    return MonthlyBotUi(
        SIGNING_KEY,
        offers_view=FakeView(service),  # type: ignore[arg-type]
        checkout=FakeCheckout(),  # type: ignore[arg-type]
        servers=FakeServers(),  # type: ignore[arg-type]
        orders=FakeOrders(),  # type: ignore[arg-type]
        renewals=FakeRenewals(),  # type: ignore[arg-type]
        offers_repo=FakeOffersRepo([]),
        wallet_history=FakeWalletHistory(),  # type: ignore[arg-type]
        hourly=hourly or FakeHourly(),  # type: ignore[arg-type]
    )


def _decode(data: str) -> Callback:
    return decode_callback(data, SIGNING_KEY)


def _buttons(screen: Any) -> list[Any]:
    return [button for row in screen.keyboard.inline_keyboard for button in row]


def _size(data: str) -> int:
    return len(data.encode("utf-8"))


def _assert_fits(data: str) -> None:
    assert _size(data) <= TELEGRAM_CALLBACK_DATA_LIMIT_BYTES, (
        f"callback_data exceeds 64 bytes: {data!r}"
    )


async def _press(ui: MonthlyBotUi, callback: str) -> Any:
    screen = await ui.handle(callback, user=USER)
    assert screen is not None
    return screen


def _pick(
    screen: Any,
    *,
    text: str | None = None,
    target: str | None = None,
    args: tuple[str, ...] | None = None,
) -> str:
    """The callback_data of the first button matching a text/screen/args filter.

    Every returned callback is size-checked, because it is exactly what the
    bot would put on a live Telegram button.
    """
    for button in _buttons(screen):
        data = button.callback_data or ""
        if text is not None and text not in button.text:
            continue
        callback = _decode(data)
        if target is not None and callback.screen != target:
            continue
        if args is not None and callback.args != args:
            continue
        _assert_fits(data)
        return data
    raise AssertionError(
        f"no button matches text={text!r} screen={target!r} args={args!r}: "
        f"{[b.text for b in _buttons(screen)]}"
    )


class TestCloudSync:
    async def test_regions_and_types_become_hourly_offers(self) -> None:
        from cloud_platform.providers.leaseweb.cloud_sync import LeasewebHourlyCloudSyncer

        repo = FakeOffersRepo()
        locations = FakeLocationsRepo()
        provider = FakeHourlyProvider(
            regions=[CloudRegion("eu-west-3", "Frankfurt", "DE", "Frankfurt")],
            types={"eu-west-3": [_cloud_type(), _cloud_type(id="lsw.big", name="Big")]},
        )

        class _Syncer(LeasewebHourlyCloudSyncer):
            pass

        syncer = _Syncer.__new__(_Syncer)
        syncer._session_factory = None
        syncer._provider = provider
        import unittest.mock as mock

        import cloud_platform.providers.leaseweb.cloud_sync as mod

        with (
            mock.patch.object(mod, "SqlAlchemySellableOfferRepository", lambda sf: repo),
            mock.patch(
                "cloud_platform.modules.catalog.repository.SqlAlchemyLocationRepository",
                lambda sf: locations,
            ),
        ):
            result = await syncer.sync_all()
        assert result.offers_written == 2
        assert result.marked_unavailable == 0
        assert len(result.verified) == 2
        row = await repo.get_by_ref(PROVIDER, "lsw.mini", "eu-west-3")
        assert row is not None
        assert row.billing_model == "hourly"
        assert row.provider_cost_minor == 2
        assert locations.rows, "location metadata persisted"

    async def test_region_failure_isolates_without_retiring(self) -> None:
        from cloud_platform.providers.errors import ProviderError
        from cloud_platform.providers.leaseweb.cloud_sync import LeasewebHourlyCloudSyncer

        repo = FakeOffersRepo([_offer()])
        locations = FakeLocationsRepo()
        provider = FakeHourlyProvider(
            regions=[
                CloudRegion("eu-west-3", "Frankfurt", "DE", "Frankfurt"),
                CloudRegion("eu-west-1", "Amsterdam", "NL", "Amsterdam"),
            ],
            types={"eu-west-3": [_cloud_type()]},
            type_errors={"eu-west-1": ProviderError("boom")},
        )
        syncer = LeasewebHourlyCloudSyncer.__new__(LeasewebHourlyCloudSyncer)
        syncer._session_factory = None
        syncer._provider = provider
        import unittest.mock as mock

        import cloud_platform.providers.leaseweb.cloud_sync as mod

        location_repo = "cloud_platform.modules.catalog.repository.SqlAlchemyLocationRepository"
        with (
            mock.patch.object(mod, "SqlAlchemySellableOfferRepository", lambda sf: repo),
            mock.patch(location_repo, lambda sf: locations),
        ):
            result = await syncer.sync_all()
        assert result.offers_written == 1
        assert result.marked_unavailable == 0  # partial view never retires
        assert any(r.error for r in result.regions)

    async def test_retirement_is_scoped_to_hourly(self) -> None:
        from cloud_platform.modules.offers.domain import BILLING_MODEL_MONTHLY
        from cloud_platform.providers.leaseweb.cloud_sync import LeasewebHourlyCloudSyncer

        monthly = _offer()
        import dataclasses

        monthly = dataclasses.replace(monthly, billing_model=BILLING_MODEL_MONTHLY)
        repo = FakeOffersRepo([monthly])
        locations = FakeLocationsRepo()
        provider = FakeHourlyProvider(
            regions=[CloudRegion("eu-west-3", "Frankfurt", "DE", "Frankfurt")],
            types={"eu-west-3": []},
        )
        syncer = LeasewebHourlyCloudSyncer.__new__(LeasewebHourlyCloudSyncer)
        syncer._session_factory = None
        syncer._provider = provider
        import unittest.mock as mock

        import cloud_platform.providers.leaseweb.cloud_sync as mod

        location_repo = "cloud_platform.modules.catalog.repository.SqlAlchemyLocationRepository"
        with (
            mock.patch.object(mod, "SqlAlchemySellableOfferRepository", lambda sf: repo),
            mock.patch(location_repo, lambda sf: locations),
        ):
            await syncer.sync_all()
        row = await repo.get_by_ref(PROVIDER, monthly.product_id, monthly.location_id)
        assert row is not None
        assert row.provider_available is True  # monthly untouched by hourly retire
        assert row.billing_model == BILLING_MODEL_MONTHLY


class TestCloudScreens:
    async def test_family_selector_lists_only_configured_families(self) -> None:
        from cloud_platform.modules.offers.domain import BILLING_MODEL_MONTHLY

        service = _service(
            [
                _offer(
                    location_id="FRA-01",
                    product_id="VPS02_1",
                    name="VPS",
                    billing_model=BILLING_MODEL_MONTHLY,
                ),
                _offer(),
            ]
        )
        families, _, _ = await service.families_screen(PROVIDER)
        assert [f.family_key for f in families] == ["vps", "cloud"]

    async def test_cloud_locations_come_from_offers(self) -> None:
        service = _service([_offer()])
        view = await service.cloud_locations_screen(PROVIDER, "cloud", 1)
        assert [item.location_id for item in view.items] == ["eu-west-3"]

    async def test_plan_families_come_from_provider_data(self) -> None:
        compute_meta = {"plan_family": "compute", "plan_family_name": "Compute Optimized"}
        general_meta = {"plan_family": "general", "plan_family_name": "General Purpose"}
        service = _service(
            [
                _offer(product_id="lsw.mini", technical_metadata=compute_meta),
                _offer(product_id="lsw.big", technical_metadata=general_meta),
                _offer(product_id="lsw.odd", technical_metadata={}),
            ]
        )
        families, _, _ = await service.cloud_plan_families_screen(PROVIDER, "cloud", "eu-west-3")
        assert [(f.family_key, f.display_name) for f in families] == [
            ("compute", "Compute Optimized"),
            ("general", "General Purpose"),
            ("other", "other"),
        ]

    async def test_cloud_plans_show_hourly_price(self) -> None:
        bot = _ui(_service([_offer()]))
        locations = await _press(
            bot, bot._callback("store", "cloud_locations", PROVIDER, "cloud", "1")
        )
        families = await _press(
            bot, next(b for b in _buttons(locations) if "eu-west-3" in b.text).callback_data or ""
        )
        family_button = next(b for b in _buttons(families) if "General" in b.text)
        plans = await _press(bot, family_button.callback_data or "")
        rows = [b for b in _buttons(plans) if "€0.02" in b.text]
        assert rows, [b.text for b in _buttons(plans)]
        assert all("ساعت" in b.text for b in rows)
        # A concrete plan opens the OS picker directly (no detail hop).
        assert all(_decode(b.callback_data or "").screen == "cloud_images" for b in rows)

    async def test_cloud_detail_shows_estimate_and_continue(self) -> None:
        bot = _ui(_service([_offer()]))
        detail = await _press(
            bot, bot._callback("store", "cloud_detail", PROVIDER, "eu-west-3", "lsw.mini")
        )
        assert "Mini" in detail.text
        assert "CPU" in detail.text or "vCPU" in detail.text
        continuation = next(
            b for b in _buttons(detail) if _decode(b.callback_data or "").screen == "cloud_images"
        )
        images = await _press(bot, continuation.callback_data or "")
        assert any("Ubuntu" in b.text for b in _buttons(images))

    async def test_browsing_never_mutates_the_provider(self) -> None:
        cloud = FakeHourlyProvider()
        bot = _ui(_service([_offer()], cloud=cloud))
        locations = await _press(
            bot, bot._callback("store", "cloud_locations", PROVIDER, "cloud", "1")
        )
        families = await _press(
            bot, next(b for b in _buttons(locations) if "eu-west-3" in b.text).callback_data or ""
        )
        plans = await _press(
            bot, next(b for b in _buttons(families) if "General" in b.text).callback_data or ""
        )
        images = await _press(
            bot,
            next(
                b
                for b in _buttons(plans)
                if _decode(b.callback_data or "").screen == "cloud_images"
            ).callback_data
            or "",
        )
        image_button = next(
            b for b in _buttons(images) if _decode(b.callback_data or "").screen == "cloud_confirm"
        )
        await _press(bot, image_button.callback_data or "")
        assert cloud.posts == [], "browsing menus must never POST to the provider"

    async def test_hourly_confirmation_says_hourly(self) -> None:
        bot = _ui(_service([_offer()]))
        detail = await _press(
            bot, bot._callback("store", "cloud_detail", PROVIDER, "eu-west-3", "lsw.mini")
        )
        continuation = next(
            b for b in _buttons(detail) if _decode(b.callback_data or "").screen == "cloud_images"
        )
        images = await _press(bot, continuation.callback_data or "")
        image_button = next(
            b for b in _buttons(images) if _decode(b.callback_data or "").screen == "cloud_confirm"
        )
        confirm = await _press(bot, image_button.callback_data or "")
        assert "ساعت" in confirm.text
        assert "ماه" in confirm.text  # monthly estimate, display only
        assert "Resource" in confirm.text  # delete-to-stop-billing warning
        assert "stop billing" not in confirm.text.lower()

    async def test_monthly_locations_exclude_hourly_offers(self) -> None:
        from cloud_platform.modules.offers.domain import BILLING_MODEL_MONTHLY

        service = _service(
            [
                _offer(),
                _offer(
                    location_id="FRA-01",
                    product_id="VPS02_1",
                    name="VPS",
                    billing_model=BILLING_MODEL_MONTHLY,
                ),
            ]
        )
        view = await service.family_locations_screen(PROVIDER, "vps", 1)
        assert [item.location_id for item in view.items] == ["FRA-01"]


class TestHourlyPlanToOsPath:
    """The real customer click path: market -> ... -> concrete plan -> OS.

    Every callback used below was generated by the PREVIOUS screen; only the
    initial market entry is constructed by the test. The old
    ``cloud_detail`` route is exercised separately as a legacy path.
    """

    async def _to_plan_rows(self, bot: MonthlyBotUi) -> Any:
        """Walk the storefront down to one instance family's plan rows.

        Every step presses the callback the PREVIOUS screen generated.
        """
        market = await _press(bot, bot._callback("store", "market"))
        providers = await _press(bot, _pick(market, target="providers", args=("foreign",)))
        families = await _press(bot, _pick(providers, target="families", args=(PROVIDER,)))
        locations = await _press(bot, _pick(families, target="family", args=(PROVIDER, "cloud")))
        instance_families = await _press(bot, _pick(locations, text="eu-west-3"))
        return await _press(bot, _pick(instance_families, text="General"))

    async def test_concrete_plan_opens_the_os_picker_directly(self) -> None:
        offer = _offer()
        cloud = FakeHourlyProvider()
        bot = _ui(_service([offer], cloud=cloud))
        plans = await self._to_plan_rows(bot)
        plan_buttons = [
            b for b in _buttons(plans) if _decode(b.callback_data or "").screen == "cloud_images"
        ]
        assert plan_buttons, [b.text for b in _buttons(plans)]
        assert not [
            b for b in _buttons(plans) if _decode(b.callback_data or "").screen == "cloud_detail"
        ], "new plan buttons must skip the redundant detail screen"
        for button in plan_buttons:
            callback = _decode(button.callback_data or "")
            assert callback.flow == "store"
            assert callback.screen == "cloud_images"
            # A compact, reversible offer reference — never a raw UUID.
            assert len(callback.args) == 1
            assert len(callback.args[0]) < 36
            assert decode_offer_ref(callback.args[0]) == offer.id
            _assert_fits(button.callback_data or "")
        images = await _press(bot, plan_buttons[0].callback_data or "")
        assert any(
            _decode(b.callback_data or "").screen == "cloud_confirm" for b in _buttons(images)
        ), "clicking a concrete plan must render real OS buttons"
        assert cloud.posts == [], "reaching the OS picker must not POST to the provider"

    async def test_os_screen_summarises_the_selected_plan(self) -> None:
        bot = _ui(_service([_offer(name="Mini")]))
        plans = await self._to_plan_rows(bot)
        images = await _press(bot, _pick(plans, target="cloud_images"))
        assert "Mini" in images.text
        assert "1 vCPU" in images.text
        assert "1 GB" in images.text
        assert "25 GB" in images.text
        assert "€0.02" in images.text
        assert "ساعت" in images.text  # the authoritative hourly selling price
        assert "Ubuntu" in " ".join(b.text for b in _buttons(images))

    async def test_os_selection_reaches_hourly_confirmation(self) -> None:
        bot = _ui(_service([_offer()]))
        plans = await self._to_plan_rows(bot)
        images = await _press(bot, _pick(plans, target="cloud_images"))
        ubuntu = _pick(images, text="Ubuntu", target="cloud_confirm")
        confirm = await _press(bot, ubuntu)
        assert "ساعت" in confirm.text  # hourly basis
        assert "ماه" in confirm.text  # display-only monthly estimate
        assert "Ubuntu" in confirm.text
        create = _pick(confirm, target="cloud_buy")
        assert create, "confirmation must offer the explicit create action"

    async def test_back_from_the_os_picker_returns_to_the_same_plan_family(self) -> None:
        bot = _ui(_service([_offer()]))
        plans = await self._to_plan_rows(bot)
        images = await _press(bot, _pick(plans, target="cloud_images"))
        back = _pick(images, target="cloud_plans")
        assert _decode(back).args == (PROVIDER, "eu-west-3", "general", "1")
        back_screen = await _press(bot, back)
        assert "€0.02" in " ".join(b.text for b in _buttons(back_screen)), (
            "back must reopen the same hourly plan list, not the monthly flow"
        )

    async def test_provider_without_images_says_so_specifically(self) -> None:
        cloud = FakeHourlyProvider(images={"eu-west-3": []})
        bot = _ui(_service([_offer()], cloud=cloud))
        plans = await self._to_plan_rows(bot)
        screen = await _press(bot, _pick(plans, target="cloud_images"))
        assert screen.text.strip() == ("در حال حاضر سیستم‌عامل قابل نصب برای این پلن در دسترس نیست.")
        assert screen.text.strip() != "این آفر در دسترس نیست."

    async def test_image_list_is_filtered_by_plan_architecture(self) -> None:
        arm = _offer(
            technical_metadata={
                "plan_family": "general",
                "plan_family_name": "General Purpose",
                "architecture": "arm64",
            }
        )
        cloud = FakeHourlyProvider(
            images={
                "eu-west-3": [
                    CloudImage("UBUNTU_X86", "Ubuntu 24.04", "ubuntu", "x86_64"),
                    CloudImage("UBUNTU_ARM", "Ubuntu 24.04 ARM", "ubuntu", "arm64"),
                    CloudImage("UNKNOWN_ARCH", "Debian 12", "debian", None),
                ]
            }
        )
        bot = _ui(_service([arm], cloud=cloud))
        plans = await self._to_plan_rows(bot)
        images = await _press(bot, _pick(plans, target="cloud_images"))
        labels = [b.text for b in _buttons(images)]
        assert "Ubuntu 24.04 ARM (arm64)" in labels
        assert "Debian 12" in labels  # unstated architecture must not vanish
        assert "Ubuntu 24.04 (x86_64)" not in labels

    async def test_tampered_offer_ref_fails_closed(self) -> None:
        bot = _ui(_service([_offer()]))
        screen = await _press(bot, bot._callback("store", "cloud_images", "zzzzzzzz"))
        assert screen.text.strip() == (
            "این دکمه معتبر نیست (منقضی یا دستکاری شده). از منوی اصلی دوباره شروع کنید."
        )
        assert not [
            b for b in _buttons(screen) if _decode(b.callback_data or "").screen == "cloud_confirm"
        ]
        # A signed but unknown offer id is just as dead an end.
        unknown = await _press(
            bot, bot._callback("store", "cloud_images", encode_offer_ref(uuid4()))
        )
        assert unknown.text.strip() == "این آفر در دسترس نیست."
        assert not [
            b for b in _buttons(unknown) if _decode(b.callback_data or "").screen == "cloud_confirm"
        ]

    async def test_legacy_cloud_detail_callbacks_still_work(self) -> None:
        """Buttons already sitting in customer chats must keep decoding."""
        bot = _ui(_service([_offer()]))
        detail = await _press(
            bot, bot._callback("store", "cloud_detail", PROVIDER, "eu-west-3", "lsw.mini")
        )
        assert "Mini" in detail.text
        images = await _press(bot, _pick(detail, target="cloud_images"))
        assert any(
            _decode(b.callback_data or "").screen == "cloud_confirm" for b in _buttons(images)
        )


class TestHourlyCreate:
    def _hourly_service(self, offers: FakeOffersRepo, cloud: FakeHourlyProvider) -> Any:
        from cloud_platform.modules.hourly.service import HourlyCloudService

        return HourlyCloudService(
            server_repo=FakeServerRepo(),
            offers_repo=offers,  # type: ignore[arg-type]
            account_repo=FakeAccountRepo(),
            wallet_repo=FakeWalletRepo2(),
            snapshot_service=FakeSnapshots(),
            operation_repo=FakeOpsRepo(),
            audit_repo=FakeAuditRepo(),
            cloud_providers={PROVIDER: cloud},
        )

    async def test_intent_persists_snapshot_without_charging(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeHourlyProvider()
        service = self._hourly_service(offers, cloud)
        offer = (await offers.list_all())[0]
        result = await service.create_instance(
            user=USER,
            offer_id=offer.id,
            image_id="UBUNTU_24_04",
            image_label="Ubuntu",
            idempotency_key="k1",
        )
        assert result.replayed is False
        assert cloud.posts == [], "no provider mutation on intent"

    async def test_replay_is_idempotent(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeHourlyProvider()
        service = self._hourly_service(offers, cloud)
        first = await service.create_instance(
            user=USER,
            offer_id=(await offers.list_all())[0].id,
            image_id="UBUNTU_24_04",
            image_label="Ubuntu",
            idempotency_key="k2",
        )
        second = await service.create_instance(
            user=USER,
            offer_id=(await offers.list_all())[0].id,
            image_id="UBUNTU_24_04",
            image_label="Ubuntu",
            idempotency_key="k2",
        )
        assert second.replayed is True
        assert second.server.id == first.server.id

    async def test_monthly_offer_rejected(self) -> None:
        from cloud_platform.modules.hourly.service import HourlyNotAvailableError
        from cloud_platform.modules.offers.domain import BILLING_MODEL_MONTHLY

        monthly = _offer()
        import dataclasses

        monthly = dataclasses.replace(monthly, billing_model=BILLING_MODEL_MONTHLY)
        offers = FakeOffersRepo([monthly])
        service = self._hourly_service(offers, FakeHourlyProvider())
        try:
            await service.create_instance(
                user=USER,
                offer_id=monthly.id,
                image_id="U",
                image_label="Ubuntu",
                idempotency_key="k3",
            )
        except HourlyNotAvailableError:
            return
        raise AssertionError("monthly offer must not create hourly")

    async def test_process_posts_once_then_reconciles(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeHourlyProvider()
        service = self._hourly_service(offers, cloud)
        offer = (await offers.list_all())[0]
        result = await service.create_instance(
            user=USER,
            offer_id=offer.id,
            image_id="UBUNTU_24_04",
            image_label="Ubuntu 24.04",
            idempotency_key="k4",
        )
        outcome = await service.process_server(result.server.id)
        assert outcome == "provisioned"
        assert len(cloud.posts) == 1
        assert cloud.posts[0]["contractType"] == "HOURLY"
        # Second run: terminal operation, no second POST.
        assert await service.process_server(result.server.id) in ("skipped", "provisioned")
        assert len(cloud.posts) == 1

    async def test_ambiguous_post_never_reposts_blindly(self) -> None:

        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeHourlyProvider()

        async def _flaky(**kwargs: Any) -> Any:
            cloud.posts.append(kwargs)
            raise _OutcomeUnknown()

        cloud.create_instance = _flaky  # type: ignore[method-assign]
        service = self._hourly_service(offers, cloud)
        offer = (await offers.list_all())[0]
        result = await service.create_instance(
            user=USER,
            offer_id=offer.id,
            image_id="UBUNTU_24_04",
            image_label="Ubuntu 24.04",
            idempotency_key="k5",
        )
        assert await service.process_server(result.server.id) == "outcome-unknown"
        assert len(cloud.posts) == 1
        # Reconcile with nothing found leaves it unknown (operator reviews).
        assert await service.reconcile_server(result.server.id) == "still-unknown"
        assert len(cloud.posts) == 1

    async def test_reconcile_attaches_proven_reference(self) -> None:
        offers = FakeOffersRepo([await _usd_offer()])
        cloud = FakeHourlyProvider()
        service = self._hourly_service(offers, cloud)
        offer = (await offers.list_all())[0]
        result = await service.create_instance(
            user=USER,
            offer_id=offer.id,
            image_id="UBUNTU_24_04",
            image_label="Ubuntu 24.04",
            idempotency_key="k6",
        )

        async def _flaky(**kwargs: Any) -> Any:
            cloud.posts.append(kwargs)
            raise _OutcomeUnknown()

        cloud.create_instance = _flaky  # type: ignore[method-assign]
        assert await service.process_server(result.server.id) == "outcome-unknown"

        async def _found(region: str, reference: str) -> Any:
            from types import SimpleNamespace

            return SimpleNamespace(
                id="i-proven",
                reference=reference,
                state="RUNNING",
                region=region,
                instance_type=offer.product_id,
                image_id="UBUNTU_24_04",
            )

        cloud.find_by_reference = _found  # type: ignore[method-assign]
        assert await service.reconcile_server(result.server.id) == "attached"


class _OutcomeUnknown(ProviderOutcomeUnknown):
    pass


class FakeServerRepo:
    def __init__(self) -> None:
        self.servers: dict[UUID, Any] = {}
        self.by_key: dict[str, Any] = {}

    async def get(self, server_id: UUID) -> Any:
        return self.servers.get(server_id)

    async def get_by_idempotency_key(self, key: str) -> Any:
        return self.by_key.get(key)

    async def create(self, server: Any, intent: Any) -> Any:
        if intent.idempotency_key in self.by_key:
            from cloud_platform.modules.compute.domain import ServerCreateError

            raise ServerCreateError("duplicate key")
        self.servers[server.id] = server
        self.by_key[intent.idempotency_key] = server
        return server

    async def save(self, server: Any) -> Any:
        self.servers[server.id] = server
        return server

    async def list_requested(self) -> list[Any]:
        from cloud_platform.modules.compute.domain import ServerLifecycleState

        return [s for s in self.servers.values() if s.state is ServerLifecycleState.REQUESTED]

    async def list_provisioning(self) -> list[Any]:
        from cloud_platform.modules.compute.domain import ServerLifecycleState

        return [s for s in self.servers.values() if s.state is ServerLifecycleState.PROVISIONING]


class FakeAccountRepo:
    async def get_or_create_active(self, user_id: UUID, provider_key: str) -> Any:
        return type("A", (), {"id": uuid4()})()


class FakeWalletRepo2:
    async def get(self, user_id: UUID) -> Any:
        return type("W", (), {"id": uuid4(), "balance": 50_000, "currency": "USD"})()


class FakeSnapshots:
    def __init__(self) -> None:
        self.created: list[Any] = []

    async def create_snapshot(self, *, server_id: UUID, price: Any, actor: Any, reason: str) -> Any:
        self.created.append((server_id, price))
        return price

    async def require_snapshot(self, server_id: UUID) -> Any:
        for sid, price in self.created:
            if sid == server_id:
                return price
        raise LookupError("no snapshot")


class FakeOpsRepo:
    def __init__(self) -> None:
        self.ops: dict[str, Any] = {}

    async def get_or_create(self, **kwargs: Any) -> Any:
        from cloud_platform.modules.operations.domain import (
            Operation,
            OperationStatus,
        )

        key = kwargs["operation_key"]
        if key in self.ops:
            return self.ops[key]
        op = Operation(
            id=uuid4(),
            operation_key=key,
            operation_type=kwargs["operation_type"],
            resource_type=kwargs["resource_type"],
            resource_id=kwargs["resource_id"],
            provider_key=kwargs["provider_key"],
            status=OperationStatus.PENDING,
        )
        self.ops[key] = op
        return op

    async def get_by_key(self, key: str) -> Any:
        return self.ops.get(key)

    async def claim(self, operation_id: UUID) -> Any:
        for op in self.ops.values():
            if op.id == operation_id and op.status.value == "pending":
                op.mark_in_flight()
                return op
        return None

    async def save(self, operation: Any) -> Any:
        self.ops[operation.operation_key] = operation
        return operation


class FakeAuditRepo:
    async def append(self, event: Any) -> Any:
        return event
