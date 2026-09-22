"""Storefront navigation: market -> provider -> location -> plan -> OS.

The customer must now choose a MARKET (🇮🇷 سرور ایران / 🌍 سرور خارج) before any
location or plan appears, and the purchase path must stay provider-neutral:
no handler may contain a provider name, and a provider that cannot actually
take an order must never be offered as buyable.

Covered here:

- the market selector is the first screen behind «خرید سرور»;
- the Iran market lists configured Iranian providers, the foreign market the
  rest — including a SECOND Iranian provider (extensibility proof);
- a provider without an ordering port renders as "coming soon" (not buyable);
- an operator-disabled provider disappears from the storefront;
- every back button walks the flow in reverse back to the main menu;
- the plan row and confirm button carry the exact monthly price.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from cloud_platform.bot.monthly_ui import MonthlyBotUi, format_minor
from cloud_platform.core.i18n import Locale, Translator
from cloud_platform.modules.checkout.service import (
    OfferCatalogView,
    OfferCatalogViewService,
    OfferConfirmView,
    OfferOsOptionView,
)
from cloud_platform.modules.markets.domain import Market, ProviderCatalog
from cloud_platform.modules.navigation.domain import Callback, decode_callback, encode_callback
from cloud_platform.modules.offers.domain import (
    BILLING_MODEL_PREPAID_MONTHLY,
    SellableOffer,
)
from cloud_platform.modules.users.domain import Role, User, UserStatus

SIGNING_KEY = "storefront-signing-key"
IRAN_PROVIDER = "ir-provider"
IRAN_PROVIDER_TWO = "ir-provider-two"
FOREIGN_PROVIDER = "eu-provider"
NOT_CAPABLE_PROVIDER = "eu-legacy"
DISABLED_PROVIDER = "eu-disabled"


def _short_product_code(provider_key: str) -> str:
    """Compact default product code (initials), production-shaped.

    Real catalog product codes are short (``VPS02_1``); the product-card
    callback must fit Telegram's 64-byte button limit, so fixtures must not
    use long synthetic ids either.
    """
    return "".join(word[0] for word in provider_key.split("-")) + "-pd"


def _offer(
    *,
    provider_key: str,
    location_id: str,
    name: str,
    price_minor: int = 1_899,
    offer_id: UUID | None = None,
    enabled: bool = True,
    product_id: str | None = None,
) -> SellableOffer:
    return SellableOffer(
        id=offer_id or uuid4(),
        provider_key=provider_key,
        product_id=product_id or _short_product_code(provider_key),
        location_id=location_id,
        name=name,
        vcpu=2,
        ram_gb=4,
        disk_gb=40,
        traffic=None,
        provider_cost_minor=1_299,
        provider_cost_currency="EUR",
        selling_price_minor=price_minor,
        selling_currency="EUR",
        billing_parameters={},
        provider_available=True,
        enabled=enabled,
        created_at=datetime.now(UTC),
    )


class FakeOffersRepo:
    """Sellable-offer repository double (sellability gates enforced)."""

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
    """A real provider product detail (specs + free OS options)."""
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
            disk_gb=40,
            traffic=None,
            currency="EUR",
            monthly_price_minor=1_299,
            provider_price_minor=1_299,
        ),
        os_options=(
            LeasewebProductOption(
                name="Ubuntu 24.04", price_minor=0, currency="EUR", selected=False
            ),
            LeasewebProductOption(name="Debian 12", price_minor=0, currency="EUR", selected=False),
            LeasewebProductOption(
                name="Windows 2022", price_minor=1_500, currency="EUR", selected=False
            ),
        ),
        control_panels=(),
        disk_upgrades=(),
        slas=(),
        available_locations=("AMS-01", "FRA-01", "ir-thr-1"),
        contract_terms={"1_MONTH": 1_299},
        billing_cycles={"1_MONTH": 1_299},
    )


class FakeRegistry:
    """Provider registry double mirroring the real port probes.

    ``ordering_capable`` models the order-based mode and ``compute_capable``
    the direct-create mode: a provider is sellable when it can provision by
    EITHER route, and the storefront must learn that from the port, never from
    a provider name.
    """

    def __init__(
        self,
        ordering_capable: dict[str, bool],
        compute_capable: dict[str, bool] | None = None,
    ) -> None:
        self._capable = ordering_capable
        self._compute = compute_capable or {}

    def get(self, key: str) -> Any:
        from cloud_platform.providers.base import Capability

        if key not in self._capable:
            raise KeyError(key)
        detail = _product_detail()

        class _Provider:
            """Ordering port + compute capability, per configuration."""

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
        if self._compute.get(key):
            capabilities.add(Capability.COMPUTE)
        provider.capabilities = frozenset(capabilities)
        return provider


class FakeWalletRepo:
    def __init__(self, balance: int) -> None:
        self.balance = balance

    async def get(self, user_id: UUID) -> Any:
        return type("W", (), {"balance": self.balance})()


def _catalog(
    *,
    markets: dict[str, str] | None = None,
    display_names: dict[str, str] | None = None,
    enabled: dict[str, bool] | None = None,
) -> ProviderCatalog:
    return ProviderCatalog(
        markets={
            IRAN_PROVIDER: "iran",
            IRAN_PROVIDER_TWO: "iran",
            FOREIGN_PROVIDER: "foreign",
            NOT_CAPABLE_PROVIDER: "foreign",
            DISABLED_PROVIDER: "foreign",
            **(markets or {}),
        },
        display_names={
            IRAN_PROVIDER: "Iran Cloud",
            IRAN_PROVIDER_TWO: "Iran Cloud 2",
            FOREIGN_PROVIDER: "Global Host",
            NOT_CAPABLE_PROVIDER: "Legacy Host",
            DISABLED_PROVIDER: "Hidden Host",
            **(display_names or {}),
        },
        enabled={DISABLED_PROVIDER: False, **(enabled or {})},
    )


OFFERS = [
    _offer(provider_key=IRAN_PROVIDER, location_id="ir-thr-1", name="IR Small"),
    _offer(provider_key=IRAN_PROVIDER_TWO, location_id="ir-thr-2", name="IR Two"),
    _offer(provider_key=FOREIGN_PROVIDER, location_id="AMS-01", name="EU Small"),
    _offer(provider_key=FOREIGN_PROVIDER, location_id="FRA-01", name="EU Big", price_minor=3_499),
    _offer(provider_key=NOT_CAPABLE_PROVIDER, location_id="LEG-01", name="Legacy"),
    _offer(provider_key=DISABLED_PROVIDER, location_id="HID-01", name="Hidden"),
]


def _view_service(
    *,
    offers: list[SellableOffer] | None = None,
    catalog: ProviderCatalog | None = None,
    compute_capable: dict[str, bool] | None = None,
) -> OfferCatalogViewService:
    return OfferCatalogViewService(
        offers_repo=FakeOffersRepo(offers if offers is not None else OFFERS),
        provider_registry=FakeRegistry(
            {
                IRAN_PROVIDER: True,
                IRAN_PROVIDER_TWO: True,
                FOREIGN_PROVIDER: True,
                NOT_CAPABLE_PROVIDER: False,
                DISABLED_PROVIDER: True,
            },
            compute_capable,
        ),
        wallet_repo=FakeWalletRepo(50_000),
        signing_key=SIGNING_KEY,
        market_catalog=catalog if catalog is not None else _catalog(),
    )


class FakeView:
    """Full storefront view double for the UI tests."""

    def __init__(self, service: OfferCatalogViewService) -> None:
        self._service = service
        self.confirmed: list[tuple[UUID, int]] = []

    def markets_screen(self) -> list[Any]:
        return self._service.markets_screen()

    def provider_display_name(self, provider_key: str) -> str:
        return self._service.provider_display_name(provider_key)

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
    ) -> tuple[list[OfferCatalogView], str, str]:
        return await self._service.plans_screen(location_id, provider_key)

    async def os_screen(self, *, offer_id: UUID) -> tuple[Any, list[Any], str, str]:
        return await self._service.os_screen(offer_id=offer_id)

    async def os_by_index(self, offer: SellableOffer, index: int) -> str:
        return await self._service.os_by_index(offer, index)

    async def confirmation(
        self, *, user_id: UUID, offer_id: UUID, os_index: int
    ) -> OfferConfirmView:
        self.confirmed.append((offer_id, os_index))
        return await self._service.confirmation(
            user_id=user_id, offer_id=offer_id, os_index=os_index
        )

    async def os_options(self, offer: SellableOffer) -> list[OfferOsOptionView]:
        return await self._service.os_options(offer)


class FakeCheckout:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, str, str]] = []

    async def create_order(
        self, *, user: User, offer_id: UUID, os_name: str, idempotency_key: str
    ) -> Any:
        self.calls.append((offer_id, os_name, idempotency_key))
        return type(
            "Result",
            (),
            {
                "order": type("O", (), {"id": uuid4()})(),
                "replayed": len(self.calls) > 1,
            },
        )()


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


USER = User(
    id=uuid4(),
    username="cust",
    email="cust@example.test",
    status=UserStatus.ACTIVE,
    role=Role.USER,
    telegram_user_id=12345,
)


@pytest.fixture
def ui() -> MonthlyBotUi:
    return MonthlyBotUi(
        SIGNING_KEY,
        offers_view=FakeView(_view_service()),
        checkout=FakeCheckout(),
        servers=FakeServers(),
        orders=FakeOrders(),
        renewals=FakeRenewals(),
        offers_repo=FakeOffersRepo(OFFERS),
        wallet_history=FakeWalletHistory(),
        support_contact="@support",
    )


def _decode(data: str) -> Callback:
    return decode_callback(data, SIGNING_KEY)


def _buttons(screen: Any) -> list[Any]:
    return [button for row in screen.keyboard.inline_keyboard for button in row]


async def _press(ui: MonthlyBotUi, callback: str, user: User | None = USER) -> Any:
    screen = await ui.handle(callback, user=user)
    assert screen is not None, f"callback {callback!r} is not handled by the storefront UI"
    return screen


class TestMarketSelector:
    """«خرید سرور» opens the market selector, not a location list."""

    async def test_menu_buy_button_opens_the_market_screen(self, ui: MonthlyBotUi) -> None:
        menu = ui.menu_screen()
        buy = menu.keyboard.inline_keyboard[0][0]
        cb = _decode(buy.callback_data)
        assert (cb.flow, cb.screen) == ("store", "market")
        screen = await _press(ui, buy.callback_data)
        assert "نوع سرور" in screen.text

    async def test_both_markets_are_always_offered(self, ui: MonthlyBotUi) -> None:
        screen = await _press(ui, ui._callback("store", "market"))
        labels = [b.text for b in _buttons(screen)]
        assert "🇮🇷 سرور ایران" in labels
        assert "🌍 سرور خارج" in labels

    async def test_market_buttons_target_their_own_market(self, ui: MonthlyBotUi) -> None:
        screen = await _press(ui, ui._callback("store", "market"))
        targets = {b.text: _decode(b.callback_data) for b in _buttons(screen)}
        assert targets["🇮🇷 سرور ایران"].args == ("iran",)
        assert targets["🌍 سرور خارج"].args == ("foreign",)


class TestProviderListing:
    """Providers come from configuration; capability decides buyability."""

    async def test_iran_market_lists_every_iranian_provider(self, ui: MonthlyBotUi) -> None:
        screen = await _press(ui, ui._callback("store", "providers", "iran"))
        labels = [b.text for b in _buttons(screen)]
        assert any("Iran Cloud — 1" in label for label in labels)
        assert any("Iran Cloud 2 — 1" in label for label in labels)
        assert not any("Global Host" in label for label in labels)

    async def test_foreign_market_lists_only_foreign_providers(self, ui: MonthlyBotUi) -> None:
        screen = await _press(ui, ui._callback("store", "providers", "foreign"))
        labels = [b.text for b in _buttons(screen)]
        assert any("Global Host — 2" in label for label in labels)
        assert not any("Iran Cloud" in label for label in labels)

    async def test_provider_that_cannot_provision_is_not_buyable(self, ui: MonthlyBotUi) -> None:
        """Legacy Host implements NEITHER provisioning mode, so it is not sold."""
        screen = await _press(ui, ui._callback("store", "providers", "foreign"))
        rows = screen.keyboard.inline_keyboard
        soon = [b for b in _buttons(screen) if "به‌زودی" in b.text]
        assert len(soon) == 1
        assert "Legacy Host" in soon[0].text
        # The row does not lead to a location list; it stays on the market.
        index = next(i for i, row in enumerate(rows) if soon[0] in row)
        target = _decode(rows[index][0].callback_data)
        assert (target.flow, target.screen) == ("store", "market")

    async def test_direct_create_provider_is_buyable_without_an_ordering_port(self) -> None:
        """The other legitimate mode: create a server directly, no order to poll."""
        service = _view_service(compute_capable={NOT_CAPABLE_PROVIDER: True})
        views, _back = await service.providers_screen("foreign")
        legacy = next(v for v in views if v.provider_key == NOT_CAPABLE_PROVIDER)
        assert legacy.buyable is True
        assert legacy.select_callback is not None

    async def test_operator_disabled_provider_is_hidden(self, ui: MonthlyBotUi) -> None:
        screen = await _press(ui, ui._callback("store", "providers", "foreign"))
        assert not any("Hidden Host" in b.text for b in _buttons(screen))

    async def test_disabling_a_provider_removes_it_from_the_storefront(self) -> None:
        catalog = _catalog(enabled={FOREIGN_PROVIDER: False})
        bot = MonthlyBotUi(
            SIGNING_KEY,
            offers_view=FakeView(_view_service(catalog=catalog)),
            checkout=FakeCheckout(),
            servers=FakeServers(),
            orders=FakeOrders(),
            renewals=FakeRenewals(),
            offers_repo=FakeOffersRepo(OFFERS),
            wallet_history=FakeWalletHistory(),
        )
        screen = await _press(bot, bot._callback("store", "providers", "foreign"))
        assert not any("Global Host" in b.text for b in _buttons(screen))

    async def test_empty_market_says_so(self) -> None:
        bot = MonthlyBotUi(
            SIGNING_KEY,
            offers_view=FakeView(_view_service(offers=[])),
            checkout=FakeCheckout(),
            servers=FakeServers(),
            orders=FakeOrders(),
            renewals=FakeRenewals(),
            offers_repo=FakeOffersRepo([]),
            wallet_history=FakeWalletHistory(),
        )
        screen = await _press(bot, bot._callback("store", "providers", "iran"))
        assert "فروش فعال نشده" in screen.text

    async def test_location_without_sellable_offers_is_hidden(self) -> None:
        """A merely known location (ineligible/empty at the provider) with
        no sellable offer must not appear; an unknown code with a sellable
        offer must appear verbatim."""
        from dataclasses import replace

        offers = [
            _offer(provider_key=FOREIGN_PROVIDER, location_id="FRA-01", name="EU Big"),
            replace(
                _offer(provider_key=FOREIGN_PROVIDER, location_id="FRA-10", name="EU Gone"),
                provider_available=False,
            ),
            _offer(provider_key=FOREIGN_PROVIDER, location_id="NEW-99", name="EU New"),
        ]
        service = _view_service(offers=offers)
        locations, _, _ = await service.locations_screen(FOREIGN_PROVIDER)
        assert {view.location_id for view in locations} == {"FRA-01", "NEW-99"}

    def test_handlers_never_contain_a_concrete_provider_name(self) -> None:
        # Provider neutrality: the storefront CODE must not branch on a
        # provider key. Documentation may name the shipped providers; the
        # executable module body may not.
        import ast
        import importlib

        path = importlib.import_module(MonthlyBotUi.__module__).__file__
        assert path
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        # Drop the module docstring: it is documentation, not a branch.
        if (
            tree.body
            and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)
        ):
            tree.body = tree.body[1:]
        code = ast.unparse(tree).lower()
        for provider in ("leaseweb", "hetzner", "arvancloud"):
            assert provider not in code, f"{provider} leaked into the storefront handlers"


class TestAggregatedCatalog:
    """One product card per product, with its availability listed underneath.

    Leaseweb is served by SEVERAL credential accounts, each with its own
    location scope, so the same product is sellable in many places. The
    customer still sees ONE card per product — never one per location — and the
    locations are the availability options they choose between.
    """

    @staticmethod
    def _multi_location_service() -> OfferCatalogViewService:
        """One product, identical spec/price, available in three locations."""
        shared = dict(
            product_id="shared-product",
            name="Shared VPS",
            price_minor=1_899,
        )
        offers = [
            _offer(provider_key=FOREIGN_PROVIDER, location_id=code, **shared)
            for code in ("AAA-01", "BBB-02", "CCC-03")
        ]
        return _view_service(offers=offers)

    async def test_a_product_available_in_many_locations_is_one_card(self) -> None:
        service = self._multi_location_service()
        products, _, _ = await service.products_screen(FOREIGN_PROVIDER)
        assert len(products) == 1
        assert products[0].locations == ("AAA-01", "BBB-02", "CCC-03")

    async def test_the_card_shows_one_price_and_the_location_count(self) -> None:
        bot = MonthlyBotUi(
            SIGNING_KEY,
            offers_view=FakeView(self._multi_location_service()),
            checkout=FakeCheckout(),
            servers=FakeServers(),
            orders=FakeOrders(),
            renewals=FakeRenewals(),
            offers_repo=FakeOffersRepo([]),
            wallet_history=FakeWalletHistory(),
        )
        screen = await _press(bot, bot._callback("store", "products", FOREIGN_PROVIDER))
        cards = [b for b in _buttons(screen) if "Shared VPS" in b.text]
        assert len(cards) == 1
        assert "€18.99" in cards[0].text
        # Readable card: spec inline, no bare location count.
        assert "2C/4GB" in cards[0].text

    async def test_each_location_is_its_own_offer_behind_one_card(self) -> None:
        service = self._multi_location_service()
        products, _, _ = await service.products_screen(FOREIGN_PROVIDER)
        locations, _, _ = await service.product_locations_screen(
            FOREIGN_PROVIDER, products[0].product_id, products[0].monthly_price_minor
        )
        assert [view.location_id for view in locations] == ["AAA-01", "BBB-02", "CCC-03"]
        # Distinct sellable offers — each with its own fulfillment route.
        assert len({view.offer_id for view in locations}) == 3

    async def test_products_with_different_specs_or_prices_stay_apart(self) -> None:
        offers = [
            _offer(
                provider_key=FOREIGN_PROVIDER,
                location_id="AAA-01",
                name="Small VPS",
                price_minor=1_899,
            ),
            _offer(
                provider_key=FOREIGN_PROVIDER,
                location_id="BBB-02",
                name="Small VPS",
                price_minor=3_499,
            ),
        ]
        products, _, _ = await _view_service(offers=offers).products_screen(FOREIGN_PROVIDER)
        assert len(products) == 2

    async def test_the_location_list_of_one_card_excludes_other_prices(self) -> None:
        offers = [
            _offer(
                provider_key=FOREIGN_PROVIDER,
                location_id="AAA-01",
                name="Small VPS",
                price_minor=1_899,
            ),
            _offer(
                provider_key=FOREIGN_PROVIDER,
                location_id="BBB-02",
                name="Small VPS",
                price_minor=3_499,
            ),
        ]
        service = _view_service(offers=offers)
        products, _, _ = await service.products_screen(FOREIGN_PROVIDER)
        cheap = next(p for p in products if p.monthly_price_minor == 1_899)
        locations, _, _ = await service.product_locations_screen(
            FOREIGN_PROVIDER, cheap.product_id, cheap.monthly_price_minor
        )
        assert [view.location_id for view in locations] == ["AAA-01"]

    async def test_a_product_card_leads_to_its_availability_list(self) -> None:
        bot = MonthlyBotUi(
            SIGNING_KEY,
            offers_view=FakeView(self._multi_location_service()),
            checkout=FakeCheckout(),
            servers=FakeServers(),
            orders=FakeOrders(),
            renewals=FakeRenewals(),
            offers_repo=FakeOffersRepo([]),
            wallet_history=FakeWalletHistory(),
        )
        products = await _press(bot, bot._callback("store", "products", FOREIGN_PROVIDER))
        card = next(b for b in _buttons(products) if "Shared VPS" in b.text)
        availability = await _press(bot, card.callback_data)
        labels = [b.text for b in _buttons(availability)]
        assert any("AAA-01" in label for label in labels)
        assert any("BBB-02" in label for label in labels)
        assert any("CCC-03" in label for label in labels)


class TestFlowAndBackChain:
    """market -> provider -> product -> location -> OS -> confirm, and back."""

    async def test_full_forward_flow_shows_the_exact_monthly_price(self, ui: MonthlyBotUi) -> None:
        # market -> providers(foreign)
        screen = await _press(ui, ui._callback("store", "providers", "foreign"))
        provider_button = next(b for b in _buttons(screen) if "Global Host" in b.text)
        # provider -> product cards (aggregated inventory)
        screen = await _press(ui, provider_button.callback_data)
        assert "Global Host" in screen.text
        card = next(b for b in _buttons(screen) if "EU Small" in b.text)
        assert "€18.99" in card.text
        # product card -> its availability (locations)
        screen = await _press(ui, card.callback_data)
        location_button = next(b for b in _buttons(screen) if "AMS-01" in b.text)
        assert "€18.99" in location_button.text
        # location -> OS
        screen = await _press(ui, location_button.callback_data)
        assert "سیستم‌عامل" in screen.text
        os_button = _buttons(screen)[0]
        # OS -> confirm
        screen = await _press(ui, os_button.callback_data)
        assert "تأیید نهایی خرید" in screen.text
        assert "€18.99" in screen.text
        assert "موجودی کیف پول: €500.00" in screen.text

    async def test_back_walks_the_flow_in_reverse_to_the_menu(self, ui: MonthlyBotUi) -> None:
        market = ui._callback("store", "market")
        foreign = _decode(
            next(
                b.callback_data
                for b in _buttons(await _press(ui, market))
                if b.text == "🌍 سرور خارج"
            )
        )
        providers = await _press(ui, encode_callback(foreign, SIGNING_KEY))
        provider_button = next(b for b in _buttons(providers) if "Global Host" in b.text)
        products = await _press(ui, provider_button.callback_data)
        card = next(b for b in _buttons(products) if "EU Small" in b.text)
        availability = await _press(ui, card.callback_data)
        location_button = next(b for b in _buttons(availability) if "AMS-01" in b.text)
        os_screen = await _press(ui, location_button.callback_data)
        os_button = _buttons(os_screen)[0]
        confirm = await _press(ui, os_button.callback_data)

        # confirm -> back = OS
        back = next(b for b in _buttons(confirm) if _decode(b.callback_data).screen == "os")
        os_again = await _press(ui, back.callback_data)
        assert "سیستم‌عامل" in os_again.text

        # OS -> back = the product's detail screen (plan first, then location)
        back = next(
            b for b in _buttons(os_again) if _decode(b.callback_data).screen == "product_detail"
        )
        target = _decode(back.callback_data)
        assert target.args == ("eu-provider", "ep-pd", "1899", "EUR")
        detail_again = await _press(ui, back.callback_data)
        assert any("AMS-01" in b.text for b in _buttons(detail_again))

        # detail -> back = the provider's product cards
        back = next(
            b for b in _buttons(detail_again) if _decode(b.callback_data).screen == "products"
        )
        assert _decode(back.callback_data).args[0] == "eu-provider"
        products_again = await _press(ui, back.callback_data)
        assert any("EU Small" in b.text for b in _buttons(products_again))

        # products -> back = providers of the market
        back = next(
            b for b in _buttons(products_again) if _decode(b.callback_data).screen == "providers"
        )
        assert _decode(back.callback_data).args == ("foreign",)
        providers_again = await _press(ui, back.callback_data)
        assert any("Global Host" in b.text for b in _buttons(providers_again))

        # providers -> back = the market selector
        back = next(
            b for b in _buttons(providers_again) if _decode(b.callback_data).screen == "market"
        )
        market_again = await _press(ui, back.callback_data)
        assert "نوع سرور" in market_again.text

        # market -> menu (the main menu button is always present)
        menu_button = next(
            b
            for b in _buttons(market_again)
            if (_decode(b.callback_data).flow, _decode(b.callback_data).screen) == ("main", "menu")
        )
        menu = await _press(ui, menu_button.callback_data)
        assert "منوی اصلی" in menu.text

    async def test_the_same_plan_name_in_two_locations_is_kept_apart(
        self, ui: MonthlyBotUi
    ) -> None:
        screen = await _press(ui, ui._callback("store", "plans", "eu-provider", "FRA-01"))
        labels = [b.text for b in _buttons(screen)]
        assert any("EU Big" in label for label in labels)
        assert not any("EU Small" in label for label in labels)

    async def test_plans_of_another_provider_are_never_shown(self, ui: MonthlyBotUi) -> None:
        screen = await _press(ui, ui._callback("store", "plans", "ir-provider", "AMS-01"))
        # No Iranian provider has a plan in AMS-01.
        assert "پلنی برای فروش" in screen.text

    async def test_english_locale_is_available_for_support(self) -> None:
        bot = MonthlyBotUi(
            SIGNING_KEY,
            offers_view=FakeView(_view_service()),
            checkout=FakeCheckout(),
            servers=FakeServers(),
            orders=FakeOrders(),
            renewals=FakeRenewals(),
            offers_repo=FakeOffersRepo(OFFERS),
            wallet_history=FakeWalletHistory(),
            translator=Translator(Locale.EN),
        )
        screen = await _press(bot, bot._callback("store", "market"))
        labels = [b.text for b in _buttons(screen)]
        assert "🇮🇷 Iran server" in labels
        assert "🌍 Foreign server" in labels


class TestRechargeAction:
    """An insufficient balance has a reachable, honest recharge action."""

    async def test_wallet_recharge_without_a_gateway_points_at_support(
        self, ui: MonthlyBotUi
    ) -> None:
        screen = await _press(ui, ui._callback("recharge", "amounts"))
        assert "فعال نیست" in screen.text
        assert "@support" in screen.text

    async def test_menu_has_a_recharge_entry(self, ui: MonthlyBotUi) -> None:
        labels = [b.text for b in _buttons(ui.menu_screen())]
        assert "⬆️ شارژ حساب" in labels

    async def test_confirmation_warns_when_the_balance_is_short(self) -> None:
        rich = _view_service()
        offer = rich.markets_screen()  # smoke: markets are always available
        assert len(offer) == 2
        bot = MonthlyBotUi(
            SIGNING_KEY,
            offers_view=FakeView(_view_service()),
            checkout=FakeCheckout(),
            servers=FakeServers(),
            orders=FakeOrders(),
            renewals=FakeRenewals(),
            offers_repo=FakeOffersRepo(OFFERS),
            wallet_history=FakeWalletHistory(),
        )
        # A 500.00 EUR wallet covers an 18.99 EUR plan: no warning.
        screen = await bot.confirm_screen(USER, OFFERS[0].id, 0)
        assert "موجودی کافی نیست" not in screen.text


class TestMarketCatalogFromSettings:
    """The storefront metadata is configuration, not code."""

    def test_settings_drive_the_markets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cloud_platform.core.config import Settings

        settings = Settings(
            provider_markets={"a": "iran", "b": "foreign", "c": "foreign"},
            provider_display_names={"a": "A", "b": "B", "c": "C"},
            providers_enabled={"c": False},
        )
        catalog = ProviderCatalog(
            markets=settings.provider_markets,
            display_names=settings.provider_display_names,
            enabled=settings.providers_enabled,
        )
        assert catalog.market_of("a") is Market.IRAN
        assert catalog.display_name_of("b") == "B"
        assert catalog.is_enabled("c") is False

    def test_defaults_cover_the_shipped_providers(self) -> None:
        from cloud_platform.core.config import DEFAULT_PROVIDER_MARKETS

        assert DEFAULT_PROVIDER_MARKETS["arvancloud"] == "iran"
        assert DEFAULT_PROVIDER_MARKETS["leaseweb"] == "foreign"
        assert DEFAULT_PROVIDER_MARKETS["hetzner"] == "foreign"

    def test_billing_model_of_every_storefront_plan_is_monthly_prepaid(self) -> None:
        service = _view_service()
        views = [service._view(o) for o in OFFERS]
        assert {v.billing_model for v in views} == {BILLING_MODEL_PREPAID_MONTHLY}

    def test_price_formatting_is_integer_based(self) -> None:
        assert format_minor(1_899, "EUR") == "€18.99"
        assert format_minor(0, "EUR") == "€0.00"
