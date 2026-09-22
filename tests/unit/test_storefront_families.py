"""Commercial product families: VPS monthly vs Cloud hourly (REWORK).

- Selecting Leaseweb shows exactly its two configured families.
- Monthly offers never surface in hourly screens and vice versa.
- A single-family provider enters its flow directly (no dead selector).
- Locations, plans and details are provider-derived; flags come from
  synced country codes; the UI never branches on a provider name.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from cloud_platform.bot.monthly_ui import MonthlyBotUi
from cloud_platform.modules.catalog.domain import LocationRecord
from cloud_platform.modules.checkout.service import OfferCatalogViewService
from cloud_platform.modules.markets.domain import ProviderCatalog
from cloud_platform.modules.navigation.domain import Callback, decode_callback
from cloud_platform.modules.offers.domain import (
    BILLING_MODEL_HOURLY,
    BILLING_MODEL_MONTHLY,
    SellableOffer,
)
from cloud_platform.modules.users.domain import Role, User, UserStatus

SIGNING_KEY = "families-signing-key"
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
    location_id: str,
    product_id: str = "VPS02_1",
    name: str = "Leaseweb VPS 1",
    price_minor: int = 624,
    currency: str = "EUR",
    billing_model: str = BILLING_MODEL_MONTHLY,
    offer_id: UUID | None = None,
) -> SellableOffer:
    return SellableOffer(
        id=offer_id or uuid4(),
        provider_key=PROVIDER,
        product_id=product_id,
        location_id=location_id,
        name=name,
        vcpu=4,
        ram_gb=6,
        disk_gb=100,
        traffic="5 TB",
        provider_cost_minor=499,
        provider_cost_currency=currency,
        selling_price_minor=price_minor,
        selling_currency=currency,
        billing_parameters={},
        billing_model=billing_model,
        provider_available=True,
        enabled=True,
        created_at=datetime.now(UTC),
    )


def _cloud_offer(*, location_id: str = "eu-west-3", offer_id: UUID | None = None) -> SellableOffer:
    return _offer(
        location_id=location_id,
        product_id="lsw.mini",
        name="Mini",
        price_minor=2,
        currency="EUR",
        billing_model=BILLING_MODEL_HOURLY,
        offer_id=offer_id,
    )


def _catalog() -> ProviderCatalog:
    return ProviderCatalog(
        markets={PROVIDER: "foreign"},
        display_names={PROVIDER: "Leaseweb"},
        enabled={},
        families={
            PROVIDER: {
                "vps": {"billing_model": BILLING_MODEL_MONTHLY, "display_name": "VPS"},
                "cloud": {"billing_model": BILLING_MODEL_HOURLY, "display_name": "Cloud"},
            }
        },
    )


class FakeOffersRepo:
    def __init__(self, offers: list[SellableOffer]) -> None:
        self.offers = offers

    async def get(self, offer_id: UUID) -> SellableOffer | None:
        return next((o for o in self.offers if o.id == offer_id), None)

    async def list_sellable(self, provider_key: str | None = None) -> list[SellableOffer]:
        return [
            o
            for o in self.offers
            if o.sellable and (provider_key is None or o.provider_key == provider_key)
        ]

    async def list_provider_locations(self) -> list[tuple[str, str]]:
        return sorted({(o.provider_key, o.location_id) for o in self.offers if o.sellable})


class FakeLocationsRepo:
    def __init__(self, records: list[LocationRecord] | None = None) -> None:
        self._records = records or []

    async def upsert(self, record: LocationRecord) -> bool:
        return True

    async def list_for_provider(self, provider_key: str) -> list[LocationRecord]:
        return [r for r in self._records if r.provider_key == provider_key]


def _product_detail() -> Any:
    from cloud_platform.providers.leaseweb.ordering import (
        LeasewebProduct,
        LeasewebProductDetail,
        LeasewebProductOption,
    )

    return LeasewebProductDetail(
        product=LeasewebProduct(
            id="product",
            name="Leaseweb VPS 1",
            location="FRA-01",
            vcpu=4,
            ram_gb=6,
            disk_gb=100,
            traffic="5 TB",
            currency="EUR",
            monthly_price_minor=499,
            provider_price_minor=499,
        ),
        os_options=(
            LeasewebProductOption(
                name="Ubuntu 24.04", price_minor=0, currency="EUR", selected=False
            ),
        ),
        control_panels=(
            LeasewebProductOption(name="Webmin", price_minor=0, currency="EUR", selected=False),
        ),
        disk_upgrades=(),
        slas=(),
        available_locations=("FRA-01",),
        contract_terms={"1_MONTH": 499},
        billing_cycles={"1_MONTH": 499},
    )


class FakeRegistry:
    def get(self, key: str) -> Any:
        from cloud_platform.providers.base import Capability

        if key != PROVIDER:
            raise KeyError(key)
        detail = _product_detail()

        class _Provider:
            capabilities = frozenset({Capability.COMPUTE})

            async def get_product(self, location_id: str, product_id: str) -> Any:
                return detail

            def os_name_allowed(self, product_detail: Any, os_name: str) -> bool:
                return True

            async def place_order(self, request: Any, idempotency_key: Any) -> Any:
                raise AssertionError("no orders in render tests")

            async def get_order(self, provider_order_id: str) -> Any:
                raise AssertionError("no polling in render tests")

        provider = _Provider()
        provider.key = key
        return provider


class FakeWalletRepo:
    async def get(self, user_id: UUID) -> Any:
        return type("W", (), {"balance": 50_000})()


def _service(
    offers: list[SellableOffer],
    locations: FakeLocationsRepo | None = None,
    catalog: ProviderCatalog | None = None,
) -> OfferCatalogViewService:
    return OfferCatalogViewService(
        offers_repo=FakeOffersRepo(offers),
        provider_registry=FakeRegistry(),
        wallet_repo=FakeWalletRepo(),
        signing_key=SIGNING_KEY,
        market_catalog=catalog if catalog is not None else _catalog(),
        location_repo=locations,
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

    async def family_plans_screen(
        self, provider_key: str, family_key: str, location_id: str, page: int = 1
    ) -> Any:
        return await self._service.family_plans_screen(provider_key, family_key, location_id, page)

    async def plan_detail_screen(self, provider_key: str, location_id: str, product_id: str) -> Any:
        return await self._service.plan_detail_screen(provider_key, location_id, product_id)

    async def panel_screen(
        self, *, offer_id: UUID, os_index: int
    ) -> tuple[Any, list[Any], str, str]:
        return await self._service.panel_screen(offer_id=offer_id, os_index=os_index)

    async def panel_name_by_index(self, offer: SellableOffer, index: int) -> str | None:
        return await self._service.panel_name_by_index(offer, index)

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
    def __init__(self) -> None:
        self.panels: list[str | None] = []

    async def create_order(self, **kwargs: Any) -> Any:
        raise AssertionError("no checkout in render tests")


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


def _ui(service: OfferCatalogViewService) -> MonthlyBotUi:
    return MonthlyBotUi(
        SIGNING_KEY,
        offers_view=FakeView(service),  # type: ignore[arg-type]
        checkout=FakeCheckout(),  # type: ignore[arg-type]
        servers=FakeServers(),  # type: ignore[arg-type]
        orders=FakeOrders(),  # type: ignore[arg-type]
        renewals=FakeRenewals(),  # type: ignore[arg-type]
        offers_repo=FakeOffersRepo([]),
        wallet_history=FakeWalletHistory(),  # type: ignore[arg-type]
    )


def _decode(data: str) -> Callback:
    return decode_callback(data, SIGNING_KEY)


def _buttons(screen: Any) -> list[Any]:
    return [button for row in screen.keyboard.inline_keyboard for button in row]


def _size(data: str) -> int:
    return len(data.encode("utf-8"))


async def _press(ui: MonthlyBotUi, callback: str) -> Any:
    screen = await ui.handle(callback, user=USER)
    assert screen is not None
    return screen


class TestFamilySelector:
    async def test_leaseweb_shows_exactly_two_families(self) -> None:
        service = _service([_offer(location_id="FRA-01"), _cloud_offer()])
        families, _, _ = await service.families_screen(PROVIDER)
        assert [(f.family_key, f.billing_model) for f in families] == [
            ("vps", BILLING_MODEL_MONTHLY),
            ("cloud", BILLING_MODEL_HOURLY),
        ]

    async def test_family_buttons_carry_provider_neutral_identity(self) -> None:
        bot = _ui(_service([_offer(location_id="FRA-01"), _cloud_offer()]))
        screen = await _press(bot, bot._callback("store", "families", PROVIDER))
        labels = [b.text for b in _buttons(screen)]
        assert any("VPS" in label and "ماهانه" in label for label in labels)
        assert any("Cloud" in label and "ساعتی" in label for label in labels)
        assert len([b for b in labels if "VPS" in b or "Cloud" in b]) == 2
        for button in _buttons(screen):
            assert button.callback_data is not None
            assert _size(button.callback_data) <= 64

    async def test_single_family_provider_enters_directly(self) -> None:
        bot = _ui(_service([_offer(location_id="FRA-01")]))
        screen = await _press(bot, bot._callback("store", "families", PROVIDER))
        # No selector: straight into the monthly locations.
        assert any("FRA-01" in b.text for b in _buttons(screen))

    async def test_implicit_family_without_configuration(self) -> None:
        catalog = ProviderCatalog(
            markets={PROVIDER: "foreign"}, display_names={PROVIDER: "Leaseweb"}, enabled={}
        )
        service = _service([_offer(location_id="FRA-01")], catalog=catalog)
        families, _, _ = await service.families_screen(PROVIDER)
        assert len(families) == 1
        assert families[0].billing_model == BILLING_MODEL_MONTHLY

    async def test_other_provider_gets_its_own_implicit_family(self) -> None:
        # Hetzner-shaped provider: no family configuration, monthly offers.
        import dataclasses

        hetzner_offer = dataclasses.replace(
            _offer(location_id="fsn1"),
            provider_key="hetzner",
            product_id="cx22",
            id=uuid4(),
        )
        catalog = ProviderCatalog(
            markets={"hetzner": "foreign"},
            display_names={"hetzner": "Hetzner"},
            enabled={},
        )
        service = OfferCatalogViewService(
            offers_repo=FakeOffersRepo([hetzner_offer]),
            provider_registry=FakeRegistry(),
            wallet_repo=FakeWalletRepo(),
            signing_key=SIGNING_KEY,
            market_catalog=catalog,
        )
        families, _, _ = await service.families_screen("hetzner")
        assert len(families) == 1
        assert families[0].billing_model == BILLING_MODEL_MONTHLY
        assert families[0].provider_key == "hetzner"

    async def test_unknown_family_key_reopens_selector(self) -> None:
        bot = _ui(_service([_offer(location_id="FRA-01"), _cloud_offer()]))
        screen = await _press(bot, bot._callback("store", "family", PROVIDER, "nope"))
        assert any("VPS" in b.text for b in _buttons(screen))

    async def test_legacy_products_callback_reenters_families(self) -> None:
        bot = _ui(_service([_offer(location_id="FRA-01"), _cloud_offer()]))
        screen = await _press(bot, bot._callback("store", "products", PROVIDER))
        assert any("VPS" in b.text for b in _buttons(screen))

    async def test_retired_detail_callback_falls_back_to_menu_safely(self) -> None:
        bot = _ui(_service([_offer(location_id="FRA-01")]))
        screen = await _press(
            bot, bot._callback("store", "product_detail", PROVIDER, "VPS02_1", "624", "EUR")
        )
        assert screen is not None


class TestBillingSeparation:
    async def test_monthly_locations_exclude_hourly(self) -> None:
        service = _service([_offer(location_id="FRA-01"), _cloud_offer(location_id="eu-west-3")])
        view = await service.family_locations_screen(PROVIDER, "vps", 1)
        assert [item.location_id for item in view.items] == ["FRA-01"]

    async def test_hourly_locations_exclude_monthly(self) -> None:
        service = _service([_offer(location_id="FRA-01"), _cloud_offer(location_id="eu-west-3")])
        view = await service.cloud_locations_screen(PROVIDER, "cloud", 1)
        assert [item.location_id for item in view.items] == ["eu-west-3"]

    async def test_monthly_checkout_rejects_hourly_offer(self) -> None:
        from cloud_platform.modules.checkout.service import (
            MonthlyCheckoutService,
            OfferUnavailableError,
        )

        offer = _cloud_offer()
        checkout = MonthlyCheckoutService(
            server_repo=FakeServers(),  # type: ignore[arg-type]
            offers_repo=FakeOffersRepo([offer]),  # type: ignore[arg-type]
            account_repo=FakeOrders(),  # type: ignore[arg-type]
            wallet_repo=FakeWalletRepo(),  # type: ignore[arg-type]
            hold_repo=FakeOrders(),  # type: ignore[arg-type]
            orders_repo=FakeOrders(),  # type: ignore[arg-type]
            operation_repo=FakeOrders(),  # type: ignore[arg-type]
            audit_repo=FakeOrders(),  # type: ignore[arg-type]
            provider_registry=FakeRegistry(),
        )

        try:
            await checkout.create_order(
                user=USER, offer_id=offer.id, os_name="x", idempotency_key="k"
            )
        except OfferUnavailableError:
            return
        raise AssertionError("hourly offer must not pass monthly checkout")

    async def test_monthly_confirmation_rejects_hourly_offer(self) -> None:
        from cloud_platform.modules.checkout.service import OfferUnavailableError

        service = _service([_cloud_offer()])
        try:
            await service.confirmation(user_id=uuid4(), offer_id=_cloud_offer().id, os_index=0)
        except OfferUnavailableError:
            return
        raise AssertionError("hourly offer must not confirm monthly")


class TestPanelFlow:
    async def test_panel_screen_lists_free_panels_and_none(self) -> None:
        service = _service([_offer(location_id="FRA-01")])
        offer = (await service._offers.list_sellable(PROVIDER))[0]
        _view, options, _back, _cancel = await service.panel_screen(offer_id=offer.id, os_index=0)
        assert options[0].name is None
        assert any(option.name == "Webmin" for option in options)

    async def test_os_leads_to_panel_then_confirm(self) -> None:
        bot = _ui(_service([_offer(location_id="FRA-01")]))
        locations = await _press(bot, bot._callback("store", "vps_locations", PROVIDER, "vps", "1"))
        plans = await _press(
            bot, next(b for b in _buttons(locations) if "FRA-01" in b.text).callback_data or ""
        )
        detail = await _press(
            bot,
            next(
                b for b in _buttons(plans) if _decode(b.callback_data or "").screen == "plan_detail"
            ).callback_data
            or "",
        )
        continuation = next(
            b for b in _buttons(detail) if _decode(b.callback_data or "").screen == "os"
        )
        os_screen = await _press(bot, continuation.callback_data or "")
        panel_entry = next(
            b for b in _buttons(os_screen) if _decode(b.callback_data or "").screen == "panel"
        )
        panel = await _press(bot, panel_entry.callback_data or "")
        assert any("Webmin" in b.text for b in _buttons(panel))
        confirm_entry = next(
            b
            for b in _buttons(panel)
            if "Webmin" in b.text and _decode(b.callback_data or "").screen == "confirm"
        )
        confirm = await _press(bot, confirm_entry.callback_data or "")
        assert "Webmin" in confirm.text

    async def test_confirm_callback_carries_panel_index_within_budget(self) -> None:
        service = _service([_offer(location_id="FRA-01")])
        offer = (await service._offers.list_sellable(PROVIDER))[0]
        view = await service.confirmation(
            user_id=uuid4(), offer_id=offer.id, os_index=0, panel_index=1
        )
        assert view.panel_name == "Webmin"
        assert _size(view.confirm_callback) <= 64
        target = _decode(view.confirm_callback)
        assert target.args[1:] == ("0", "1")
