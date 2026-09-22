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

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from cloud_platform.bot.monthly_ui import MonthlyBotUi
from cloud_platform.modules.catalog.domain import LocationRecord
from cloud_platform.modules.checkout.service import OfferCatalogViewService
from cloud_platform.modules.markets.domain import ProviderCatalog
from cloud_platform.modules.navigation.domain import (
    TELEGRAM_CALLBACK_DATA_LIMIT_BYTES,
    Callback,
    decode_callback,
)
from cloud_platform.modules.offers.domain import (
    BILLING_MODEL_HOURLY,
    OfferSpecUpdate,
    SellableOffer,
)
from cloud_platform.modules.users.domain import Role, User, UserStatus
from cloud_platform.providers.errors import ProviderOutcomeUnknown
from cloud_platform.providers.leaseweb.cloud import (
    CloudImage,
    CloudInstance,
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
        self, provider_key: str, available: set[tuple[str, str]], billing_model: Any = None
    ) -> int:
        changed = 0
        for key, offer in list(self._rows.items()):
            if key[0] != provider_key:
                continue
            if billing_model is not None and offer.billing_model != billing_model:
                continue
            if (key[1], key[2]) not in available and offer.provider_available:
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

    async def find_by_reference(self, region: str, reference: str) -> Any:
        return None

    async def create_instance(self, **kwargs: Any) -> CloudInstance:
        from cloud_platform.providers.leaseweb.cloud import build_create_body

        body = build_create_body(
            instance_type=kwargs["instance_type"],
            image_id=kwargs["image_id"],
            region=kwargs["region"],
            reference=kwargs["reference"],
        )
        self.posts.append(body)
        return CloudInstance(
            id="i-1",
            reference=kwargs.get("reference", ""),
            state="CREATING",
            region=kwargs.get("region", ""),
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
    def get(self, key: str) -> Any:
        raise KeyError(key)


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

    async def families_screen(self, provider_key: str) -> tuple[list[Any], str, str]:
        return await self._service.families_screen(provider_key)

    async def family_locations_screen(
        self, provider_key: str, family_key: str, page: int = 1
    ) -> Any:
        return await self._service.family_locations_screen(provider_key, family_key, page)

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
        detail = await _press(
            bot,
            next(
                b
                for b in _buttons(plans)
                if _decode(b.callback_data or "").screen == "cloud_detail"
            ).callback_data
            or "",
        )
        continuation = next(
            b for b in _buttons(detail) if _decode(b.callback_data or "").screen == "cloud_images"
        )
        images = await _press(bot, continuation.callback_data or "")
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
        offers = FakeOffersRepo([_offer()])
        cloud = FakeHourlyProvider()
        service = self._hourly_service(offers, cloud)
        offer = (await offers.list_all())[0]
        result = await service.create_instance(
            user=USER,
            offer_id=offer.id,
            image_id="UBUNTU",
            image_label="Ubuntu",
            idempotency_key="k1",
        )
        assert result.replayed is False
        assert cloud.posts == [], "no provider mutation on intent"

    async def test_replay_is_idempotent(self) -> None:
        offers = FakeOffersRepo([_offer()])
        cloud = FakeHourlyProvider()
        service = self._hourly_service(offers, cloud)
        first = await service.create_instance(
            user=USER,
            offer_id=(await offers.list_all())[0].id,
            image_id="U",
            image_label="Ubuntu",
            idempotency_key="k2",
        )
        second = await service.create_instance(
            user=USER,
            offer_id=(await offers.list_all())[0].id,
            image_id="U",
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
        offers = FakeOffersRepo([_offer()])
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

        offers = FakeOffersRepo([_offer()])
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
        offers = FakeOffersRepo([_offer()])
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
            from cloud_platform.providers.leaseweb.cloud import CloudInstance

            return CloudInstance(id="i-proven", reference=reference, state="RUNNING", region=region)

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
        return type("W", (), {"id": uuid4(), "balance": 50_000})()


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
