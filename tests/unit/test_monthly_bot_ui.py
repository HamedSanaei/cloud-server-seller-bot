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
from cloud_platform.modules.servers.confirmations import ConfirmationStatus
from cloud_platform.modules.servers.models import (
    CustomerServerPage,
    CustomerServerState,
    CustomerServerView,
    ServerActionOutcome,
    ServerOperation,
)
from cloud_platform.modules.servers.policies import ServerManagementPolicy
from cloud_platform.modules.servers.service import (
    ServerConfirmationError,
    ServerNotFoundError,
)
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
                "formatted": "€100.00",
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


class FakeManagement:
    """The slice of ``ServerManagementService`` the storefront's UI uses.

    Ownership is enforced exactly like the real service (a foreign row is
    indistinguishable from a missing one) so the delegation seam keeps the
    same guarantees. The full action surface is covered in
    ``test_servers_management_ui.py``.
    """

    def __init__(self, servers: list[CloudServer] | None = None) -> None:
        rows = servers if servers is not None else [_server()]
        self.servers = {server.id: server for server in rows}
        self.policy = ServerManagementPolicy()
        self.calls: list[tuple[str, UUID, UUID]] = []
        self.tokens: list[str] = []
        self.consumed: set[str] = set()

    def _owned(self, customer_id: UUID, server_id: UUID) -> CloudServer:
        server = self.servers.get(server_id)
        if server is None or server.user_id != customer_id:
            raise ServerNotFoundError("server not found")
        return server

    @staticmethod
    def _view(server: CloudServer) -> CustomerServerView:
        return CustomerServerView(
            server_id=server.id,
            state=CustomerServerState.RUNNING,
            ip=server.ipv4,
            operating_system=server.os,
        )

    async def list_servers(self, customer_id: UUID, *, page: int = 1) -> CustomerServerPage:
        rows = sorted(
            (s for s in self.servers.values() if s.user_id == customer_id),
            key=lambda s: str(s.id),
        )
        items = tuple(self._view(server) for server in rows)
        return CustomerServerPage(items=items, page=page, page_size=5, total=len(items))

    async def get_server(self, customer_id: UUID, server_id: UUID) -> CustomerServerView:
        self.calls.append(("get_server", customer_id, server_id))
        return self._view(self._owned(customer_id, server_id))

    async def refresh_server(self, customer_id: UUID, server_id: UUID) -> CustomerServerView:
        return self._view(self._owned(customer_id, server_id))

    async def issue_confirmation(
        self, customer_id: UUID, server_id: UUID, operation: ServerOperation, **_: Any
    ) -> str:
        self._owned(customer_id, server_id)
        token = f"tok-{len(self.tokens)}"
        self.tokens.append(token)
        return token

    async def start_server(
        self, customer_id: UUID, server_id: UUID, **_kwargs: Any
    ) -> ServerActionOutcome:
        self._owned(customer_id, server_id)
        self.calls.append(("start_server", customer_id, server_id))
        return ServerActionOutcome(operation=ServerOperation.START, accepted=True)

    async def reboot_server(
        self, customer_id: UUID, server_id: UUID, **_kwargs: Any
    ) -> ServerActionOutcome:
        self._owned(customer_id, server_id)
        self.calls.append(("reboot_server", customer_id, server_id))
        return ServerActionOutcome(operation=ServerOperation.REBOOT, accepted=True)

    async def stop_server(
        self,
        customer_id: UUID,
        server_id: UUID,
        *,
        confirmation_token: str | None = None,
        **_kwargs: Any,
    ) -> ServerActionOutcome:
        if not confirmation_token:
            raise ServerConfirmationError(
                "confirmation required", status=ConfirmationStatus.INVALID
            )
        if confirmation_token in self.consumed:
            raise ServerConfirmationError("replayed", status=ConfirmationStatus.REPLAYED)
        self.consumed.add(confirmation_token)
        self._owned(customer_id, server_id)
        self.calls.append(("stop_server", customer_id, server_id))
        return ServerActionOutcome(operation=ServerOperation.STOP, accepted=True)


def _ui(
    *,
    checkout: FakeCheckout | None = None,
    servers: FakeServers | None = None,
    power: FakePower | None = None,
    offers: FakeOffersRepo | None = None,
    management: FakeManagement | None = None,
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
        server_management=management,  # type: ignore[arg-type]
    )
    deps = {
        "checkout": checkout,
        "servers": servers,
        "power": power,
        "offers": offers,
        "management": management,
    }
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
        assert "€12.99" in screen.text
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


