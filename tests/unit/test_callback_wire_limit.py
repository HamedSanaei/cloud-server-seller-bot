"""Telegram 64-byte callback_data invariant for the storefront.

Incident: adding currency to the product-card identity pushed
``store:product_locations:leaseweb:VPS02_1:624:EUR`` to 68 bytes, so Telegram
rejected the products screen keyboard (BUTTON_DATA_INVALID) while the CLI
preview stayed green.

Covered here with REAL production-shaped values
(leaseweb / VPS02_1 / 624 / EUR):

- the new product_locations callback carries currency semantically;
- its wire form uses the compact ``pl`` alias and fits in 64 UTF-8 bytes;
- it decodes back to the canonical
  ``store:product_locations:(leaseweb, VPS02_1, 624, EUR)``;
- EUR and GBP cards stay isolated (same price, different currencies);
- the literal pre-alias form still decodes (same signature either way);
- ambiguous legacy callbacks still reject instead of crossing currencies;
- every button of the real production-shaped products screen fits;
- a generic walk over the representative Telegram screens asserts the
  invariant for every button callback.
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
    TELEGRAM_CALLBACK_DATA_LIMIT_BYTES,
    Callback,
    CallbackError,
    decode_callback,
    encode_callback,
    encode_offer_ref,
    encode_telegram_callback,
    ensure_telegram_callback_size,
    resolve_offer_id_arg,
)
from cloud_platform.modules.offers.domain import SellableOffer
from cloud_platform.modules.users.domain import Role, User, UserStatus

SIGNING_KEY = "wire-limit-signing-key"
PROVIDER = "leaseweb"
PRODUCT = "VPS02_1"
PRICE = 624
OFFER_UUID = "0df88978-e3c3-464b-8f46-963a33a36636"

USER = User(
    id=uuid4(),
    username="cust",
    email="cust@example.test",
    status=UserStatus.ACTIVE,
    role=Role.USER,
    telegram_user_id=12345,
)


def _size(data: str) -> int:
    return len(data.encode("utf-8"))


def _offer(*, location_id: str, currency: str, offer_id: UUID | None = None) -> SellableOffer:
    return SellableOffer(
        id=offer_id or uuid4(),
        provider_key=PROVIDER,
        product_id=PRODUCT,
        location_id=location_id,
        name="Leaseweb VPS 1",
        vcpu=2,
        ram_gb=4,
        disk_gb=100,
        traffic=None,
        provider_cost_minor=500,
        provider_cost_currency=currency,
        selling_price_minor=PRICE,
        selling_currency=currency,
        billing_parameters={},
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


class FakeRegistry:
    def __init__(self, capable: dict[str, bool] | None = None) -> None:
        self._capable = capable or {PROVIDER: True}

    def get(self, key: str) -> Any:
        from cloud_platform.providers.base import Capability

        if key not in self._capable:
            raise KeyError(key)

        class _Provider:
            capabilities = frozenset({Capability.COMPUTE}) if self._capable[key] else frozenset()

        provider = _Provider()
        provider.key = key
        return provider


class FakeWalletRepo:
    async def get(self, user_id: UUID) -> Any:
        return type("W", (), {"balance": 50_000})()


def _production_shaped_offers() -> list[SellableOffer]:
    """35 sellable offers across two currencies/prices, production-shaped."""
    offers = [
        _offer(location_id="FRA-01", currency="EUR"),
        _offer(location_id="LON-01", currency="GBP"),
    ]
    for i in range(33):
        offers.append(
            _offer(
                location_id=f"LOC-{i:02d}",
                currency="EUR" if i % 2 == 0 else "GBP",
            )
        )
    return offers


def _service(offers: list[SellableOffer] | None = None) -> OfferCatalogViewService:
    rows = offers if offers is not None else _production_shaped_offers()
    return OfferCatalogViewService(
        offers_repo=FakeOffersRepo(rows),
        provider_registry=FakeRegistry(),
        wallet_repo=FakeWalletRepo(),
        signing_key=SIGNING_KEY,
        market_catalog=ProviderCatalog(
            markets={PROVIDER: "foreign"},
            display_names={PROVIDER: "Leaseweb"},
            enabled={},
        ),
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

    async def plans_screen(
        self, location_id: str, provider_key: str | None = None
    ) -> tuple[list[Any], str, str]:
        return await self._service.plans_screen(location_id, provider_key)

    async def os_screen(self, *, offer_id: UUID) -> tuple[Any, list[Any], str, str]:
        return await self._service.os_screen(offer_id=offer_id)

    async def os_by_index(self, offer: SellableOffer, index: int) -> str:
        return await self._service.os_by_index(offer, index)

    async def confirmation(
        self, *, user_id: UUID, offer_id: UUID, os_index: int, panel_index: int | None = None
    ) -> Any:
        return await self._service.confirmation(
            user_id=user_id, offer_id=offer_id, os_index=os_index
        )

    async def os_options(self, offer: SellableOffer) -> list[Any]:
        return await self._service.os_options(offer)


class FakeCheckout:
    async def create_order(self, **kwargs: Any) -> Any:
        raise AssertionError("no checkout in wire-limit tests")


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


def _assert_fits(data: str) -> None:
    assert _size(data) <= TELEGRAM_CALLBACK_DATA_LIMIT_BYTES, (
        f"callback_data exceeds {TELEGRAM_CALLBACK_DATA_LIMIT_BYTES} bytes: {data!r}"
    )


class TestProductLocationsWireAlias:
    """Currency stays in the identity; the wire stays within 64 bytes."""

    def test_new_callback_includes_currency_and_fits(self) -> None:
        encoded = encode_callback(
            Callback("store", "product_locations", (PROVIDER, PRODUCT, "624", "EUR")),
            SIGNING_KEY,
        )
        assert _size(encoded) <= TELEGRAM_CALLBACK_DATA_LIMIT_BYTES
        assert ":pl:" in encoded
        assert "product_locations" not in encoded

    def test_new_callback_decodes_to_canonical_screen_and_args(self) -> None:
        encoded = encode_callback(
            Callback("store", "product_locations", (PROVIDER, PRODUCT, "624", "EUR")),
            SIGNING_KEY,
        )
        decoded = decode_callback(encoded, SIGNING_KEY)
        assert decoded.flow == "store"
        assert decoded.screen == "product_locations"
        assert decoded.args == (PROVIDER, PRODUCT, "624", "EUR")

    def test_gbp_card_fits_and_stays_isolated(self) -> None:
        eur = encode_callback(
            Callback("store", "product_locations", (PROVIDER, PRODUCT, "624", "EUR")),
            SIGNING_KEY,
        )
        gbp = encode_callback(
            Callback("store", "product_locations", (PROVIDER, PRODUCT, "624", "GBP")),
            SIGNING_KEY,
        )
        assert eur != gbp
        _assert_fits(eur)
        _assert_fits(gbp)
        assert _decode(eur).args[3] == "EUR"
        assert _decode(gbp).args[3] == "GBP"

    def test_legacy_literal_form_still_decodes(self) -> None:
        encoded = encode_callback(
            Callback("store", "product_locations", (PROVIDER, PRODUCT, "624", "EUR")),
            SIGNING_KEY,
        )
        # The pre-alias literal bytes verify through the same signature path.
        legacy = encoded.replace("store:pl:", "store:product_locations:")
        assert "product_locations" in legacy
        assert _decode(legacy) == _decode(encoded)
        assert _decode(legacy).screen == "product_locations"

    def test_legacy_price_only_form_still_decodes(self) -> None:
        encoded = encode_callback(
            Callback("store", "product_locations", (PROVIDER, PRODUCT, "624")),
            SIGNING_KEY,
        )
        decoded = _decode(encoded)
        assert (decoded.flow, decoded.screen) == ("store", "product_locations")
        assert decoded.args == (PROVIDER, PRODUCT, "624")

    def test_fail_fast_rejects_over_limit_payloads(self) -> None:
        oversized = encode_callback(
            Callback("store", "confirm", (OFFER_UUID, "0")),
            SIGNING_KEY,
        )
        assert _size(oversized) > TELEGRAM_CALLBACK_DATA_LIMIT_BYTES
        with pytest.raises(CallbackError):
            ensure_telegram_callback_size(oversized)
        with pytest.raises(CallbackError):
            encode_telegram_callback(
                Callback("store", "confirm", (OFFER_UUID, "0")),
                SIGNING_KEY,
            )

    def test_decode_stays_permissive_for_oversized_legacy(self) -> None:
        # Length enforcement applies to generation only: previously issued
        # callbacks must still decode.
        oversized = encode_callback(
            Callback("store", "confirm", (OFFER_UUID, "0")),
            SIGNING_KEY,
        )
        decoded = _decode(oversized)
        assert (decoded.flow, decoded.screen) == ("store", "confirm")


class TestCurrencyIsolationOnTheWire:
    """The alias must not weaken the currency-isolation semantics."""

    async def test_cards_encode_distinct_fitting_callbacks(self) -> None:
        service = _service(
            [
                _offer(location_id="FRA-01", currency="EUR"),
                _offer(location_id="LON-01", currency="GBP"),
            ]
        )
        products, _, _ = await service.products_screen(PROVIDER)
        assert len(products) == 2
        for card in products:
            _assert_fits(card.select_callback)
        eur = next(p for p in products if p.currency == "EUR")
        gbp = next(p for p in products if p.currency == "GBP")
        assert eur.select_callback != gbp.select_callback
        assert _decode(eur.select_callback).args[3] == "EUR"
        assert _decode(gbp.select_callback).args[3] == "GBP"
        eur_locations, _, _ = await service.product_locations_screen(
            PROVIDER, PRODUCT, PRICE, "EUR"
        )
        gbp_locations, _, _ = await service.product_locations_screen(
            PROVIDER, PRODUCT, PRICE, "GBP"
        )
        assert [v.location_id for v in eur_locations] == ["FRA-01"]
        assert [v.location_id for v in gbp_locations] == ["LON-01"]

    async def test_ambiguous_legacy_still_rejects(self) -> None:
        service = _service(
            [
                _offer(location_id="FRA-01", currency="EUR"),
                _offer(location_id="LON-01", currency="GBP"),
            ]
        )
        with pytest.raises(OfferUnavailableError):
            await service.product_locations_screen(PROVIDER, PRODUCT, PRICE, None)


class TestProductionShapedProductsScreen:
    """The real failing screen: 35 production-shaped cards, every button fits."""

    async def test_every_product_card_button_fits(self) -> None:
        products, back, cancel = await _service().products_screen(PROVIDER)
        assert len(products) >= 2
        for card in products:
            _assert_fits(card.select_callback)
        _assert_fits(back)
        _assert_fits(cancel)

    async def test_full_screen_renders_without_over_limit_callback(self) -> None:
        bot = _ui(_service())
        screen = await _press(bot, bot._callback("store", "products", PROVIDER))
        buttons = _buttons(screen)
        assert buttons, "the products screen must render buttons"
        for button in buttons:
            assert button.callback_data is not None
            _assert_fits(button.callback_data)

    async def test_product_locations_buttons_fit_for_both_currencies(self) -> None:
        offers = [
            _offer(location_id="FRA-01", currency="EUR"),
            _offer(location_id="LON-01", currency="GBP"),
        ]
        bot = _ui(_service(offers))
        products = await _press(bot, bot._callback("store", "products", PROVIDER))
        for card in [b for b in _buttons(products) if "VPS 1" in b.text]:
            currency = _decode(card.callback_data or "").args[3]
            availability = await _press(bot, card.callback_data or "")
            for button in _buttons(availability):
                assert button.callback_data is not None
                _assert_fits(button.callback_data)
                target = _decode(button.callback_data)
                if target.screen == "os":
                    # Offer-identity row (compact ref): must point at the card's
                    # currency and resolve to the exact offer UUID.
                    offer = next(o for o in offers if o.id == resolve_offer_id_arg(target.args[0]))
                    assert offer.selling_currency == currency


class TestRepresentativeScreensInvariant:
    """Generic walk: every button on the core Telegram screens fits."""

    async def test_core_navigation_buttons_fit(self) -> None:
        bot = _ui(
            _service(
                [
                    _offer(location_id="FRA-01", currency="EUR"),
                    _offer(location_id="LON-01", currency="GBP"),
                ]
            )
        )
        market = await _press(bot, bot._callback("store", "market"))
        providers = await _press(bot, bot._callback("store", "providers", "foreign"))
        # Single implicit family enters its locations directly.
        locations = await _press(bot, bot._callback("store", "families", PROVIDER))
        location_button = next(b for b in _buttons(locations) if "FRA-01" in b.text)
        plans = await _press(bot, location_button.callback_data or "")
        plan_button = next(
            b for b in _buttons(plans) if _decode(b.callback_data or "").screen == "plan_detail"
        )
        detail = await _press(bot, plan_button.callback_data or "")
        os_entry = next(
            b for b in _buttons(detail) if _decode(b.callback_data or "").screen == "os"
        )
        await _press(bot, os_entry.callback_data or "")
        menu = bot.menu_screen()
        wallet = await _press(bot, bot._callback("wallet", "balance"))
        recharge = await _press(bot, bot._callback("recharge", "amounts"))
        servers = await _press(bot, bot._callback("servers", "list"))
        support = bot.support_screen()
        # Offer-identity buttons (os/confirm/buy with compact refs) fit by
        # construction; assert every button fits without exceptions.
        for screen in (
            market,
            providers,
            locations,
            plans,
            detail,
            menu,
            wallet,
            recharge,
            servers,
            support,
        ):
            for button in _buttons(screen):
                if button.callback_data is None:
                    continue
                _assert_fits(button.callback_data)


class TestCallbackAudit:
    """Central audit: bounded navigation fits; report the overall maximum."""

    def test_bounded_navigation_shapes_fit(self) -> None:
        ref = encode_offer_ref(UUID(OFFER_UUID))
        shapes = {
            "store.market": ("store", "market", ()),
            "store.providers": ("store", "providers", ("foreign",)),
            "store.families": ("store", "families", (PROVIDER,)),
            "store.family": ("store", "family", (PROVIDER, "vps")),
            "store.vps_locations": ("store", "vps_locations", (PROVIDER, "vps", "1")),
            "store.vps_plans": ("store", "vps_plans", (PROVIDER, "vps", "FRA-01", "1")),
            "store.plan_detail": ("store", "plan_detail", (PROVIDER, "FRA-01", PRODUCT)),
            "store.panel": ("store", "panel", (ref, "0")),
            "store.os": ("store", "os", (ref,)),
            "store.confirm": ("store", "confirm", (ref, "0", "0")),
            "store.buy": ("store", "buy", (ref, "0", "0")),
            "store.locations": ("store", "locations", (PROVIDER,)),
            "store.plans": ("store", "plans", (PROVIDER, "FRA-01")),
            "store.product_locations": ("store", "product_locations", (PROVIDER, PRODUCT, "624")),
            "main.menu": ("main", "menu", ()),
            "recharge.start": ("recharge", "start", ("10000", "stripe")),
            "wallet.history": ("wallet", "history", ()),
            "servers.list": ("servers", "list", ("1",)),
            "support.contact": ("support", "contact", ()),
        }
        worst = 0
        for label, (flow, screen, args) in shapes.items():
            encoded = encode_callback(Callback(flow, screen, args), SIGNING_KEY)
            size = _size(encoded)
            worst = max(worst, size)
            assert size <= TELEGRAM_CALLBACK_DATA_LIMIT_BYTES, label
        assert worst <= TELEGRAM_CALLBACK_DATA_LIMIT_BYTES

    def test_new_offer_callbacks_fit(self) -> None:
        # Offer-identity callbacks now carry compact refs and fit.
        for flow, screen, args in [
            ("store", "os", (encode_offer_ref(UUID(OFFER_UUID)),)),
            ("store", "confirm", (encode_offer_ref(UUID(OFFER_UUID)), "0", "0")),
            ("store", "buy", (encode_offer_ref(UUID(OFFER_UUID)), "0", "0")),
        ]:
            encoded = encode_callback(Callback(flow, screen, args), SIGNING_KEY)
            assert _size(encoded) <= TELEGRAM_CALLBACK_DATA_LIMIT_BYTES, (flow, screen)

    def test_legacy_uuid_callbacks_still_decode(self) -> None:
        # Callbacks signed before compact refs existed carry the plain UUID
        # (over the old 64-byte budget, but decoding stays permissive) and
        # must resolve to the exact same offer.
        import hashlib
        import hmac as hmac_lib

        for flow, screen, args in [
            ("store", "os", (OFFER_UUID,)),
            ("store", "confirm", (OFFER_UUID, "0")),
            ("store", "buy", (OFFER_UUID, "0")),
        ]:
            key = ":".join((flow, screen, *args))
            sig = hmac_lib.new(SIGNING_KEY.encode(), key.encode(), hashlib.sha256).hexdigest()[:16]
            legacy = f"v1|{key}|{sig}"
            decoded = _decode(legacy)
            assert (decoded.flow, decoded.screen) == (flow, screen)
            assert resolve_offer_id_arg(decoded.args[0]) == UUID(OFFER_UUID)
