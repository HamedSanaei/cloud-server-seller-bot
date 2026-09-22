"""Storefront currency isolation: currency is part of product-card identity.

Regression case for the cross-currency grouping/navigation bug: the same
provider/product/spec with the SAME minor-unit price in two currencies must
stay two separate product cards end-to-end, and navigation must never
cross-match them.

Covered here (provider-neutral, no currency conversion anywhere):

- products_screen returns TWO cards for 624 EUR vs 624 GBP;
- each card's locations list contains ONLY its own currency/location;
- each card callback encodes provider/product/price/currency and cannot
  navigate into the other currency's offer;
- back navigation (OS -> product_locations) preserves the exact currency;
- confirmation still uses the exact selected offer ID and exact currency;
- legacy callbacks without currency never silently cross currencies;
- same currency + same price + same product still aggregates into ONE card.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from cloud_platform.bot.monthly_ui import MonthlyBotUi
from cloud_platform.modules.checkout.service import (
    OfferCatalogViewService,
    OfferUnavailableError,
)
from cloud_platform.modules.markets.domain import ProviderCatalog
from cloud_platform.modules.navigation.domain import (
    Callback,
    decode_callback,
    resolve_offer_id_arg,
)
from cloud_platform.modules.offers.domain import SellableOffer
from cloud_platform.modules.users.domain import Role, User, UserStatus

SIGNING_KEY = "currency-isolation-signing-key"
PROVIDER = "eu-provider"
PRODUCT = "VPS02_1"
PRICE = 624
EUR_LOCATION = "FRA-01"
GBP_LOCATION = "LON-01"

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
    provider_key: str = PROVIDER,
    product_id: str = PRODUCT,
    location_id: str,
    price_minor: int = PRICE,
    currency: str,
    name: str = "VPS 1",
    offer_id: UUID | None = None,
) -> SellableOffer:
    return SellableOffer(
        id=offer_id or uuid4(),
        provider_key=provider_key,
        product_id=product_id,
        location_id=location_id,
        name=name,
        vcpu=2,
        ram_gb=4,
        disk_gb=100,
        traffic=None,
        provider_cost_minor=500,
        provider_cost_currency=currency,
        selling_price_minor=price_minor,
        selling_currency=currency,
        billing_parameters={},
        provider_available=True,
        enabled=True,
        created_at=datetime.now(UTC),
    )


def _currency_pair_offers() -> list[SellableOffer]:
    """Same provider/product/spec/price, two currencies (the bug shape)."""
    return [
        _offer(location_id=EUR_LOCATION, currency="EUR"),
        _offer(location_id=GBP_LOCATION, currency="GBP"),
    ]


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


def _product_detail() -> Any:
    from cloud_platform.providers.leaseweb.ordering import (
        LeasewebProduct,
        LeasewebProductDetail,
        LeasewebProductOption,
    )

    return LeasewebProductDetail(
        product=LeasewebProduct(
            id="product",
            name="Small",
            location="AMS-01",
            vcpu=2,
            ram_gb=4,
            disk_gb=100,
            traffic=None,
            currency="EUR",
            monthly_price_minor=500,
            provider_price_minor=500,
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
        available_locations=(EUR_LOCATION, GBP_LOCATION),
        contract_terms={"1_MONTH": 500},
        billing_cycles={"1_MONTH": 500},
    )


class FakeRegistry:
    """Provider registry double mirroring the real ordering port probe."""

    def __init__(self, capable: dict[str, bool] | None = None) -> None:
        self._capable = capable or {PROVIDER: True}

    def get(self, key: str) -> Any:
        from cloud_platform.providers.base import Capability

        if key not in self._capable:
            raise KeyError(key)
        detail = _product_detail()

        class _Provider:
            """Ordering port, per configuration."""

            capabilities = frozenset()

            async def get_product(self, location_id: str, product_id: str) -> Any:
                return detail

            def os_name_allowed(self, product_detail: Any, os_name: str) -> bool:
                return any(
                    o.name == os_name and o.price_minor == 0 for o in product_detail.os_options
                )

        provider = _Provider()
        provider.key = key
        capabilities: set[Any] = set()
        if self._capable[key]:
            capabilities.add(Capability.COMPUTE)

            async def place_order(request: Any, idempotency_key: Any) -> Any:
                raise AssertionError("the storefront UI must never place an order")

            async def get_order(provider_order_id: str) -> Any:
                raise AssertionError("the storefront UI must never poll an order")

            provider.place_order = place_order  # type: ignore[attr-defined]
            provider.get_order = get_order  # type: ignore[attr-defined]
        provider.capabilities = frozenset(capabilities)
        return provider


class FakeWalletRepo:
    def __init__(self, balance: int = 50_000) -> None:
        self.balance = balance

    async def get(self, user_id: UUID) -> Any:
        return type("W", (), {"balance": self.balance})()


def _catalog(provider_key: str = PROVIDER) -> ProviderCatalog:
    return ProviderCatalog(
        markets={provider_key: "foreign"},
        display_names={provider_key: "Generic Host"},
        enabled={},
    )


def _service(
    offers: list[SellableOffer] | None = None,
    provider_key: str = PROVIDER,
) -> OfferCatalogViewService:
    rows = offers if offers is not None else _currency_pair_offers()
    providers = {o.provider_key for o in rows} | {provider_key}
    return OfferCatalogViewService(
        offers_repo=FakeOffersRepo(rows),
        provider_registry=FakeRegistry({key: True for key in providers}),
        wallet_repo=FakeWalletRepo(),
        signing_key=SIGNING_KEY,
        market_catalog=ProviderCatalog(
            markets={key: "foreign" for key in providers},
            display_names={key: "Generic Host" for key in providers},
            enabled={},
        ),
    )


class FakeView:
    def __init__(self, service: OfferCatalogViewService) -> None:
        self._service = service

    def provider_display_name(self, provider_key: str) -> str:
        return self._service.provider_display_name(provider_key)

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

    async def products_page(self, provider_key: str, page: int = 1) -> Any:
        return await self._service.products_page(provider_key, page)

    async def product_detail_screen(
        self,
        provider_key: str,
        product_id: str,
        price_minor: int,
        currency: str,
    ) -> Any:
        return await self._service.product_detail_screen(
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

    async def confirmation(self, *, user_id: UUID, offer_id: UUID, os_index: int) -> Any:
        return await self._service.confirmation(
            user_id=user_id, offer_id=offer_id, os_index=os_index
        )

    async def os_options(self, offer: SellableOffer) -> list[Any]:
        return await self._service.os_options(offer)


class FakeCheckout:
    async def create_order(self, **kwargs: Any) -> Any:
        raise AssertionError("no checkout in navigation tests")


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


async def _press(ui: MonthlyBotUi, callback: str) -> Any:
    screen = await ui.handle(callback, user=USER)
    assert screen is not None
    return screen


class TestCurrencyCardIdentity:
    """The same price in two currencies stays two isolated cards."""

    async def test_same_price_different_currencies_is_two_cards(self) -> None:
        products, _, _ = await _service().products_screen(PROVIDER)
        assert len(products) == 2
        assert {(p.monthly_price_minor, p.currency) for p in products} == {
            (PRICE, "EUR"),
            (PRICE, "GBP"),
        }

    async def test_card_callbacks_encode_provider_product_price_and_currency(self) -> None:
        products, _, _ = await _service().products_screen(PROVIDER)
        for card in products:
            target = _decode(card.select_callback)
            assert (target.flow, target.screen) == ("store", "product_locations")
            assert target.args == (PROVIDER, PRODUCT, str(PRICE), card.currency)

    async def test_eur_card_locations_contain_only_the_eur_location(self) -> None:
        service = _service()
        products, _, _ = await service.products_screen(PROVIDER)
        eur = next(p for p in products if p.currency == "EUR")
        locations, _, _ = await service.product_locations_screen(
            PROVIDER, eur.product_id, eur.monthly_price_minor, eur.currency
        )
        assert [view.location_id for view in locations] == [EUR_LOCATION]
        assert {view.currency for view in locations} == {"EUR"}

    async def test_gbp_card_locations_contain_only_the_gbp_location(self) -> None:
        service = _service()
        products, _, _ = await service.products_screen(PROVIDER)
        gbp = next(p for p in products if p.currency == "GBP")
        locations, _, _ = await service.product_locations_screen(
            PROVIDER, gbp.product_id, gbp.monthly_price_minor, gbp.currency
        )
        assert [view.location_id for view in locations] == [GBP_LOCATION]
        assert {view.currency for view in locations} == {"GBP"}

    async def test_eur_card_callback_cannot_navigate_into_gbp_offer(self) -> None:
        service = _service()
        products, _, _ = await service.products_screen(PROVIDER)
        eur = next(p for p in products if p.currency == "EUR")
        gbp_offer = next(o for o in _currency_pair_offers() if o.selling_currency == "GBP")
        target = _decode(eur.select_callback)
        locations, _, _ = await service.product_locations_screen(
            target.args[0], target.args[1], int(target.args[2]), target.args[3]
        )
        assert [view.location_id for view in locations] == [EUR_LOCATION]
        assert gbp_offer.id not in {view.offer_id for view in locations}

    async def test_gbp_card_callback_cannot_navigate_into_eur_offer(self) -> None:
        service = _service()
        products, _, _ = await service.products_screen(PROVIDER)
        gbp = next(p for p in products if p.currency == "GBP")
        eur_offer = next(o for o in _currency_pair_offers() if o.selling_currency == "EUR")
        target = _decode(gbp.select_callback)
        locations, _, _ = await service.product_locations_screen(
            target.args[0], target.args[1], int(target.args[2]), target.args[3]
        )
        assert [view.location_id for view in locations] == [GBP_LOCATION]
        assert eur_offer.id not in {view.offer_id for view in locations}

    async def test_back_navigation_from_os_preserves_currency(self) -> None:
        offers = _currency_pair_offers()
        service = _service(offers)
        for offer in offers:
            _view, _options, back_callback, _cancel = await service.os_screen(offer_id=offer.id)
            target = _decode(back_callback)
            assert (target.flow, target.screen) == ("store", "product_detail")
            assert target.args == (
                offer.provider_key,
                offer.product_id,
                str(offer.selling_price_minor),
                offer.selling_currency,
            )
            detail = await service.product_detail_screen(
                target.args[0], target.args[1], int(target.args[2]), target.args[3]
            )
            assert [view.offer_id for view in detail.locations] == [offer.id]
            assert {view.currency for view in detail.locations} == {offer.selling_currency}

    async def test_confirmation_uses_the_exact_selected_offer_and_currency(self) -> None:
        offers = _currency_pair_offers()
        service = _service(offers)
        for offer in offers:
            view = await service.confirmation(user_id=uuid4(), offer_id=offer.id, os_index=0)
            assert view.offer.offer_id == offer.id
            assert view.currency == offer.selling_currency
            assert view.offer.monthly_price_minor == offer.selling_price_minor
            confirm = _decode(view.confirm_callback)
            assert offer.id == resolve_offer_id_arg(confirm.args[0])

    async def test_isolation_is_provider_neutral(self) -> None:
        """Same shape with another provider and other currencies stays apart."""
        provider = "neutral-provider"
        offers = [
            _offer(
                provider_key=provider,
                product_id="SHARED-9",
                location_id="LOC-A",
                price_minor=999,
                currency="USD",
            ),
            _offer(
                provider_key=provider,
                product_id="SHARED-9",
                location_id="LOC-B",
                price_minor=999,
                currency="CHF",
            ),
        ]
        service = _service(offers, provider_key=provider)
        products, _, _ = await service.products_screen(provider)
        assert len(products) == 2
        usd = next(p for p in products if p.currency == "USD")
        chf = next(p for p in products if p.currency == "CHF")
        usd_locations, _, _ = await service.product_locations_screen(
            provider, usd.product_id, usd.monthly_price_minor, usd.currency
        )
        chf_locations, _, _ = await service.product_locations_screen(
            provider, chf.product_id, chf.monthly_price_minor, chf.currency
        )
        assert [view.location_id for view in usd_locations] == ["LOC-A"]
        assert [view.location_id for view in chf_locations] == ["LOC-B"]


class TestSameCurrencyAggregation:
    """Same currency + same price + same product still aggregates to one card."""

    async def test_identical_price_and_currency_is_one_card(self) -> None:
        offers = [
            _offer(location_id=code, price_minor=1899, currency="EUR")
            for code in ("AAA-01", "BBB-02", "CCC-03")
        ]
        service = _service(offers)
        products, _, _ = await service.products_screen(PROVIDER)
        assert len(products) == 1
        card = products[0]
        assert card.monthly_price_minor == 1899
        assert card.currency == "EUR"
        assert card.locations == ("AAA-01", "BBB-02", "CCC-03")
        target = _decode(card.select_callback)
        assert target.args == (PROVIDER, PRODUCT, "1899", "EUR")
        locations, _, _ = await service.product_locations_screen(
            PROVIDER, card.product_id, card.monthly_price_minor, card.currency
        )
        assert [view.location_id for view in locations] == ["AAA-01", "BBB-02", "CCC-03"]
        assert len({view.offer_id for view in locations}) == 3


class TestLegacyCallbacks:
    """Old callbacks without currency must never silently cross currencies."""

    async def test_legacy_price_only_callback_is_rejected_when_ambiguous(self) -> None:
        with pytest.raises(OfferUnavailableError):
            await _service().product_locations_screen(PROVIDER, PRODUCT, PRICE, None)

    async def test_legacy_price_only_callback_succeeds_when_unambiguous(self) -> None:
        offers = [_offer(location_id="ONLY-01", price_minor=1899, currency="EUR")]
        service = _service(offers)
        locations, _, _ = await service.product_locations_screen(PROVIDER, PRODUCT, 1899, None)
        assert [view.location_id for view in locations] == ["ONLY-01"]

    async def test_legacy_product_only_callback_is_rejected_when_ambiguous(self) -> None:
        with pytest.raises(OfferUnavailableError):
            await _service().product_locations_screen(PROVIDER, PRODUCT, None, None)

    async def test_legacy_ui_callback_falls_back_to_products_without_crossing(self) -> None:
        bot = _ui(_service())
        legacy = bot._callback("store", "product_locations", PROVIDER, PRODUCT, str(PRICE))
        screen = await _press(bot, legacy)
        # Re-rendered products screen: forward buttons are product cards
        # (4-arg product_detail), never OS rows of a mixed location list.
        targets = [_decode(b.callback_data) for b in _buttons(screen)]
        forward = [t for t in targets if t.screen == "product_detail"]
        assert forward, "legacy callback must fall back to the product cards"
        assert all(len(t.args) == 4 for t in forward)
        assert not any(t.screen == "os" for t in targets)

    async def test_unambiguous_legacy_ui_callback_still_lists_locations(self) -> None:
        offers = [_offer(location_id="ONLY-01", price_minor=1899, currency="EUR")]
        bot = _ui(_service(offers))
        legacy = bot._callback("store", "product_locations", PROVIDER, PRODUCT, "1899")
        screen = await _press(bot, legacy)
        assert any("ONLY-01" in b.text for b in _buttons(screen))


class TestUiNavigation:
    """End-to-end button navigation keeps each currency isolated."""

    async def test_pressing_each_card_shows_only_its_location(self) -> None:
        bot = _ui(_service())
        products = await _press(bot, bot._callback("store", "products", PROVIDER))
        cards = [b for b in _buttons(products) if "VPS 1" in b.text]
        assert len(cards) == 2
        seen: dict[str, list[str]] = {}
        for card in cards:
            availability = await _press(bot, card.callback_data)
            labels = [b.text for b in _buttons(availability)]
            target = _decode(card.callback_data)
            if target.args[3] == "EUR":
                assert any(EUR_LOCATION in label for label in labels)
                assert not any(GBP_LOCATION in label for label in labels)
                seen["EUR"] = labels
            else:
                assert any(GBP_LOCATION in label for label in labels)
                assert not any(EUR_LOCATION in label for label in labels)
                seen["GBP"] = labels
        assert set(seen) == {"EUR", "GBP"}

    async def test_os_back_button_preserves_currency_in_ui(self) -> None:
        bot = _ui(_service())
        products = await _press(bot, bot._callback("store", "products", PROVIDER))
        card = next(b for b in _buttons(products) if "VPS 1" in b.text)
        card_target = _decode(card.callback_data)
        availability = await _press(bot, card.callback_data)
        location_button = next(
            b for b in _buttons(availability) if _decode(b.callback_data).screen == "os"
        )
        os_screen = await _press(bot, location_button.callback_data)
        back = next(
            b for b in _buttons(os_screen) if _decode(b.callback_data).screen == "product_detail"
        )
        back_target = _decode(back.callback_data)
        assert back_target.args == card_target.args
        assert back_target.args[3] == card_target.args[3]
        again = await _press(bot, back.callback_data)
        labels = [b.text for b in _buttons(again)]
        if card_target.args[3] == "EUR":
            assert any(EUR_LOCATION in label for label in labels)
            assert not any(GBP_LOCATION in label for label in labels)
        else:
            assert any(GBP_LOCATION in label for label in labels)
            assert not any(EUR_LOCATION in label for label in labels)