class TestServersFlowDelegation:
    """The storefront owns the MARKET/store path; My Servers is delegated.

    The detailed flows (actions, confirmations, snapshots, IPs...) live in
    ``test_servers_management_ui.py``; what matters here is the seam: the
    storefront routes ``servers.*`` to the management UI, adds its own plan
    and renewal lines to the details screen, and stays honest when the
    management service is not configured at all.
    """

    async def test_list_shows_only_own_servers(self) -> None:
        management = FakeManagement([_server(USER_A.id or uuid4())])
        ui, _deps = _ui(management=management)

        mine = await ui.handle(ui._callback("servers", "list"), user=USER_A)
        theirs = await ui.handle(ui._callback("servers", "list"), user=USER_B)

        assert "My servers" in mine.text
        assert "1.2.3.4" in mine.text
        manage = [b for row in mine.keyboard.inline_keyboard for b in row if "Manage" in b.text]
        assert len(manage) == 1
        # The button carries an opaque reference, never the local UUID.
        assert str(SERVER_ID) not in manage[0].callback_data
        assert "no servers yet" in theirs.text.lower()

    async def test_empty_list_when_the_customer_has_no_servers(self) -> None:
        ui, _ = _ui(management=FakeManagement([]))
        screen = await ui.handle(ui._callback("servers", "list"), user=USER_A)
        assert "no servers yet" in screen.text.lower()

    async def test_user_b_cannot_view_user_a_server(self) -> None:
        ui, deps = _ui(management=FakeManagement([_server(USER_A.id)]))
        # USER_B can only ever hold a reference minted for USER_B, and the
        # service refuses the row anyway.
        screen = await ui.handle(ui._callback("servers", "view", "deadbeef"), user=USER_B)
        assert "expired" in screen.text.lower()
        assert deps["management"].calls == []

    async def test_detail_combines_provider_view_with_storefront_facts(self) -> None:
        ui, _ = _ui(management=FakeManagement([_server(USER_A.id)]))
        ref = await ui._servers_ui.ref_for(USER_A, SERVER_ID)
        screen = await ui.handle(ui._callback("servers", "view", str(ref)), user=USER_A)
        assert "Server details" in screen.text
        assert "1.2.3.4" in screen.text  # provider-neutral view
        assert "VPS S" in screen.text  # the local offer
        assert "2026-09-30" in screen.text  # the local renewal

    async def test_power_off_requires_confirmation_then_executes_once(self) -> None:
        ui, deps = _ui(management=FakeManagement([_server(USER_A.id)]))
        management = deps["management"]
        ref = await ui._servers_ui.ref_for(USER_A, SERVER_ID)

        confirm = await ui.handle(ui._callback("servers", "pwr", str(ref), "off"), user=USER_A)
        assert "Confirm operation" in confirm.text
        assert management.calls == []

        exec_button = next(
            b for row in confirm.keyboard.inline_keyboard for b in row if "Do it" in b.text
        )
        screen = await ui.handle(exec_button.callback_data, user=USER_A)
        again = await ui.handle(exec_button.callback_data, user=USER_A)

        assert "Power off" in screen.text
        assert [call[0] for call in management.calls] == ["stop_server"]
        assert "already running" in again.text

    async def test_power_on_user_b_blocked(self) -> None:
        ui, deps = _ui(management=FakeManagement([_server(USER_A.id)]))
        screen = await ui.handle(ui._callback("servers", "pwr", "deadbeef", "on"), user=USER_B)
        assert "expired" in screen.text.lower()
        assert deps["management"].calls == []

    async def test_without_the_management_service_the_flow_is_disabled(self) -> None:
        ui, _deps = _ui()
        screen = await ui.handle(ui._callback("servers", "list"), user=USER_A)
        assert "not enabled" in screen.text.lower()


class TestWalletAndSupport:
    async def test_wallet_balance_screen(self) -> None:
        ui, _ = _ui()
        cb = ui._callback("wallet", "balance")
        screen = await ui.handle(cb, user=USER_A)
        assert "Your wallet" in screen.text
        assert "€100.00" in screen.text

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
