"""Monthly Telegram UI tests (LEASEWEB-MVP).

The MonthlyBotUi is framework-light: screens and callbacks are tested
without Telegram or a database. Acceptance: full buy flow navigation
(locations -> plans -> OS -> confirm -> buy), idempotent replay, strict
ownership on servers, explicit power confirmation, wallet screens.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from cloud_platform.bot.monthly_ui import MonthlyBotUi
from cloud_platform.core.i18n import Locale, Translator
from cloud_platform.modules.checkout.service import (
    MonthlyCheckoutResult,
    OfferCatalogView,
    OfferConfirmView,
    OfferOsOptionView,
)
from cloud_platform.modules.compute.domain import (
    BILLING_MODEL_PREPAID_MONTHLY,
    CloudServer,
    ServerLifecycleState,
)
from cloud_platform.modules.navigation.domain import Callback, decode_callback, encode_callback
from cloud_platform.modules.offers.domain import SellableOffer
from cloud_platform.modules.renewals.domain import RenewalRecord, RenewalStatus
from cloud_platform.modules.users.domain import User
from cloud_platform.modules.wallet.domain import Hold, Wallet

KEY = "test-signing-key"
USER_A = User(id=uuid4(), username="alice", email="alice@t.me")
USER_B = User(id=uuid4(), username="bob", email="bob@t.me")
OFFER_ID = uuid4()
SERVER_ID = uuid4()


def _offer() -> SellableOffer:
    return SellableOffer(
        id=OFFER_ID,
        provider_key="leaseweb",
        product_id="VPS02_1",
        location_id="AMS-01",
        name="VPS S",
        vcpu=2,
        ram_gb=4,
        disk_gb=100,
        traffic="10 TB",
        provider_cost_minor=999,
        provider_cost_currency="EUR",
        selling_price_minor=1299,
        selling_currency="EUR",
        billing_parameters={},
        provider_available=True,
        enabled=True,
    )


def _server(owner: UUID | None = None) -> CloudServer:
    if owner is None:
        owner = USER_A.id or uuid4()
    return CloudServer(
        id=SERVER_ID,
        user_id=owner,
        provider_key="leaseweb",
        provider_account_id=uuid4(),
        state=ServerLifecycleState.RUNNING,
        billing_model=BILLING_MODEL_PREPAID_MONTHLY,
        provider_server_id="vps-1",
        ipv4="1.2.3.4",
        os="Ubuntu 24.04",
    )


class FakeOffersView:
    def __init__(self) -> None:
        self.confirm_views: list[OfferConfirmView] = []

    @staticmethod
    def _cb(flow: str, screen: str, *args: str) -> str:
        return encode_callback(Callback(flow=flow, screen=screen, args=args), KEY)

    async def plans_screen(self, location_id: str) -> tuple[list[OfferCatalogView], str, str]:
        view = OfferCatalogView(
            offer_id=OFFER_ID,
            provider_key="leaseweb",
            product_id="VPS02_1",
            location_id=location_id,
            name="VPS S",
            vcpu=2,
            ram_gb=4,
            disk_gb=100,
            traffic="10 TB",
            monthly_price_minor=1299,
            currency="EUR",
        )
        return [view], self._cb("offers", "locations"), self._cb("main", "menu")

    async def os_screen(
        self, *, offer_id: UUID
    ) -> tuple[OfferCatalogView, list[OfferOsOptionView], str, str]:
        view = OfferCatalogView(
            offer_id=offer_id,
            provider_key="leaseweb",
            product_id="VPS02_1",
            location_id="AMS-01",
            name="VPS S",
            vcpu=2,
            ram_gb=4,
            disk_gb=100,
            traffic="10 TB",
            monthly_price_minor=1299,
            currency="EUR",
        )
        option = OfferOsOptionView(
            name="Ubuntu 24.04",
            price_minor=0,
            index=0,
            select_callback=self._cb("offers", "confirm", str(offer_id), "0"),
        )
        return view, [option], self._cb("offers", "plans", "AMS-01"), self._cb("main", "menu")

    async def os_by_index(self, offer: SellableOffer, index: int) -> str:
        return "Ubuntu 24.04"

    async def confirmation(
        self, *, user_id: UUID, offer_id: UUID, os_index: int
    ) -> OfferConfirmView:
        view = OfferConfirmView(
            offer=OfferCatalogView(
                offer_id=offer_id,
                provider_key="leaseweb",
                product_id="VPS02_1",
                location_id="AMS-01",
                name="VPS S",
                vcpu=2,
                ram_gb=4,
                disk_gb=100,
                traffic="10 TB",
                monthly_price_minor=1299,
                currency="EUR",
            ),
            os_name="Ubuntu 24.04",
            balance_minor=10_000,
            currency="EUR",
            sufficient=True,
            confirm_callback=self._cb("offers", "buy", str(offer_id), str(os_index)),
            back_callback=self._cb("offers", "os", str(offer_id)),
            cancel_callback=self._cb("main", "menu"),
        )
        self.confirm_views.append(view)
        return view


class FakeCheckout:
    def __init__(self, *, replay: bool = False) -> None:
        self.replay = replay
        self.calls: list[tuple[User, UUID, str, str]] = []

    async def create_order(
        self, *, user: User, offer_id: UUID, os_name: str, idempotency_key: str
    ) -> MonthlyCheckoutResult:
        self.calls.append((user, offer_id, os_name, idempotency_key))
        server = _server(user.id or uuid4())
        order = type("Order", (), {"id": uuid4()})()
        hold = Hold(wallet_id=uuid4(), amount=1299, currency="EUR", idempotency_key="k")
        return MonthlyCheckoutResult(
            server=server, order=order, hold=hold, offer=_offer(), replayed=self.replay
        )


class FakeServers:
    def __init__(self, servers: list[CloudServer] | None = None) -> None:
        self.servers = servers or [_server()]

    async def list_by_user(self, user_id: UUID) -> list[CloudServer]:
        return [s for s in self.servers if s.user_id == user_id]

    async def get(self, server_id: UUID) -> CloudServer | None:
        return next((s for s in self.servers if s.id == server_id), None)


class FakeOrders:
    def __init__(self) -> None:
        self.order = type(
            "Order",
            (),
            {"offer_id": OFFER_ID, "provider_order_id": "LS-ORD-1"},
        )()

    async def get_by_server(self, server_id: UUID) -> Any:
        return self.order


class FakeOffersRepo:
    def __init__(self, offers: list[SellableOffer] | None = None) -> None:
        self.offers = offers or [_offer()]

    async def list_provider_locations(self) -> list[tuple[str, str]]:
        return [(o.provider_key, o.location_id) for o in self.offers]

    async def get(self, offer_id: UUID) -> SellableOffer | None:
        return next((o for o in self.offers if o.id == offer_id), None)


class FakeRenewals:
    async def get(self, server_id: UUID) -> RenewalRecord | None:
        return RenewalRecord(
            server_id=server_id,
            purchased_at=datetime(2026, 9, 1, tzinfo=UTC),
            provider_renewal_at=datetime(2026, 9, 30, tzinfo=UTC),
            customer_price_minor=1299,
            currency="EUR",
            status=RenewalStatus.ACTIVE,
        )


class FakeWalletHistory:
    def __init__(self) -> None:
        self.wallet = Wallet(user_id=USER_A.id or uuid4(), balance=10_000, currency="EUR")

    async def balance(self, user_id: UUID) -> Any:
        return type(
            "Balance",
            (),
            {
                "has_wallet": True,
                "balance_minor": 10_000,
                "currency": "EUR",
                "formatted": "100.00 EUR",
            },
        )()

    async def history(self, user_id: UUID, limit: int = 20) -> Any:
        return type("Page", (), {"items": []})()


class FakePower:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, UUID, str]] = []

    async def power_on(self, user_id: UUID, server_id: UUID, idempotency_key: str) -> Any:
        self.calls.append((user_id, server_id, "power_on"))
        return None

    async def power_off(self, user_id: UUID, server_id: UUID, idempotency_key: str) -> Any:
        self.calls.append((user_id, server_id, "power_off"))
        return None

    async def reboot(self, user_id: UUID, server_id: UUID, idempotency_key: str) -> Any:
        self.calls.append((user_id, server_id, "reboot"))
        return None


def _ui(
    *,
    checkout: FakeCheckout | None = None,
    servers: FakeServers | None = None,
    power: FakePower | None = None,
    offers: FakeOffersRepo | None = None,
) -> tuple[MonthlyBotUi, dict[str, Any]]:
    checkout = checkout or FakeCheckout()
    servers = servers or FakeServers()
    power = power or FakePower()
    offers = offers or FakeOffersRepo()
    ui = MonthlyBotUi(
        KEY,
        offers_view=FakeOffersView(),
        checkout=checkout,
        servers=servers,
        orders=FakeOrders(),
        renewals=FakeRenewals(),
        offers_repo=offers,
        wallet_history=FakeWalletHistory(),
        power=power,
        translator=Translator(Locale.EN),
    )
    deps = {"checkout": checkout, "servers": servers, "power": power, "offers": offers}
    return ui, deps


def _assert_callback_target(data: str, flow: str, screen: str) -> None:
    cb = decode_callback(data, KEY)
    assert cb.flow == flow
    assert cb.screen == screen


class TestBuyFlow:
    async def test_full_flow_to_order(self) -> None:
        ui, deps = _ui()
        # locations
        screen = await ui.handle(ui._callback("offers", "locations"), user=USER_A)
        assert "Choose a location" in screen.text
        plans_button = screen.keyboard.inline_keyboard[0][0]
        _assert_callback_target(plans_button.callback_data, "offers", "plans")
        # plans
        screen = await ui.handle(plans_button.callback_data, user=USER_A)
        os_button = screen.keyboard.inline_keyboard[0][0]
        _assert_callback_target(os_button.callback_data, "offers", "os")
        # os
        screen = await ui.handle(os_button.callback_data, user=USER_A)
        confirm_button = screen.keyboard.inline_keyboard[0][0]
        _assert_callback_target(confirm_button.callback_data, "offers", "confirm")
        # confirm
        screen = await ui.handle(confirm_button.callback_data, user=USER_A)
        assert "12.99 EUR" in screen.text
        buy_button = screen.keyboard.inline_keyboard[0][0]
        _assert_callback_target(buy_button.callback_data, "offers", "buy")
        # buy -> order created
        screen = await ui.handle(buy_button.callback_data, user=USER_A)
        assert "Your order is registered" in screen.text
        assert len(deps["checkout"].calls) == 1
        _, offer_id, os_name, ik = deps["checkout"].calls[0]
        assert offer_id == OFFER_ID
        assert os_name == "Ubuntu 24.04"
        assert ik.startswith("bot-monthly:")

    async def test_replay_shows_replayed_notice(self) -> None:
        ui, _deps = _ui(checkout=FakeCheckout(replay=True))
        cb = ui._callback("offers", "buy", str(OFFER_ID), "0")
        screen = await ui.handle(cb, user=USER_A)
        assert "not charged twice" in screen.text

    async def test_tampered_callback_falls_through(self) -> None:
        ui, _ = _ui()
        screen = await ui.handle("v1|offers:locations|deadbeef", user=USER_A)
        assert screen is None  # caller renders the tamper notice


class TestServersOwnership:
    async def test_list_shows_only_own_servers(self) -> None:
        alice_server = _server(USER_A.id or uuid4())
        bob_server = _server(USER_B.id or uuid4())
        ui, _deps = _ui(servers=FakeServers([alice_server, bob_server]))
        cb = ui._callback("servers", "list")
        screen = await ui.handle(cb, user=USER_A)
        assert "My servers" in screen.text
        buttons = screen.keyboard.inline_keyboard
        assert len(buttons) == 2  # one server row + menu
        detail_button = buttons[0][0]
        _assert_callback_target(detail_button.callback_data, "servers", "detail")

    async def test_user_b_cannot_view_user_a_server(self) -> None:
        ui, _ = _ui()
        cb = ui._callback("servers", "detail", str(SERVER_ID))
        screen = await ui.handle(cb, user=USER_B)
        assert "Server not found" in screen.text

    async def test_detail_shows_ips_and_renewal(self) -> None:
        ui, _ = _ui()
        cb = ui._callback("servers", "detail", str(SERVER_ID))
        screen = await ui.handle(cb, user=USER_A)
        assert "Server details" in screen.text
        assert "1.2.3.4" in screen.text
        assert "2026-09-30" in screen.text

    async def test_power_off_requires_confirmation_then_executes(self) -> None:
        ui, deps = _ui()
        confirm_cb = ui._callback("servers", "power_confirm", str(SERVER_ID), "power_off")
        screen = await ui.handle(confirm_cb, user=USER_A)
        assert "Confirm operation" in screen.text
        exec_button = screen.keyboard.inline_keyboard[0][0]
        _assert_callback_target(exec_button.callback_data, "servers", "power")
        screen = await ui.handle(exec_button.callback_data, user=USER_A)
        assert "completed on your server" in screen.text
        assert deps["power"].calls == [(USER_A.id, SERVER_ID, "power_off")]

    async def test_power_on_user_b_blocked(self) -> None:
        ui, deps = _ui()
        cb = ui._callback("servers", "power", str(SERVER_ID), "power_on")
        screen = await ui.handle(cb, user=USER_B)
        assert "Server not found" in screen.text
        assert deps["power"].calls == []


class TestWalletAndSupport:
    async def test_wallet_balance_screen(self) -> None:
        ui, _ = _ui()
        cb = ui._callback("wallet", "balance")
        screen = await ui.handle(cb, user=USER_A)
        assert "Your wallet" in screen.text
        assert "100.00 EUR" in screen.text

    async def test_support_screen(self) -> None:
        ui = MonthlyBotUi(
            KEY,
            offers_view=FakeOffersView(),
            checkout=FakeCheckout(),
            servers=FakeServers(),
            orders=FakeOrders(),
            renewals=FakeRenewals(),
            offers_repo=FakeOffersRepo(),
            wallet_history=FakeWalletHistory(),
            power=FakePower(),
            support_contact="@support",
            translator=Translator(Locale.EN),
        )
        cb = ui._callback("support", "contact")
        screen = await ui.handle(cb, user=USER_A)
        assert "Support" in screen.text
        assert "@support" in screen.text
