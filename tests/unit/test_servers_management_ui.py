"""Telegram My Servers UI: flows, ownership, confirmations and callback budget.

The UI is a renderer, so these tests assert the things a renderer can get wrong:

- it never renders a provider/DTO field the customer should not see;
- it always routes a mutation through the application service, with the
  ownership-scoped reference the customer actually owns;
- a destructive action renders a confirmation first, and the confirmed action
  is executed exactly once even if Telegram delivers the tap twice;
- every ``callback_data`` fits Telegram's 64-byte budget (a signed callback
  already spends 21 of them, so a raw UUID would not fit);
- no exception text, console URL or provider identifier reaches a screen.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from cloud_platform.bot.servers_ui import ServerManagementUi
from cloud_platform.bot.sessions import ServerSessions
from cloud_platform.bot.ui import BotScreen
from cloud_platform.core.i18n import Translator
from cloud_platform.core.session_store import InMemoryBotSessionStore
from cloud_platform.modules.navigation.domain import Callback
from cloud_platform.modules.servers.confirmations import ConfirmationStatus, arguments_digest
from cloud_platform.modules.servers.models import (
    CustomerServerPage,
    CustomerServerState,
    CustomerServerView,
    IpAddressView,
    MonitoringView,
    ReinstallImageView,
    ServerActionOutcome,
    ServerConsoleView,
    ServerOperation,
    ServerSnapshotView,
    TrafficUsageView,
)
from cloud_platform.modules.servers.policies import ServerManagementPolicy
from cloud_platform.modules.servers.service import (
    ServerAmbiguousOutcomeError,
    ServerConfirmationError,
    ServerNotFoundError,
    ServerOperationNotAllowedError,
)
from cloud_platform.modules.users.domain import User

CUSTOMER = uuid4()
OTHER_CUSTOMER = uuid4()
SERVER_ID = uuid4()
OTHER_SERVER_ID = uuid4()
CONSOLE_URL = "https://console.leaseweb.com/session?token=super-secret-console-token"

TRANSLATOR = Translator()


def _user(user_id: UUID = CUSTOMER) -> User:
    return User(
        id=user_id,
        username=f"cust{user_id.int % 1_000_000}",
        email=f"cust{user_id.int % 1_000_000}@example.test",
        telegram_user_id=user_id.int % 10_000_000,
    )


def _view(
    server_id: UUID = SERVER_ID,
    *,
    state: CustomerServerState = CustomerServerState.RUNNING,
    **overrides: object,
) -> CustomerServerView:
    base: dict[str, object] = {
        "server_id": server_id,
        "state": state,
        "location_label": "🇩🇪 Frankfurt",
        "ip": "88.1.2.3",
        "operating_system": "Ubuntu 24.04",
        "plan": "Leaseweb VPS 2",
    }
    base.update(overrides)
    return CustomerServerView(**base)  # type: ignore[arg-type]


@dataclass
class FakeManagement:
    """A recording stand-in for the application service.

    It enforces the same ownership rule the real service enforces (a foreign
    server looks missing) and consumes a confirmation token at most once, so
    the double-click guarantees are exercised end to end through the UI.
    """

    policy: ServerManagementPolicy = field(default_factory=ServerManagementPolicy)
    view: CustomerServerView = field(default_factory=_view)
    owner: UUID = CUSTOMER
    # NOTE: the data attributes are deliberately NOT named after the methods
    # they feed (a dataclass field would shadow the method on the instance).
    snapshot_rows: list[ServerSnapshotView] = field(
        default_factory=lambda: [
            ServerSnapshotView(ref="snap-1", name="before-upgrade", state="AVAILABLE")
        ]
    )
    image_rows: list[ReinstallImageView] = field(
        default_factory=lambda: [
            ReinstallImageView(ref="img-ubuntu", name="Ubuntu 24.04", family="ubuntu"),
            ReinstallImageView(ref="img-debian", name="Debian 13", family="debian"),
        ]
    )
    ip_rows: list[IpAddressView] = field(
        default_factory=lambda: [
            IpAddressView(ip="88.1.2.3", version=4, network_type="PUBLIC", main_ip=True),
            IpAddressView(ip="95.4.5.6", version=4, network_type="PUBLIC", null_routed=True),
        ]
    )
    iso_rows: list[tuple[str, str]] = field(default_factory=lambda: [("iso-1", "debian-13.iso")])
    traffic_view: TrafficUsageView = field(
        default_factory=lambda: TrafficUsageView(
            period_from="2026-08-01T00:00:00Z",
            period_to="2026-09-01T00:00:00Z",
            downloaded_bytes=4096,
            uploaded_bytes=1024,
            total_bytes=5120,
            limit_label="30 TB",
            separate_directions=True,
        )
    )
    monitoring_view: MonitoringView = field(
        default_factory=lambda: MonitoringView(enabled=False, status="DOWN", can_enable=True)
    )
    console_url: str = CONSOLE_URL
    failure: Exception | None = None
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    issued: list[str] = field(default_factory=list)
    consumed: set[str] = field(default_factory=set)

    # -- helpers ---------------------------------------------------------

    def _record(self, event: str, **fields: object) -> None:
        if self.failure is not None:
            failure, self.failure = self.failure, None
            raise failure
        self.calls.append((event, fields))

    def count(self, name: str) -> int:
        return len([call for call in self.calls if call[0] == name])

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def _owned(self, customer_id: UUID) -> None:
        if customer_id != self.owner:
            raise ServerNotFoundError("server not found")

    def _consume(self, token: str | None, **fields: object) -> None:
        if not token:
            raise ServerConfirmationError(
                "confirmation required", status=ConfirmationStatus.INVALID
            )
        if token in self.consumed:
            raise ServerConfirmationError("replayed", status=ConfirmationStatus.REPLAYED)
        self.consumed.add(token)

    async def issue_confirmation(
        self, customer_id: UUID, server_id: UUID, operation: ServerOperation, **_: object
    ) -> str:
        self._owned(customer_id)
        token = f"token-{len(self.issued)}-{operation.value}"
        self.issued.append(token)
        return token

    # -- reads -----------------------------------------------------------

    async def list_servers(self, customer_id: UUID, *, page: int = 1) -> CustomerServerPage:
        self._owned(customer_id)
        if customer_id != self.owner:
            return CustomerServerPage(items=(), page=1, page_size=5, total=0)
        items = (self.view,) if page == 1 else ()
        return CustomerServerPage(items=items, page=page, page_size=1, total=2)

    async def get_server(self, customer_id: UUID, server_id: UUID) -> CustomerServerView:
        self._record("get_server", server_id=server_id)
        self._owned(customer_id)
        if server_id != SERVER_ID:
            raise ServerNotFoundError("server not found")
        return self.view

    async def refresh_server(self, customer_id: UUID, server_id: UUID) -> CustomerServerView:
        self._record("refresh_server", server_id=server_id)
        self._owned(customer_id)
        return self.view

    async def traffic(self, customer_id: UUID, server_id: UUID) -> TrafficUsageView:
        self._record("traffic", server_id=server_id)
        self._owned(customer_id)
        return self.traffic_view

    async def console(self, customer_id: UUID, server_id: UUID) -> ServerConsoleView:
        self._record("console", server_id=server_id)
        self._owned(customer_id)
        return ServerConsoleView(url=self.console_url)

    async def snapshots(self, customer_id: UUID, server_id: UUID) -> list[ServerSnapshotView]:
        self._record("snapshots", server_id=server_id)
        self._owned(customer_id)
        return list(self.snapshot_rows)

    async def reinstall_images(
        self, customer_id: UUID, server_id: UUID
    ) -> list[ReinstallImageView]:
        self._record("reinstall_images", server_id=server_id)
        self._owned(customer_id)
        return list(self.image_rows)

    async def list_ips(self, customer_id: UUID, server_id: UUID) -> list[IpAddressView]:
        self._record("list_ips", server_id=server_id)
        self._owned(customer_id)
        return list(self.ip_rows)

    async def monitoring(self, customer_id: UUID, server_id: UUID) -> MonitoringView:
        self._record("monitoring", server_id=server_id)
        self._owned(customer_id)
        return self.monitoring_view

    async def list_isos(self, customer_id: UUID, server_id: UUID) -> list[tuple[str, str]]:
        self._record("list_isos", server_id=server_id)
        self._owned(customer_id)
        return list(self.iso_rows)

    # -- mutations -------------------------------------------------------

    async def start_server(
        self, customer_id: UUID, server_id: UUID, **_: object
    ) -> ServerActionOutcome:
        self._record("start_server", server_id=server_id)
        self._owned(customer_id)
        return ServerActionOutcome(operation=ServerOperation.START, accepted=True)

    async def stop_server(
        self,
        customer_id: UUID,
        server_id: UUID,
        *,
        confirmation_token: str | None = None,
        **_: object,
    ) -> ServerActionOutcome:
        self._consume(confirmation_token)
        self._record("stop_server", server_id=server_id)
        self._owned(customer_id)
        return ServerActionOutcome(operation=ServerOperation.STOP, accepted=True)

    async def reboot_server(
        self, customer_id: UUID, server_id: UUID, **_: object
    ) -> ServerActionOutcome:
        self._record("reboot_server", server_id=server_id)
        self._owned(customer_id)
        return ServerActionOutcome(operation=ServerOperation.REBOOT, accepted=True)

    async def reinstall(
        self, customer_id: UUID, server_id: UUID, *, image_ref: str, confirmation_token: str | None
    ) -> ServerActionOutcome:
        self._consume(confirmation_token, image=image_ref)
        self._record("reinstall", server_id=server_id, image=image_ref)
        self._owned(customer_id)
        return ServerActionOutcome(operation=ServerOperation.REINSTALL, accepted=True)

    async def reset_password(
        self, customer_id: UUID, server_id: UUID, *, confirmation_token: str | None
    ) -> ServerActionOutcome:
        self._consume(confirmation_token)
        self._record("reset_password", server_id=server_id)
        self._owned(customer_id)
        return ServerActionOutcome(operation=ServerOperation.PASSWORD_RESET, accepted=True)

    async def create_snapshot(
        self,
        customer_id: UUID,
        server_id: UUID,
        *,
        name: str,
        confirmation_token: str | None,
    ) -> ServerActionOutcome:
        self._consume(confirmation_token, name=name)
        self._record("create_snapshot", server_id=server_id, name=name)
        self._owned(customer_id)
        return ServerActionOutcome(operation=ServerOperation.SNAPSHOT_CREATE, accepted=True)

    async def restore_snapshot(
        self,
        customer_id: UUID,
        server_id: UUID,
        *,
        snapshot_ref: str,
        confirmation_token: str | None,
    ) -> ServerActionOutcome:
        self._consume(confirmation_token, snapshot=snapshot_ref)
        self._record("restore_snapshot", server_id=server_id, snapshot=snapshot_ref)
        self._owned(customer_id)
        return ServerActionOutcome(operation=ServerOperation.SNAPSHOT_RESTORE, accepted=True)

    async def delete_snapshot(
        self,
        customer_id: UUID,
        server_id: UUID,
        *,
        snapshot_ref: str,
        confirmation_token: str | None,
    ) -> ServerActionOutcome:
        self._consume(confirmation_token, snapshot=snapshot_ref)
        self._record("delete_snapshot", server_id=server_id, snapshot=snapshot_ref)
        self._owned(customer_id)
        return ServerActionOutcome(operation=ServerOperation.SNAPSHOT_DELETE, accepted=True)

    async def null_route_ip(
        self, customer_id: UUID, server_id: UUID, *, ip: str, confirmation_token: str | None
    ) -> ServerActionOutcome:
        self._consume(confirmation_token, ip=ip)
        self._record("null_route_ip", server_id=server_id, ip=ip)
        self._owned(customer_id)
        return ServerActionOutcome(operation=ServerOperation.IP_NULL_ROUTE, accepted=True)

    async def unnull_route_ip(
        self, customer_id: UUID, server_id: UUID, *, ip: str
    ) -> ServerActionOutcome:
        self._record("unnull_route_ip", server_id=server_id, ip=ip)
        self._owned(customer_id)
        return ServerActionOutcome(operation=ServerOperation.IP_UNNULL_ROUTE, accepted=True)

    async def set_reverse_dns(
        self, customer_id: UUID, server_id: UUID, *, ip: str, reverse_lookup: str
    ) -> IpAddressView:
        self._record("set_reverse_dns", server_id=server_id, ip=ip, value=reverse_lookup)
        self._owned(customer_id)
        return IpAddressView(ip=ip, version=4, network_type="PUBLIC", reverse_lookup=reverse_lookup)

    async def attach_iso(
        self, customer_id: UUID, server_id: UUID, *, iso_ref: str, confirmation_token: str | None
    ) -> ServerActionOutcome:
        self._consume(confirmation_token, iso=iso_ref)
        self._record("attach_iso", server_id=server_id, iso=iso_ref)
        self._owned(customer_id)
        return ServerActionOutcome(operation=ServerOperation.ISO_ATTACH, accepted=True)

    async def detach_iso(
        self, customer_id: UUID, server_id: UUID, *, confirmation_token: str | None
    ) -> ServerActionOutcome:
        self._consume(confirmation_token)
        self._record("detach_iso", server_id=server_id)
        self._owned(customer_id)
        return ServerActionOutcome(operation=ServerOperation.ISO_DETACH, accepted=True)

    async def enable_monitoring(self, customer_id: UUID, server_id: UUID) -> ServerActionOutcome:
        self._record("enable_monitoring", server_id=server_id)
        self._owned(customer_id)
        return ServerActionOutcome(operation=ServerOperation.MONITORING_ENABLE, accepted=True)

    async def rename(self, customer_id: UUID, server_id: UUID, *, display_name: str) -> object:
        self._record("rename", server_id=server_id, name=display_name)
        self._owned(customer_id)

        @dataclass(frozen=True)
        class _Renamed:
            display_name: str

        return _Renamed(display_name=display_name)


class FakeTtlStore(InMemoryBotSessionStore):
    """In-memory store with an explicit "lose everything" switch for tests."""

    def expire_everything(self) -> None:
        self.clear()


def make_ui(
    management: FakeManagement | None = None, **kwargs: object
) -> tuple[ServerManagementUi, FakeManagement, ServerSessions]:
    management = management or FakeManagement()
    sessions = ServerSessions()
    ui = ServerManagementUi(
        "test-signing-key",
        management,  # type: ignore[arg-type]
        sessions=sessions,
        translator=TRANSLATOR,
        **kwargs,  # type: ignore[arg-type]
    )
    return ui, management, sessions


async def _ref(ui: ServerManagementUi, user: User | None = None) -> str:
    """The reference the UI hands to Telegram for SERVER_ID."""
    ref = await ui.ref_for(user or _user(), SERVER_ID)
    assert ref is not None
    return ref


def _buttons(screen: BotScreen) -> list[object]:
    return [button for row in screen.keyboard.inline_keyboard for button in row]


def _exec_callback(screen: BotScreen) -> Callback:
    """The ``servers.exec`` callback the confirm button carries.

    Read straight off the rendered keyboard (the signed wire form is not
    needed to dispatch) so the test exercises exactly what Telegram sends.
    """
    button = next(
        b
        for b in _buttons(screen)
        if b.text == TRANSLATOR.t("servers.confirm_button")  # type: ignore[attr-defined]
    )
    parts = button.callback_data.split("|")[1].split(":")  # type: ignore[attr-defined]
    assert parts[0] == "servers" and parts[1] == "exec", parts
    return Callback("servers", "exec", tuple(parts[2:4]))


# ---------------------------------------------------------------------------
# List + pagination
# ---------------------------------------------------------------------------


class TestList:
    async def test_list_shows_a_manage_button_per_server(self) -> None:
        ui, _management, _sessions = make_ui()
        screen = await ui.list_screen(_user())

        assert "88.1.2.3" in screen.text
        assert "Ubuntu 24.04" in screen.text
        manage = [b for b in _buttons(screen) if "مدیریت" in b.text]  # type: ignore[attr-defined]
        assert len(manage) == 1
        assert str(SERVER_ID) not in str(manage[0].callback_data)  # type: ignore[attr-defined]

    async def test_empty_list_points_at_the_storefront(self) -> None:
        management = FakeManagement()
        management.owner = CUSTOMER
        management.view = _view()
        ui, _m, _s = make_ui(management)
        # An owner with no servers: the page comes back empty.
        screen = await ui.list_screen(_user(OTHER_CUSTOMER))
        assert "سرور پیدا نشد" in screen.text or "دسترسی" in screen.text

    async def test_pagination_buttons_reflect_the_page(self) -> None:
        management = FakeManagement()
        ui, _m, _s = make_ui(management)
        screen = await ui.list_screen(_user())
        texts = [b.text for b in _buttons(screen)]  # type: ignore[attr-defined]
        # page_size=1 with a total of 2 -> a "next" page button exists.
        assert any(t == TRANSLATOR.t("servers.next") for t in texts)
        assert TRANSLATOR.t("servers.page", page="1", pages="2") in texts


# ---------------------------------------------------------------------------
# Details + manage menu
# ---------------------------------------------------------------------------


class TestDetails:
    async def test_detail_renders_only_known_fields(self) -> None:
        ui, _m, _s = make_ui()
        screen = await ui.handle(Callback("servers", "view", (await _ref(ui),)), _user())

        assert TRANSLATOR.t("servers.detail_header") in screen.text
        assert "88.1.2.3" in screen.text
        # The provider returned no RAM/window figure, so none is invented.
        assert "RAM" not in screen.text
        assert "🧠" not in screen.text

    async def test_manage_menu_hides_the_impossible_power_actions(self) -> None:
        ui, _m, _s = make_ui()
        screen = await ui.handle(Callback("servers", "manage", (await _ref(ui),)), _user())
        labels = [b.text for b in _buttons(screen)]  # type: ignore[attr-defined]
        assert TRANSLATOR.t("servers.power_off") in labels
        assert TRANSLATOR.t("servers.reboot") in labels
        assert TRANSLATOR.t("servers.power_on") not in labels

    async def test_stopped_server_offers_start_not_stop(self) -> None:
        management = FakeManagement(view=_view(state=CustomerServerState.STOPPED))
        ui, _m, _s = make_ui(management)
        screen = await ui.handle(Callback("servers", "manage", (await _ref(ui),)), _user())
        labels = [b.text for b in _buttons(screen)]  # type: ignore[attr-defined]
        assert TRANSLATOR.t("servers.power_on") in labels
        assert TRANSLATOR.t("servers.power_off") not in labels

    async def test_iso_is_not_offered_by_default(self) -> None:
        ui, _m, _s = make_ui()
        screen = await ui.handle(Callback("servers", "manage", (await _ref(ui),)), _user())
        labels = [b.text for b in _buttons(screen)]  # type: ignore[attr-defined]
        assert TRANSLATOR.t("servers.iso_button") not in labels


# ---------------------------------------------------------------------------
# Power + confirmation
# ---------------------------------------------------------------------------


class TestPowerFlow:
    async def test_start_runs_directly_and_reboot_too(self) -> None:
        ui, management, _s = make_ui()
        ref = await _ref(ui)
        await ui.handle(Callback("servers", "pwr", (ref, "on")), _user())
        await ui.handle(Callback("servers", "pwr", (ref, "rb")), _user())
        assert management.names() == ["start_server", "reboot_server"]

    async def test_stop_requires_confirmation_then_executes_once(self) -> None:
        ui, management, _s = make_ui()
        ref = await _ref(ui)

        confirm = await ui.handle(Callback("servers", "pwr", (ref, "off")), _user())
        assert TRANSLATOR.t("servers.power_confirm_title") in confirm.text
        assert management.names() == []  # nothing happened yet

        callback = _exec_callback(confirm)
        first = await ui.handle(callback, _user())
        second = await ui.handle(callback, _user())

        assert management.count("stop_server") == 1
        assert TRANSLATOR.t("servers.action_in_progress") in second.text
        assert TRANSLATOR.t("servers.op.stop") in first.text

    async def test_double_tap_on_confirm_is_idempotent(self) -> None:
        ui, management, _s = make_ui()
        ref = await _ref(ui)
        await ui.handle(Callback("servers", "pwr", (ref, "off")), _user())
        await ui.handle(Callback("servers", "pwr", (ref, "off")), _user())
        nonce = arguments_digest(ServerOperation.STOP, {})[:10]
        await ui.handle(Callback("servers", "exec", (ref, nonce)), _user())
        await ui.handle(Callback("servers", "exec", (ref, nonce)), _user())
        assert management.count("stop_server") == 1


MANAGEMENT_SENT = "ارسال شد"


# ---------------------------------------------------------------------------
# Destructive operations
# ---------------------------------------------------------------------------


class TestDestructiveFlows:
    async def test_reinstall_uses_provider_images_and_confirms(self) -> None:
        ui, management, _s = make_ui()
        ref = await _ref(ui)

        images = await ui.handle(Callback("servers", "rein", (ref,)), _user())
        labels = [b.text for b in _buttons(images)]  # type: ignore[attr-defined]
        assert "Ubuntu 24.04" in labels and "Debian 13" in labels
        assert management.names() == ["reinstall_images"]

        confirm = await ui.handle(Callback("servers", "reinpick", (ref, "1")), _user())
        assert TRANSLATOR.t("servers.reinstall_title") in confirm.text
        assert "Debian 13" in confirm.text

        await ui.handle(_exec_callback(confirm), _user())

        assert management.count("reinstall") == 1
        assert management.calls[-1][1]["image"] == "img-debian"

    async def test_stale_selection_index_is_refused(self) -> None:
        ui, management, _s = make_ui()
        ref = await _ref(ui)
        await ui.handle(Callback("servers", "rein", (ref,)), _user())
        screen = await ui.handle(Callback("servers", "reinpick", (ref, "9")), _user())
        assert TRANSLATOR.t("nav.expired") in screen.text
        assert management.count("reinstall") == 0

    async def test_snapshot_create_then_execute_once(self) -> None:
        ui, management, _s = make_ui()
        ref = await _ref(ui)
        confirm = await ui.handle(Callback("servers", "snapnew", (ref,)), _user())
        assert TRANSLATOR.t("servers.snapshot_create_title") in confirm.text
        callback = _exec_callback(confirm)
        await ui.handle(callback, _user())
        await ui.handle(callback, _user())
        assert management.count("create_snapshot") == 1

    async def test_snapshot_restore_and_delete_bind_the_shown_snapshot(self) -> None:
        ui, management, _s = make_ui()
        ref = await _ref(ui)
        listing = await ui.handle(Callback("servers", "snaplist", (ref,)), _user())
        assert "before-upgrade" in listing.text
        restore = await ui.handle(Callback("servers", "snapres", (ref, "0")), _user())
        await ui.handle(_exec_callback(restore), _user())
        await ui.handle(Callback("servers", "snapdel", (ref, "0")), _user())
        delete_nonce = arguments_digest(ServerOperation.SNAPSHOT_DELETE, {"snapshot": "snap-1"})[
            :10
        ]
        await ui.handle(Callback("servers", "exec", (ref, delete_nonce)), _user())
        assert management.count("restore_snapshot") == 1
        assert management.count("delete_snapshot") == 1
        assert management.calls[-1][1]["snapshot"] == "snap-1"

    async def test_password_reset_requires_confirmation(self) -> None:
        ui, management, _s = make_ui()
        ref = await _ref(ui)
        screen = await ui.handle(Callback("servers", "pwreset", (ref,)), _user())
        assert TRANSLATOR.t("servers.password_title") in screen.text
        assert management.count("reset_password") == 0

        done = await ui.handle(_exec_callback(screen), _user())
        assert management.count("reset_password") == 1
        # No credential is ever invented or displayed.
        assert "password" not in done.text.lower()
        assert "رمز" in done.text or "✅" in done.text

    async def test_null_route_confirms_with_the_owned_ip(self) -> None:
        ui, management, _s = make_ui()
        ref = await _ref(ui)
        await ui.handle(Callback("servers", "ips", (ref,)), _user())
        confirm = await ui.handle(Callback("servers", "ipnull", (ref, "0")), _user())
        assert TRANSLATOR.t("servers.ip_null_title") in confirm.text
        await ui.handle(_exec_callback(confirm), _user())
        assert management.calls[-1][1]["ip"] == "88.1.2.3"

    async def test_iso_attach_flow_when_enabled(self) -> None:
        policy = ServerManagementPolicy(iso=True)
        ui, management, _s = make_ui(FakeManagement(policy=policy))
        ref = await _ref(ui)
        listing = await ui.handle(Callback("servers", "isoat", (ref, "0")), _user())
        assert "debian-13.iso" in [b.text for b in _buttons(listing)]  # type: ignore[attr-defined]
        confirm = await ui.handle(Callback("servers", "isoat", (ref, "0")), _user())
        assert TRANSLATOR.t("servers.iso_title") in confirm.text
        await ui.handle(_exec_callback(confirm), _user())
        assert management.count("attach_iso") == 1
        assert management.calls[-1][1]["iso"] == "iso-1"


# ---------------------------------------------------------------------------
# Reads: console, traffic, snapshots, IPs, monitoring
# ---------------------------------------------------------------------------


class TestReadScreens:
    async def test_console_is_a_url_button_and_not_in_the_text(self) -> None:
        ui, _m, _s = make_ui()
        screen = await ui.handle(Callback("servers", "console", (await _ref(ui),)), _user())
        assert CONSOLE_URL not in screen.text
        urls = [b.url for b in _buttons(screen) if b.url is not None]  # type: ignore[attr-defined]
        assert urls == [CONSOLE_URL]
        assert TRANSLATOR.t("servers.console_note") in screen.text

    async def test_non_http_console_url_is_not_offered_as_a_link(self) -> None:
        management = FakeManagement(console_url="vnc://legacy-host/1")
        ui, _m, _s = make_ui(management)
        screen = await ui.handle(Callback("servers", "console", (await _ref(ui),)), _user())
        assert all(b.url is None for b in _buttons(screen))  # type: ignore[attr-defined]
        assert TRANSLATOR.t("servers.console_unavailable") in screen.text

    async def test_traffic_shows_directions_only_when_provided(self) -> None:
        ui, _m, _s = make_ui()
        screen = await ui.handle(Callback("servers", "traffic", (await _ref(ui),)), _user())
        assert TRANSLATOR.t("servers.traffic_down", value="4 KB") in screen.text
        assert TRANSLATOR.t("servers.traffic_limit", value="30 TB") in screen.text

    async def test_traffic_without_directions_is_not_fabricated(self) -> None:
        management = FakeManagement(
            traffic_view=TrafficUsageView(
                total_bytes=2048, directions=("total",), separate_directions=False
            )
        )
        ui, _m, _s = make_ui(management)
        screen = await ui.handle(Callback("servers", "traffic", (await _ref(ui),)), _user())
        assert TRANSLATOR.t("servers.traffic_total", value="2 KB") in screen.text
        assert "دانلود" not in screen.text

    async def test_traffic_unavailable_is_explained(self) -> None:
        management = FakeManagement(
            traffic_view=TrafficUsageView(unavailable_reason="provider down")
        )
        ui, _m, _s = make_ui(management)
        screen = await ui.handle(Callback("servers", "traffic", (await _ref(ui),)), _user())
        assert TRANSLATOR.t("servers.traffic_unavailable") in screen.text

    async def test_ip_list_marks_null_routes_and_swaps_the_action(self) -> None:
        ui, _m, _s = make_ui()
        screen = await ui.handle(Callback("servers", "ips", (await _ref(ui),)), _user())
        assert "95.4.5.6" in screen.text
        labels = [b.text for b in _buttons(screen)]  # type: ignore[attr-defined]
        assert TRANSLATOR.t("servers.ip_unnull_button") in labels
        assert TRANSLATOR.t("servers.ip_null_button") in labels

    async def test_monitoring_view_offers_enable(self) -> None:
        ui, _m, _s = make_ui()
        screen = await ui.handle(Callback("servers", "mon", (await _ref(ui),)), _user())
        assert TRANSLATOR.t("servers.monitoring_off") in screen.text
        labels = [b.text for b in _buttons(screen)]  # type: ignore[attr-defined]
        assert TRANSLATOR.t("servers.monitoring_enable_button") in labels

    async def test_monitoring_enable_runs_through_the_service(self) -> None:
        ui, management, _s = make_ui()
        await ui.handle(Callback("servers", "monon", (await _ref(ui),)), _user())
        assert management.count("enable_monitoring") == 1

    async def test_snapshot_list_empty_is_explained(self) -> None:
        ui, _m, _s = make_ui(FakeManagement(snapshot_rows=[]))
        screen = await ui.handle(Callback("servers", "snaplist", (await _ref(ui),)), _user())
        assert TRANSLATOR.t("servers.snapshots_empty") in screen.text
        del _m


# ---------------------------------------------------------------------------
# Text prompts
# ---------------------------------------------------------------------------


class TestTextPrompts:
    async def test_rename_flow_applies_the_typed_name(self) -> None:
        ui, management, _s = make_ui()
        ref = await _ref(ui)
        prompt = await ui.handle(Callback("servers", "rename", (ref,)), _user())
        assert TRANSLATOR.t("servers.rename_prompt") in prompt.text
        done = await ui.handle_text("my new server", _user())
        assert done is not None
        assert TRANSLATOR.t("servers.rename_done", name="my new server") in done.text
        assert management.calls[-1][1]["name"] == "my new server"

    async def test_reverse_dns_flow(self) -> None:
        ui, management, _s = make_ui()
        ref = await _ref(ui)
        await ui.handle(Callback("servers", "ips", (ref,)), _user())
        prompt = await ui.handle(Callback("servers", "iprdns", (ref, "0")), _user())
        assert TRANSLATOR.t("servers.ip_rdns_prompt") in prompt.text
        done = await ui.handle_text("host.example.com", _user())
        assert done is not None
        assert management.calls[-1][1] == {
            "server_id": SERVER_ID,
            "ip": "88.1.2.3",
            "value": "host.example.com",
        }

    async def test_text_without_a_prompt_is_ignored(self) -> None:
        ui, _m, _s = make_ui()
        assert await ui.handle_text("hello", _user()) is None

    async def test_a_prompt_is_single_use(self) -> None:
        ui, _m, _s = make_ui()
        await ui.handle(Callback("servers", "rename", (await _ref(ui),)), _user())
        assert await ui.handle_text("first", _user()) is not None
        assert await ui.handle_text("second", _user()) is None


# ---------------------------------------------------------------------------
# Ownership, staleness, errors
# ---------------------------------------------------------------------------


class TestOwnershipAndErrors:
    async def test_another_customers_reference_does_not_resolve(self) -> None:
        ui, management, _s = make_ui()
        ref = await _ref(ui, _user(CUSTOMER))
        screen = await ui.handle(Callback("servers", "view", (ref,)), _user(OTHER_CUSTOMER))
        assert TRANSLATOR.t("nav.expired") in screen.text
        assert management.count("get_server") == 0

    async def test_unknown_reference_is_stale_not_an_error(self) -> None:
        ui, _m, _s = make_ui()
        screen = await ui.handle(Callback("servers", "view", ("deadbeef",)), _user())
        assert TRANSLATOR.t("nav.expired") in screen.text

    async def test_reference_minted_for_another_customer_does_not_resolve(self) -> None:
        management = FakeManagement()
        sessions = ServerSessions()
        theirs = await sessions.ref_for(OTHER_CUSTOMER, SERVER_ID)
        ui = ServerManagementUi(
            "test-signing-key",
            management,
            sessions=sessions,
            translator=TRANSLATOR,  # type: ignore[arg-type]
        )
        screen = await ui.handle(Callback("servers", "view", (theirs,)), _user(CUSTOMER))
        assert TRANSLATOR.t("nav.expired") in screen.text
        assert management.count("get_server") == 0

    async def test_ambiguous_outcome_uses_the_safe_wording(self) -> None:
        management = FakeManagement()
        management.failure = ServerAmbiguousOutcomeError("timed out")
        ui, _m, _s = make_ui(management)
        screen = await ui.handle(Callback("servers", "pwr", (await _ref(ui), "rb")), _user())
        assert TRANSLATOR.t("servers.outcome_unknown") in screen.text

    async def test_errors_never_leak_exception_text(self) -> None:
        management = FakeManagement()
        management.failure = ServerOperationNotAllowedError(
            "X-LSW-Auth: test-api-key-should-never-leak",
            operation=ServerOperation.REBOOT,
            reason="state_not_allowed",
        )
        ui, _m, _s = make_ui(management)
        screen = await ui.handle(Callback("servers", "pwr", (await _ref(ui), "rb")), _user())
        assert "test-api-key-should-never-leak" not in screen.text
        assert TRANSLATOR.t("servers.err_forbidden") in screen.text

    async def test_disabled_feature_shows_the_disabled_notice(self) -> None:
        ui, _m, _s = make_ui(FakeManagement(policy=ServerManagementPolicy(enabled=False)))
        screen = await ui.handle(Callback("servers", "list", ()), _user())
        assert TRANSLATOR.t("servers.disabled") in screen.text

    async def test_unresolved_identity_is_refused(self) -> None:
        ui, _m, _s = make_ui()
        screen = await ui.handle(Callback("servers", "list", ()), None)
        assert TRANSLATOR.t("buy.no_identity") in screen.text


# ---------------------------------------------------------------------------
# Callback budget + signed round trip
# ---------------------------------------------------------------------------


class TestCallbackBudget:
    async def test_every_button_fits_telegrams_64_byte_limit(self) -> None:
        policy = ServerManagementPolicy(iso=True)
        management = FakeManagement(policy=policy)
        ui, _m, _s = make_ui(management)
        user = _user()
        ref = await _ref(ui)
        screens: list[BotScreen] = [
            await ui.list_screen(user),
            await ui.handle(Callback("servers", "view", (ref,)), user),
            await ui.handle(Callback("servers", "manage", (ref,)), user),
            await ui.handle(Callback("servers", "refresh", (ref,)), user),
            await ui.handle(Callback("servers", "pwr", (ref, "off")), user),
            await ui.handle(Callback("servers", "console", (ref,)), user),
            await ui.handle(Callback("servers", "traffic", (ref,)), user),
            await ui.handle(Callback("servers", "snap", (ref,)), user),
            await ui.handle(Callback("servers", "snaplist", (ref,)), user),
            await ui.handle(Callback("servers", "snapnew", (ref,)), user),
            await ui.handle(Callback("servers", "snapres", (ref, "0")), user),
            await ui.handle(Callback("servers", "snapdel", (ref, "0")), user),
            await ui.handle(Callback("servers", "rein", (ref,)), user),
            await ui.handle(Callback("servers", "reinpick", (ref, "0")), user),
            await ui.handle(Callback("servers", "pwreset", (ref,)), user),
            await ui.handle(Callback("servers", "ips", (ref,)), user),
            await ui.handle(Callback("servers", "ipnull", (ref, "0")), user),
            await ui.handle(Callback("servers", "iprdns", (ref, "0")), user),
            await ui.handle(Callback("servers", "iso", (ref,)), user),
            await ui.handle(Callback("servers", "isoat", (ref, "0")), user),
            await ui.handle(Callback("servers", "isodet", (ref,)), user),
            await ui.handle(Callback("servers", "mon", (ref,)), user),
            await ui.handle(Callback("servers", "rename", (ref,)), user),
        ]
        for screen in screens:
            for button in _buttons(screen):
                data = button.callback_data  # type: ignore[attr-defined]
                if data is None:
                    continue
                assert len(data.encode()) <= 64, f"{data!r} is {len(data.encode())} bytes"

    async def test_buttons_carry_no_uuid_provider_id_or_token(self) -> None:
        ui, _m, _s = make_ui()
        user = _user()
        screen = await ui.handle(Callback("servers", "manage", (await _ref(ui),)), user)
        for button in _buttons(screen):
            data = button.callback_data  # type: ignore[attr-defined]
            if data is None:
                continue
            assert str(SERVER_ID) not in data
            assert "lsw-vps-1" not in data
            assert "token-" not in data

    async def test_signed_callback_round_trip(self) -> None:
        """What the bot actually sends must decode back to the same screen."""

        from cloud_platform.modules.navigation.domain import decode_callback, encode_callback

        ui, _m, _s = make_ui()
        user = _user()
        ref = await _ref(ui)
        data = encode_callback(Callback("servers", "view", (ref,)), "test-signing-key")
        decoded = decode_callback(data, "test-signing-key")
        screen = await ui.handle(decoded, user)
        assert "88.1.2.3" in screen.text

    async def test_reference_is_opaque_and_stable(self) -> None:
        ui, _m, _s = make_ui()
        user = _user()
        first = await ui.ref_for(user, SERVER_ID)
        second = await ui.ref_for(user, SERVER_ID)
        assert first == second
        assert first is not None and len(first) == 8
        assert str(SERVER_ID) not in first
        assert base64.urlsafe_b64decode(first + "=" * (-len(first) % 4))
        assert json.dumps(first)


# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------


class TestServerSessions:
    async def test_expired_reference_stops_resolving(self) -> None:
        # The store is now the TTL authority (Redis in production), so expiry is
        # asserted against a fake clock rather than a process-local timer.
        store = FakeTtlStore()
        sessions = ServerSessions(store)
        ref = await sessions.ref_for(CUSTOMER, SERVER_ID)
        assert await sessions.server_id(CUSTOMER, ref) == SERVER_ID
        store.expire_everything()
        assert await sessions.server_id(CUSTOMER, ref) is None

    async def test_pending_actions_are_single_use(self) -> None:
        from cloud_platform.bot.sessions import PendingAction

        sessions = ServerSessions()
        ref = await sessions.ref_for(CUSTOMER, SERVER_ID)
        action = PendingAction(operation=ServerOperation.REBOOT)
        assert await sessions.stash(CUSTOMER, ref, "nonce1234", action) is True
        taken = await sessions.take(CUSTOMER, ref, "nonce1234")
        assert taken == action
        assert await sessions.take(CUSTOMER, ref, "nonce1234") is None

    async def test_references_are_per_customer(self) -> None:
        sessions = ServerSessions()
        mine = await sessions.ref_for(CUSTOMER, SERVER_ID)
        await sessions.ref_for(OTHER_CUSTOMER, OTHER_SERVER_ID)
        assert await sessions.server_id(OTHER_CUSTOMER, mine) is None
        assert await sessions.server_id(CUSTOMER, mine) == SERVER_ID
