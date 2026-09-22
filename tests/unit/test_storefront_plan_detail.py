"""Monthly plan list, pagination and plan-detail screen (STOREFRONT-REWORK).

Location-first flow: families -> VPS locations -> plans (paginated,
concise spec rows) -> plan detail (full spec, proven facts only) ->
continue -> OS -> panel -> confirm. Country flags come from synced
location rows; every emitted callback fits 64 bytes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from cloud_platform.bot.monthly_ui import MonthlyBotUi
from cloud_platform.core.i18n import Locale, Translator
from cloud_platform.modules.catalog.domain import LocationRecord
from cloud_platform.modules.checkout.service import OfferCatalogViewService
from cloud_platform.modules.markets.domain import ProviderCatalog
from cloud_platform.modules.navigation.domain import (
    TELEGRAM_CALLBACK_DATA_LIMIT_BYTES,
    Callback,
    decode_callback,
)
from cloud_platform.modules.offers.domain import SellableOffer
from cloud_platform.modules.users.domain import Role, User, UserStatus

SIGNING_KEY = "plan-detail-signing-key"
PROVIDER = "leaseweb"
PRODUCT = "VPS02_1"

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
    price_minor: int = 562,
    currency: str = "EUR",
    product_id: str = PRODUCT,
    name: str = "Leaseweb VPS 1",
    technical_metadata: dict[str, object] | None = None,
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
        provider_cost_minor=449,
        provider_cost_currency=currency,
        selling_price_minor=price_minor,
        selling_currency=currency,
        billing_parameters={},
        technical_metadata=dict(technical_metadata or {}),
        provider_available=True,
        enabled=True,
        created_at=datetime.now(UTC),
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
    """Synced provider-location rows with real country metadata."""

    def __init__(self, records: list[LocationRecord]) -> None:
        self._records = records

    async def upsert(self, record: LocationRecord) -> bool:
        return True

    async def list_for_provider(self, provider_key: str) -> list[LocationRecord]:
        return [r for r in self._records if r.provider_key == provider_key]


def _locations(*codes: str) -> FakeLocationsRepo:
    table = {
        "FRA-01": ("Frankfurt", "DE"),
        "FRA-10": ("Frankfurt", "DE"),
        "FRA-14": ("Frankfurt", "DE"),
        "LON-01": ("London", "GB"),
        "LON-11": ("London", "GB"),
        "LON-12": ("London", "GB"),
    }
    return FakeLocationsRepo(
        [
            LocationRecord(
                provider_key=PROVIDER,
                location_id=code,
                name=table[code][0] if code in table else code,
                country_code=table[code][1] if code in table else None,
                city=table[code][0] if code in table else None,
            )
            for code in codes
        ]
    )


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
            location="FRA-10",
            vcpu=4,
            ram_gb=6,
            disk_gb=100,
            traffic="5 TB",
            currency="EUR",
            monthly_price_minor=449,
            provider_price_minor=449,
        ),
        os_options=(
            LeasewebProductOption(
                name="Ubuntu 24.04", price_minor=0, currency="EUR", selected=False
            ),
            LeasewebProductOption(name="Debian 12", price_minor=0, currency="EUR", selected=False),
        ),
        control_panels=(),
        disk_upgrades=(),
        slas=(),
        available_locations=("FRA-10", "FRA-14"),
        contract_terms={"1_MONTH": 449},
        billing_cycles={"1_MONTH": 449},
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
                return any(
                    o.name == os_name and o.price_minor == 0 for o in product_detail.os_options
                )

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
) -> OfferCatalogViewService:
    return OfferCatalogViewService(
        offers_repo=FakeOffersRepo(offers),
        provider_registry=FakeRegistry(),
        wallet_repo=FakeWalletRepo(),
        signing_key=SIGNING_KEY,
        market_catalog=ProviderCatalog(
            markets={PROVIDER: "foreign"},
            display_names={PROVIDER: "Leaseweb"},
            enabled={},
        ),
        location_repo=locations,
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

    async def locations_screen(self, provider_key: str) -> tuple[list[Any], str, str]:
        return await self._service.locations_screen(provider_key)

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


def _assert_fits(data: str) -> None:
    assert _size(data) <= TELEGRAM_CALLBACK_DATA_LIMIT_BYTES, (
        f"callback_data exceeds 64 bytes: {data!r}"
    )


async def _press(ui: MonthlyBotUi, callback: str) -> Any:
    screen = await ui.handle(callback, user=USER)
    assert screen is not None
    return screen


async def _open_detail(bot: MonthlyBotUi, location_id: str) -> Any:
    locations = await _press(bot, bot._callback("store", "vps_locations", PROVIDER, "monthly", "1"))
    location_button = next(b for b in _buttons(locations) if location_id in b.text)
    plans = await _press(bot, location_button.callback_data or "")
    plan_button = next(
        b for b in _buttons(plans) if _decode(b.callback_data or "").screen == "plan_detail"
    )
    return await _press(bot, plan_button.callback_data or "")


class TestVpsLocations:
    async def test_locations_show_flags_not_raw_codes(self) -> None:
        bot = _ui(_service([_offer(location_id="FRA-10")], _locations("FRA-10")))
        screen = await _press(
            bot, bot._callback("store", "vps_locations", PROVIDER, "monthly", "1")
        )
        labels = [b.text for b in _buttons(screen) if "FRA-10" in b.text]
        assert len(labels) == 1
        assert "🇩🇪" in labels[0]
        assert "Frankfurt" in labels[0]

    async def test_unknown_country_renders_neutrally(self) -> None:
        bot = _ui(_service([_offer(location_id="FRA-10")]))
        screen = await _press(
            bot, bot._callback("store", "vps_locations", PROVIDER, "monthly", "1")
        )
        labels = [b.text for b in _buttons(screen) if "FRA-10" in b.text]
        assert len(labels) == 1
        assert "??" not in labels[0]

    async def test_locations_paginate(self) -> None:
        offers = [_offer(location_id=f"LOC-{i:02d}", price_minor=500 + i) for i in range(8)]
        service = _service(offers)
        first = await service.family_locations_screen(PROVIDER, "monthly", 1)
        assert first.page == 1
        assert first.total_pages == 2
        assert len(first.items) == 6
        assert first.next_callback is not None
        second = await service.family_locations_screen(PROVIDER, "monthly", 2)
        assert len(second.items) == 2
        assert second.prev_callback is not None
        assert {i.location_id for i in first.items}.isdisjoint(
            {i.location_id for i in second.items}
        )


class TestVpsPlans:
    def _many(self, count: int) -> list[SellableOffer]:
        return [
            _offer(location_id="FRA-01", price_minor=500 + i, product_id=f"VPS02_{i}")
            for i in range(count)
        ]

    async def test_plans_show_specs_and_price(self) -> None:
        bot = _ui(_service([_offer(location_id="FRA-01")], _locations("FRA-01")))
        locations = await _press(
            bot, bot._callback("store", "vps_locations", PROVIDER, "monthly", "1")
        )
        location_button = next(b for b in _buttons(locations) if "FRA-01" in b.text)
        plans = await _press(bot, location_button.callback_data or "")
        rows = [b for b in _buttons(plans) if "€5.62" in b.text]
        assert len(rows) == 1
        assert "4" in rows[0].text and "6" in rows[0].text

    async def test_plans_paginate_without_loss_or_duplicates(self) -> None:
        service = _service(self._many(13))
        seen: list[str] = []
        for page_number in (1, 2, 3):
            page = await service.family_plans_screen(PROVIDER, "monthly", "FRA-01", page_number)
            seen.extend(str(item.offer_id) for item in page.items)
        assert len(seen) == 13
        assert len(set(seen)) == 13

    async def test_plans_of_other_locations_are_excluded(self) -> None:
        service = _service(
            [
                _offer(location_id="FRA-01", price_minor=562),
                _offer(location_id="LON-01", price_minor=700),
            ]
        )
        page = await service.family_plans_screen(PROVIDER, "monthly", "FRA-01")
        assert [item.monthly_price_minor for item in page.items] == [562]


class TestPlanDetailScreen:
    async def test_detail_shows_exact_specs_and_price(self) -> None:
        bot = _ui(
            _service(
                [_offer(location_id="FRA-10"), _offer(location_id="FRA-14")],
                _locations("FRA-10", "FRA-14"),
            )
        )
        detail = await _open_detail(bot, "FRA-10")
        assert "Leaseweb VPS 1" in detail.text
        assert "4 vCPU" in detail.text
        assert "6 GB" in detail.text
        assert "100 GB" in detail.text
        assert "5 TB" in detail.text
        assert "€5.62" in detail.text
        assert "FRA-10" in detail.text
        assert "FRA-14" not in detail.text

    async def test_detail_shows_known_technical_facts(self) -> None:
        bot = _ui(
            _service(
                [
                    _offer(
                        location_id="FRA-10",
                        technical_metadata={
                            "architecture": "x86_64",
                            "storage_type": "NVMe",
                        },
                    )
                ],
                _locations("FRA-10"),
            )
        )
        detail = await _open_detail(bot, "FRA-10")
        assert "x86_64" in detail.text
        assert "NVMe" in detail.text

    async def test_unknown_ipv6_renders_neutrally(self) -> None:
        bot = _ui(_service([_offer(location_id="FRA-10")], _locations("FRA-10")))
        detail = await _open_detail(bot, "FRA-10")
        assert "IPv6" in detail.text
        unknown = Translator().t("store.detail_unknown")
        assert unknown in detail.text

    async def test_detail_continue_leads_to_os(self) -> None:
        bot = _ui(
            _service(
                [_offer(location_id="FRA-10"), _offer(location_id="FRA-14")],
                _locations("FRA-10", "FRA-14"),
            )
        )
        detail = await _open_detail(bot, "FRA-10")
        continuation = next(
            b for b in _buttons(detail) if _decode(b.callback_data or "").screen == "os"
        )
        os_screen = await _press(bot, continuation.callback_data or "")
        assert "سیستم‌عامل" in os_screen.text

    async def test_english_detail_renders(self) -> None:
        service = _service([_offer(location_id="FRA-10")], _locations("FRA-10"))
        bot = MonthlyBotUi(
            SIGNING_KEY,
            offers_view=FakeView(service),  # type: ignore[arg-type]
            checkout=FakeCheckout(),  # type: ignore[arg-type]
            servers=FakeServers(),  # type: ignore[arg-type]
            orders=FakeOrders(),  # type: ignore[arg-type]
            renewals=FakeRenewals(),  # type: ignore[arg-type]
            offers_repo=FakeOffersRepo([]),
            wallet_history=FakeWalletHistory(),  # type: ignore[arg-type]
            translator=Translator(Locale.EN),
        )
        detail = await _open_detail(bot, "FRA-10")
        assert "Leaseweb VPS 1" in detail.text
        assert "CPU" in detail.text


class TestFullStorefrontWalk:
    """families -> locations -> plans -> detail -> OS -> panel ->
    confirmation; every emitted callback fits Telegram's budget."""

    async def test_complete_walk_with_byte_budget(self) -> None:
        bot = _ui(
            _service(
                [_offer(location_id="FRA-10"), _offer(location_id="FRA-14")],
                _locations("FRA-10", "FRA-14"),
            )
        )
        seen: list[str] = []

        def _track(callback_data: str | None) -> str:
            assert callback_data is not None
            _assert_fits(callback_data)
            seen.append(callback_data)
            return callback_data

        market = await _press(bot, _track(bot._callback("store", "market")))
        provider_button = next(b for b in _buttons(market) if "🌍" in b.text)
        providers = await _press(bot, _track(provider_button.callback_data))
        leaseweb_button = next(b for b in _buttons(providers) if "Leaseweb" in b.text)
        # Single family enters its locations directly (no selector tap).
        locations = await _press(bot, _track(leaseweb_button.callback_data))
        location_button = next(b for b in _buttons(locations) if "FRA-10" in b.text)
        plans = await _press(bot, _track(location_button.callback_data))
        plan_button = next(
            b for b in _buttons(plans) if _decode(b.callback_data or "").screen == "plan_detail"
        )
        detail = await _press(bot, _track(plan_button.callback_data))
        continuation = next(
            b for b in _buttons(detail) if _decode(b.callback_data or "").screen == "os"
        )
        os_screen = await _press(bot, _track(continuation.callback_data))
        assert "سیستم‌عامل" in os_screen.text
        os_button = _buttons(os_screen)[0]
        panel = await _press(bot, _track(os_button.callback_data))
        panel_button = _buttons(panel)[0]
        confirm = await _press(bot, _track(panel_button.callback_data))
        assert "€5.62" in confirm.text
        buy_button = next(
            b for b in _buttons(confirm) if _decode(b.callback_data or "").screen == "buy"
        )
        _track(buy_button.callback_data)
        for button in _buttons(confirm):
            if button.callback_data is not None:
                _assert_fits(button.callback_data)
        assert seen, "the walk must emit callbacks"
