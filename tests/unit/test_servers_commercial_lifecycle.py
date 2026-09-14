"""Commercial lifecycle in My Servers (PROD-HARDENING §17-§38).

Two guarantees are asserted here, and they are the ones that cost money:

1. **Infrastructure state and commercial state are separate facts.** A server
   can be ``running`` (what the provider says) and ``payment_due`` (what the
   customer owes) at the same time, and the screen shows both without deriving
   one from the other.
2. **A renewal settles exactly once.** The manual "renew now" path reuses the
   worker's idempotent debit and the shared one-time confirmation, so a double
   click, a replayed token or a second replica can never charge twice — and it
   never calls a provider API.

The renewal amount always comes from the LOCAL commercial record, so a change
in the provider's current price can never reprice an existing customer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from cloud_platform.bot.servers_ui import ServerManagementUi
from cloud_platform.bot.sessions import ServerSessions
from cloud_platform.bot.ui import BotScreen
from cloud_platform.core.i18n import Translator
from cloud_platform.core.session_store import InMemoryBotSessionStore, SessionStoreUnavailable
from cloud_platform.modules.businesslog.domain import BusinessEvent, BusinessEventType
from cloud_platform.modules.compute.domain import CloudServer, ServerLifecycleState
from cloud_platform.modules.navigation.domain import Callback
from cloud_platform.modules.renewals.domain import RenewalStatus
from cloud_platform.modules.servers.confirmations import ConfirmationVerifier
from cloud_platform.modules.servers.models import (
    CustomerServerState,
    CustomerServerView,
    ServerOperation,
    ServerRenewalView,
)
from cloud_platform.modules.servers.policies import ServerManagementPolicy
from cloud_platform.modules.servers.service import (
    ServerConfirmationError,
    ServerManagementService,
    ServerNotFoundError,
    ServerOperationNotAllowedError,
)
from cloud_platform.modules.users.domain import User

CUSTOMER = uuid4()
OTHER_CUSTOMER = uuid4()
SERVER_ID = uuid4()
PROVIDER_ID = "lsw-vps-1"
PRICE = 1299
TRANSLATOR = Translator()


# ---------------------------------------------------------------------------
# Records / fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeCommercialRecord:
    """The slice of ``RenewalRecord`` the customer surface reads."""

    status: RenewalStatus = RenewalStatus.ACTIVE
    customer_price_minor: int = PRICE
    currency: str = "EUR"
    provider_renewal_at: datetime | None = None
    grace_until: datetime | None = None
    auto_charge_enabled: bool = True

    @property
    def payable(self) -> bool:
        return self.status in (
            RenewalStatus.PAYMENT_DUE,
            RenewalStatus.GRACE_PERIOD,
            RenewalStatus.SUSPENDED,
        )


class FakeRenewalCollector:
    """A recording stand-in for ``RenewalChecker``.

    It reproduces the two behaviours the service depends on: the debit is
    idempotent per period (a second settle reports ``already_charged`` without
    taking money again), and the auto-renew preference round-trips.
    """

    def __init__(
        self,
        *,
        record: FakeCommercialRecord | None = None,
        settle_reason: str | None = None,
        balance: int = 10_000,
    ) -> None:
        self.record = record
        self.settle_reason = settle_reason
        self.balance = balance
        self.settles = 0
        self.toggles: list[bool] = []
        self.period_end = datetime(2026, 10, 1, tzinfo=UTC)

    async def record_for(self, server_id: UUID) -> Any | None:
        return self.record if server_id == SERVER_ID else None

    async def settle_now(self, server_id: UUID) -> Any:
        if self.record is None:
            return _outcome(server_id, "no_renewal_record")
        if self.settle_reason is not None:
            return _outcome(server_id, self.settle_reason, record=self.record)
        self.settles += 1
        self.balance -= self.record.customer_price_minor
        self.record.status = RenewalStatus.ACTIVE
        self.record.grace_until = None
        self.record.provider_renewal_at = self.period_end
        return _outcome(server_id, "charged", record=self.record)

    async def set_auto_renew(self, server_id: UUID, enabled: bool) -> Any | None:
        if self.record is None or server_id != SERVER_ID:
            return None
        self.toggles.append(bool(enabled))
        self.record.auto_charge_enabled = bool(enabled)
        return self.record


def _outcome(server_id: UUID, reason: str, *, record: Any | None = None) -> Any:
    """A ``RenewalNowOutcome``-shaped result (duck-typed on purpose)."""

    @dataclass(frozen=True)
    class _Outcome:
        server_id: UUID
        reason: str
        settled: bool = False
        status: RenewalStatus = RenewalStatus.ACTIVE
        amount_minor: int = PRICE
        currency: str = "EUR"
        period_end: datetime | None = None
        grace_until: datetime | None = None
        auto_renew_enabled: bool = True

    settled = reason in ("charged", "already_charged")
    return _Outcome(
        server_id=server_id,
        reason=reason,
        settled=settled,
        status=record.status if record is not None else RenewalStatus.ACTIVE,
        amount_minor=record.customer_price_minor if record is not None else PRICE,
        currency=record.currency if record is not None else "EUR",
        period_end=record.provider_renewal_at if record is not None else None,
        auto_renew_enabled=record.auto_charge_enabled if record is not None else True,
    )


class FakeServerRepo:
    def __init__(self, servers: list[CloudServer]) -> None:
        self.rows = {server.id: server for server in servers}

    async def get(self, server_id: UUID) -> CloudServer | None:
        return self.rows.get(server_id)

    async def list_by_user_paged(
        self, user_id: UUID, *, offset: int, limit: int
    ) -> tuple[list[CloudServer], int]:
        owned = [s for s in self.rows.values() if s.user_id == user_id]
        return owned[offset : offset + limit], len(owned)

    async def save(self, server: CloudServer) -> CloudServer:
        self.rows[server.id] = server
        return server


class FakeAuditRepo:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> Any:
        self.events.append(event)
        return event

    @property
    def text(self) -> str:
        return "\n".join(
            f"{getattr(e, 'action', '')} {getattr(e, 'reason', '')} {getattr(e, 'metadata', {})}"
            for e in self.events
        )


class FakeEventSink:
    def __init__(self) -> None:
        self.events: list[BusinessEvent] = []

    async def emit(self, event: BusinessEvent) -> bool:
        self.events.append(event)
        return True

    @property
    def types(self) -> list[BusinessEventType]:
        return [event.event_type for event in self.events]


class UnusedRegistry:
    """Commercial operations are provider-agnostic: the registry is never used."""

    def get(self, key: str) -> Any:
        raise AssertionError("no provider lookup may happen on the commercial path")


def _server(*, user_id: UUID = CUSTOMER) -> CloudServer:
    return CloudServer(
        id=SERVER_ID,
        user_id=user_id,
        provider_key="leaseweb",
        provider_account_id=uuid4(),
        state=ServerLifecycleState.RUNNING,
        provider_server_id=PROVIDER_ID,
        created_at=datetime.now(UTC),
    )


def make_service(
    *,
    collector: FakeRenewalCollector | None = None,
    policy: ServerManagementPolicy | None = None,
) -> tuple[ServerManagementService, FakeRenewalCollector, FakeAuditRepo, FakeEventSink]:
    collector = collector or FakeRenewalCollector()
    audit = FakeAuditRepo()
    sink = FakeEventSink()
    service = ServerManagementService(
        servers=FakeServerRepo([_server()]),  # type: ignore[arg-type]
        registry=UnusedRegistry(),  # type: ignore[arg-type]
        policy=policy or ServerManagementPolicy(),
        confirmations=ConfirmationVerifier("test-signing-key", ttl_seconds=900),
        audit_repo=audit,  # type: ignore[arg-type]
        event_sink=sink,
        renewal_collector=collector,
    )
    return service, collector, audit, sink


def _payable_record() -> FakeCommercialRecord:
    return FakeCommercialRecord(
        status=RenewalStatus.PAYMENT_DUE,
        provider_renewal_at=datetime(2026, 9, 1, tzinfo=UTC),
        grace_until=datetime(2026, 9, 3, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# Commercial status on the view
# ---------------------------------------------------------------------------


class TestCommercialStatus:
    async def test_the_view_carries_the_commercial_status(self) -> None:
        service, *_ = make_service(collector=FakeRenewalCollector(record=_payable_record()))
        view = await service.get_server(CUSTOMER, SERVER_ID)
        assert view.commercial_status == RenewalStatus.PAYMENT_DUE.value
        assert view.commercial_payable is True
        assert view.auto_renew_enabled is True
        assert view.renewal_price_minor == PRICE
        assert view.renewal_currency == "EUR"
        assert view.grace_until == datetime(2026, 9, 3, tzinfo=UTC)

    async def test_infrastructure_and_commercial_state_are_independent(self) -> None:
        """A RUNNING server that owes money is a normal combination (§18)."""
        service, *_ = make_service(collector=FakeRenewalCollector(record=_payable_record()))
        view = await service.get_server(CUSTOMER, SERVER_ID)
        assert view.state is CustomerServerState.RUNNING
        assert view.commercial_status == RenewalStatus.PAYMENT_DUE.value

    async def test_a_server_without_a_record_reports_no_commercial_state(self) -> None:
        service, *_ = make_service(collector=FakeRenewalCollector(record=None))
        view = await service.get_server(CUSTOMER, SERVER_ID)
        assert view.commercial_status is None
        assert view.has_commercial_record is False
        assert view.commercial_payable is False
        assert view.auto_renew_enabled is None

    async def test_a_broken_commercial_read_never_hides_the_server(self) -> None:
        class Broken(FakeRenewalCollector):
            async def record_for(self, server_id: UUID) -> Any | None:
                raise RuntimeError("billing backend down")

        service, *_ = make_service(collector=Broken(record=_payable_record()))
        view = await service.get_server(CUSTOMER, SERVER_ID)
        assert view.server_id == SERVER_ID
        assert view.commercial_status is None

    async def test_a_deployment_without_a_collector_still_reads(self) -> None:
        service = ServerManagementService(
            servers=FakeServerRepo([_server()]),  # type: ignore[arg-type]
            registry=UnusedRegistry(),  # type: ignore[arg-type]
            policy=ServerManagementPolicy(),
            confirmations=ConfirmationVerifier("test-signing-key", ttl_seconds=900),
            audit_repo=FakeAuditRepo(),  # type: ignore[arg-type]
        )
        view = await service.get_server(CUSTOMER, SERVER_ID)
        assert view.server_id == SERVER_ID
        assert view.commercial_status is None


# ---------------------------------------------------------------------------
# renew now
# ---------------------------------------------------------------------------


class TestRenewNow:
    async def _token(self, service: ServerManagementService) -> str:
        return await service.issue_confirmation(
            CUSTOMER, SERVER_ID, ServerOperation.RENEW_NOW, arguments={}
        )

    async def test_renew_now_requires_a_confirmation_token(self) -> None:
        service, collector, audit, sink = make_service(
            collector=FakeRenewalCollector(record=_payable_record())
        )
        with pytest.raises(ServerConfirmationError):
            await service.renew_now(CUSTOMER, SERVER_ID, confirmation_token=None)
        assert collector.settles == 0
        assert audit.events == []
        assert sink.events == []

    async def test_a_confirmed_renewal_settles_once_and_logs_it(self) -> None:
        service, collector, audit, sink = make_service(
            collector=FakeRenewalCollector(record=_payable_record())
        )
        token = await self._token(service)
        result = await service.renew_now(CUSTOMER, SERVER_ID, confirmation_token=token)

        assert result.settled is True
        assert result.reason == "charged"
        assert result.amount_minor == PRICE
        assert collector.settles == 1
        assert BusinessEventType.SERVICE_RENEWAL_SUCCEEDED in sink.types
        assert "service.renewal_requested" in audit.text

    async def test_a_replayed_token_can_never_charge_twice(self) -> None:
        service, collector, *_ = make_service(
            collector=FakeRenewalCollector(record=_payable_record())
        )
        token = await self._token(service)
        first = await service.renew_now(CUSTOMER, SERVER_ID, confirmation_token=token)
        second = await service.renew_now(CUSTOMER, SERVER_ID, confirmation_token=token)

        assert first.settled is True
        assert second.reason == "already_charged"
        assert collector.settles == 1

    async def test_a_foreign_renewal_is_indistinguishable_from_missing(self) -> None:
        service, collector, audit, sink = make_service(
            collector=FakeRenewalCollector(record=_payable_record())
        )
        with pytest.raises(ServerNotFoundError):
            await service.renew_now(OTHER_CUSTOMER, SERVER_ID, confirmation_token="x")
        assert collector.settles == 0
        assert audit.events == []
        assert sink.events == []

    async def test_a_foreign_token_cannot_be_confirmed_by_another_customer(self) -> None:
        service, collector, *_ = make_service(
            collector=FakeRenewalCollector(record=_payable_record())
        )
        token = await self._token(service)
        with pytest.raises(ServerNotFoundError):
            await service.renew_now(OTHER_CUSTOMER, SERVER_ID, confirmation_token=token)
        assert collector.settles == 0

    @pytest.mark.parametrize(
        ("reason", "event_type"),
        [
            (
                "insufficient_funds",
                BusinessEventType.SERVICE_RENEWAL_FAILED_INSUFFICIENT_BALANCE,
            ),
            ("manual_review_required", BusinessEventType.SERVICE_OPERATOR_ATTENTION_REQUIRED),
        ],
    )
    async def test_a_failed_settlement_is_reported_not_invented(
        self, reason: str, event_type: BusinessEventType
    ) -> None:
        service, collector, _, sink = make_service(
            collector=FakeRenewalCollector(record=_payable_record(), settle_reason=reason)
        )
        token = await self._token(service)
        result = await service.renew_now(CUSTOMER, SERVER_ID, confirmation_token=token)

        assert result.settled is False
        assert result.reason == reason
        assert collector.settles == 0
        assert event_type in sink.types

    async def test_billing_disabled_hides_and_refuses_renewal(self) -> None:
        service, collector, *_ = make_service(
            collector=FakeRenewalCollector(record=_payable_record()),
            policy=ServerManagementPolicy(billing=False),
        )
        with pytest.raises(ServerOperationNotAllowedError):
            await service.issue_confirmation(
                CUSTOMER, SERVER_ID, ServerOperation.RENEW_NOW, arguments={}
            )
        with pytest.raises(ServerOperationNotAllowedError):
            await service.renew_now(CUSTOMER, SERVER_ID, confirmation_token="x")
        assert collector.settles == 0

    async def test_no_secret_reaches_audit_or_the_event_stream(self) -> None:
        service, _, audit, sink = make_service(
            collector=FakeRenewalCollector(record=_payable_record())
        )
        token = await self._token(service)
        await service.renew_now(CUSTOMER, SERVER_ID, confirmation_token=token)

        blob = (
            audit.text
            + "\n".join(str(event.sanitized_payload()) for event in sink.events)
            + "\n".join(str(event) for event in sink.events)
        )
        for secret in (token, PROVIDER_ID, "X-LSW-Auth", "LEASEWEB_API_KEY"):
            assert secret not in blob


# ---------------------------------------------------------------------------
# auto renew
# ---------------------------------------------------------------------------


class TestAutoRenew:
    async def test_toggling_off_persists_and_emits_an_event(self) -> None:
        service, collector, audit, sink = make_service(
            collector=FakeRenewalCollector(record=FakeCommercialRecord())
        )
        result = await service.set_auto_renew(CUSTOMER, SERVER_ID, enabled=False)

        assert result.auto_renew_enabled is False
        assert result.reason == "auto_renew_off"
        assert collector.toggles == [False]
        assert collector.record is not None
        assert collector.record.auto_charge_enabled is False
        assert BusinessEventType.SERVICE_AUTO_RENEW_DISABLED in sink.types
        assert "service.auto_renew_changed" in audit.text

    async def test_toggling_on_persists_and_emits_an_event(self) -> None:
        service, _collector, _, sink = make_service(
            collector=FakeRenewalCollector(record=FakeCommercialRecord(auto_charge_enabled=False))
        )
        result = await service.set_auto_renew(CUSTOMER, SERVER_ID, enabled=True)
        assert result.auto_renew_enabled is True
        assert result.reason == "auto_renew_on"
        assert BusinessEventType.SERVICE_AUTO_RENEW_ENABLED in sink.types

    async def test_a_foreign_toggle_is_refused_without_writing(self) -> None:
        service, collector, audit, sink = make_service(
            collector=FakeRenewalCollector(record=FakeCommercialRecord())
        )
        with pytest.raises(ServerNotFoundError):
            await service.set_auto_renew(OTHER_CUSTOMER, SERVER_ID, enabled=False)
        assert collector.toggles == []
        assert audit.events == []
        assert sink.events == []

    async def test_a_service_without_a_record_reports_unavailable(self) -> None:
        service, collector, _, sink = make_service(collector=FakeRenewalCollector(record=None))
        result = await service.set_auto_renew(CUSTOMER, SERVER_ID, enabled=False)
        assert result.auto_renew_enabled is None
        assert result.reason == "no_renewal_record"
        assert collector.toggles == []
        assert sink.events == []

    async def test_billing_disabled_refuses_the_toggle(self) -> None:
        service, collector, *_ = make_service(
            collector=FakeRenewalCollector(record=FakeCommercialRecord()),
            policy=ServerManagementPolicy(billing=False),
        )
        with pytest.raises(ServerOperationNotAllowedError):
            await service.set_auto_renew(CUSTOMER, SERVER_ID, enabled=False)
        assert collector.toggles == []


# ---------------------------------------------------------------------------
# Telegram rendering + session/confirmation integration
# ---------------------------------------------------------------------------


class FakeManagement:
    """The UI-facing slice of the service (records every call by name)."""

    def __init__(
        self,
        *,
        view: CustomerServerView,
        policy: ServerManagementPolicy | None = None,
        settlements: int = 1,
    ) -> None:
        self.policy = policy or ServerManagementPolicy()
        self.view = view
        self.settlements = settlements
        self.calls: list[str] = []
        self.tokens: dict[str, str] = {}
        self.consumed: set[str] = set()

    async def _owned(self, customer_id: UUID) -> None:
        if customer_id != CUSTOMER:
            raise ServerNotFoundError("server not found")

    async def list_servers(self, customer_id: UUID, *, page: int = 1) -> Any:
        from cloud_platform.modules.servers.models import CustomerServerPage

        await self._owned(customer_id)
        return CustomerServerPage(items=(self.view,), page=1, page_size=5, total=1)

    async def get_server(self, customer_id: UUID, server_id: UUID) -> CustomerServerView:
        await self._owned(customer_id)
        return self.view

    async def refresh_server(self, customer_id: UUID, server_id: UUID) -> CustomerServerView:
        await self._owned(customer_id)
        return self.view

    async def issue_confirmation(
        self, customer_id: UUID, server_id: UUID, operation: ServerOperation, **_: object
    ) -> str:
        await self._owned(customer_id)
        token = f"token-{operation.value}-{len(self.tokens)}"
        self.tokens[token] = operation.value
        return token

    async def renew_now(
        self,
        customer_id: UUID,
        server_id: UUID,
        *,
        confirmation_token: str | None,
    ) -> ServerRenewalView:
        await self._owned(customer_id)
        if not confirmation_token:
            raise ServerConfirmationError("confirmation required")
        self.calls.append("renew_now")
        if confirmation_token in self.consumed:
            return ServerRenewalView(server_id=server_id, reason="already_charged", settled=True)
        self.consumed.add(confirmation_token)
        return ServerRenewalView(
            server_id=server_id,
            reason="charged",
            settled=True,
            status="active",
            amount_minor=PRICE,
            currency="EUR",
            period_end=datetime(2026, 10, 1, tzinfo=UTC),
        )

    async def set_auto_renew(
        self, customer_id: UUID, server_id: UUID, *, enabled: bool
    ) -> ServerRenewalView:
        await self._owned(customer_id)
        self.calls.append(f"set_auto_renew:{enabled}")
        self.view = _view(auto_renew_enabled=enabled)
        return ServerRenewalView(
            server_id=server_id,
            reason="auto_renew_on" if enabled else "auto_renew_off",
            auto_renew_enabled=enabled,
        )


def _view(**overrides: object) -> CustomerServerView:
    base: dict[str, object] = {
        "server_id": SERVER_ID,
        "state": CustomerServerState.RUNNING,
        "location_label": "🇩🇪 Frankfurt",
        "ip": "88.1.2.3",
        "operating_system": "Ubuntu 24.04",
        "commercial_status": RenewalStatus.PAYMENT_DUE.value,
        "commercial_payable": True,
        "auto_renew_enabled": True,
        "grace_until": datetime(2026, 9, 3, tzinfo=UTC),
        "next_renewal_at": datetime(2026, 9, 1, tzinfo=UTC),
        "renewal_price_minor": PRICE,
        "renewal_currency": "EUR",
    }
    base.update(overrides)
    return CustomerServerView(**base)  # type: ignore[arg-type]


def _user(user_id: UUID = CUSTOMER) -> User:
    return User(
        id=user_id,
        username="cust",
        email="cust@example.test",
        telegram_user_id=1234,
    )


def _make_ui(
    management: FakeManagement | None = None,
    *,
    store: Any | None = None,
) -> tuple[ServerManagementUi, FakeManagement]:
    management = management or FakeManagement(view=_view())
    sessions = ServerSessions(store or InMemoryBotSessionStore())
    ui = ServerManagementUi(
        "test-signing-key",
        management,  # type: ignore[arg-type]
        sessions=sessions,
        translator=TRANSLATOR,
    )
    return ui, management


def _texts(screen: BotScreen) -> list[str]:
    return [
        button.text
        for row in screen.keyboard.inline_keyboard
        for button in row
        if getattr(button, "text", None)
    ]


def _button_for(screen: BotScreen, label: str) -> Any:
    for row in screen.keyboard.inline_keyboard:
        for button in row:
            if button.text == label:
                return button
    raise AssertionError(f"no button labelled {label!r}: {_texts(screen)}")


def _callback_of(screen: BotScreen, label: str) -> Callback:
    button = _button_for(screen, label)
    parts = button.callback_data.split("|")[1].split(":")
    return Callback("servers", parts[1], tuple(parts[2:]))


class TestCommercialScreens:
    async def test_details_show_both_states_separately(self) -> None:
        ui, _ = _make_ui()
        screen = await ui.list_screen(_user())
        ref = await ui.ref_for(_user(), SERVER_ID)
        assert ref is not None
        details = await ui.handle(Callback("servers", "view", (ref,)), _user())

        assert TRANSLATOR.t("servers.spec_state", value=TRANSLATOR.t("servers.state.running")) in (
            details.text
        )
        assert (
            TRANSLATOR.t(
                "servers.commercial.status",
                value=TRANSLATOR.t("servers.commercial.payment_due"),
            )
            in details.text
        )
        assert TRANSLATOR.t("servers.commercial.grace_until", value="2026-09-03") in details.text
        assert (
            TRANSLATOR.t(
                "servers.commercial.auto_renew",
                value=TRANSLATOR.t("servers.commercial.on"),
            )
            in details.text
        )
        assert screen is not None

    async def test_no_commercial_block_when_there_is_no_record(self) -> None:
        ui, _ = _make_ui(
            FakeManagement(
                view=_view(
                    commercial_status=None,
                    commercial_payable=False,
                    auto_renew_enabled=None,
                    renewal_price_minor=None,
                    renewal_currency=None,
                    grace_until=None,
                )
            )
        )
        ref = await ui.ref_for(_user(), SERVER_ID)
        assert ref is not None
        details = await ui.handle(Callback("servers", "view", (ref,)), _user())
        assert "commercial" not in details.text
        assert TRANSLATOR.t("servers.commercial.status", value="x") not in details.text

    async def test_manage_menu_offers_renewal_only_while_payable(self) -> None:
        ui, _ = _make_ui()
        ref = await ui.ref_for(_user(), SERVER_ID)
        assert ref is not None
        manage = await ui.handle(Callback("servers", "manage", (ref,)), _user())
        assert TRANSLATOR.t("servers.renew_button") in _texts(manage)
        assert TRANSLATOR.t("servers.auto_renew_off_button") in _texts(manage)

    async def test_manage_menu_hides_renewal_when_nothing_is_due(self) -> None:
        ui, _ = _make_ui(
            FakeManagement(
                view=_view(commercial_status=RenewalStatus.ACTIVE.value, commercial_payable=False)
            )
        )
        ref = await ui.ref_for(_user(), SERVER_ID)
        assert ref is not None
        manage = await ui.handle(Callback("servers", "manage", (ref,)), _user())
        assert TRANSLATOR.t("servers.renew_button") not in _texts(manage)

    async def test_manage_menu_hides_renewal_when_billing_is_off(self) -> None:
        ui, _ = _make_ui()
        ui._mgmt.policy = ServerManagementPolicy(billing=False)  # type: ignore[attr-defined]
        ref = await ui.ref_for(_user(), SERVER_ID)
        assert ref is not None
        manage = await ui.handle(Callback("servers", "manage", (ref,)), _user())
        assert TRANSLATOR.t("servers.renew_button") not in _texts(manage)
        assert TRANSLATOR.t("servers.auto_renew_off_button") not in _texts(manage)

    async def test_renewal_flow_confirms_then_settles_once(self) -> None:
        ui, management = _make_ui()
        ref = await ui.ref_for(_user(), SERVER_ID)
        assert ref is not None

        confirm = await ui.handle(Callback("servers", "renew", (ref,)), _user())
        assert TRANSLATOR.t("servers.renew_amount", amount="12.99 EUR") in confirm.text
        exec_cb = _callback_of(confirm, TRANSLATOR.t("servers.renew_confirm_button"))

        done = await ui.handle(exec_cb, _user())
        assert (
            TRANSLATOR.t("servers.renew_done", amount="12.99 EUR", value="2026-10-01") in done.text
        )
        assert management.calls.count("renew_now") == 1

    async def test_a_double_tap_reports_already_processed(self) -> None:
        ui, management = _make_ui()
        ref = await ui.ref_for(_user(), SERVER_ID)
        assert ref is not None
        confirm = await ui.handle(Callback("servers", "renew", (ref,)), _user())
        exec_cb = _callback_of(confirm, TRANSLATOR.t("servers.renew_confirm_button"))

        await ui.handle(exec_cb, _user())
        # Telegram redelivers the same callback: the pending action is gone.
        again = await ui.handle(exec_cb, _user())
        assert TRANSLATOR.t("servers.action_in_progress") in again.text
        assert management.calls.count("renew_now") == 1

    async def test_insufficient_funds_is_reported_politely(self) -> None:
        ui, management = _make_ui()
        management.renew_now = _renew_insufficient  # type: ignore[method-assign]
        ref = await ui.ref_for(_user(), SERVER_ID)
        assert ref is not None
        confirm = await ui.handle(Callback("servers", "renew", (ref,)), _user())
        exec_cb = _callback_of(confirm, TRANSLATOR.t("servers.renew_confirm_button"))
        screen = await ui.handle(exec_cb, _user())
        assert "کیف پول" in screen.text or "wallet" in screen.text

    async def test_renew_screen_refuses_when_nothing_is_due(self) -> None:
        ui, management = _make_ui(
            FakeManagement(
                view=_view(commercial_status=RenewalStatus.ACTIVE.value, commercial_payable=False)
            )
        )
        ref = await ui.ref_for(_user(), SERVER_ID)
        assert ref is not None
        screen = await ui.handle(Callback("servers", "renew", (ref,)), _user())
        assert TRANSLATOR.t("servers.renew_not_due") in screen.text
        assert "renew_now" not in management.calls

    async def test_auto_renew_toggle_round_trips(self) -> None:
        ui, management = _make_ui()
        ref = await ui.ref_for(_user(), SERVER_ID)
        assert ref is not None
        screen = await ui.handle(Callback("servers", "autorenew", (ref, "off")), _user())
        assert TRANSLATOR.t("servers.auto_renew_off") in screen.text
        assert "set_auto_renew:False" in management.calls

        screen = await ui.handle(Callback("servers", "autorenew", (ref, "on")), _user())
        assert TRANSLATOR.t("servers.auto_renew_on") in screen.text
        assert "set_auto_renew:True" in management.calls

    async def test_a_foreign_customer_cannot_reach_the_commercial_screens(self) -> None:
        """Even a reference this customer minted for a foreign server is refused.

        A second customer can obtain a perfectly valid reference for a server
        id they do not own (references are opaque, not secret); the application
        service must still refuse it, and must not have charged anything.
        """
        ui, management = _make_ui()
        foreign = _user(OTHER_CUSTOMER)
        ref = await ui.ref_for(foreign, SERVER_ID)
        assert ref is not None
        for screen, args in (
            ("renew", (ref,)),
            ("autorenew", (ref, "off")),
            ("view", (ref,)),
        ):
            rendered = await ui.handle(Callback("servers", screen, args), foreign)
            assert TRANSLATOR.t("servers.not_found") in rendered.text
        assert management.calls == []

    async def test_a_reference_minted_for_another_customer_does_not_resolve(self) -> None:
        ui, management = _make_ui()
        ref = await ui.ref_for(_user(), SERVER_ID)
        assert ref is not None
        rendered = await ui.handle(Callback("servers", "renew", (ref,)), _user(OTHER_CUSTOMER))
        assert TRANSLATOR.t("nav.expired") in rendered.text
        assert management.calls == []

    async def test_every_commercial_callback_fits_the_telegram_budget(self) -> None:
        ui, _ = _make_ui()
        ref = await ui.ref_for(_user(), SERVER_ID)
        assert ref is not None
        screens = [
            await ui.handle(Callback("servers", "manage", (ref,)), _user()),
            await ui.handle(Callback("servers", "renew", (ref,)), _user()),
            await ui.handle(Callback("servers", "autorenew", (ref, "off")), _user()),
        ]
        for screen in screens:
            for row in screen.keyboard.inline_keyboard:
                for button in row:
                    data = getattr(button, "callback_data", None)
                    if data:
                        assert len(data.encode()) <= 64, data

    async def test_the_session_store_outage_fails_closed(self) -> None:
        class DeadStore(InMemoryBotSessionStore):
            async def get(self, namespace: str, key: str) -> Any:
                raise SessionStoreUnavailable("session store get failed")

        ui, management = _make_ui(store=DeadStore())
        screen = await ui.handle(Callback("servers", "renew", ("ref-ref-ref",)), _user())
        assert TRANSLATOR.t("servers.err_retry") in screen.text
        assert management.calls == []


async def _renew_insufficient(
    customer_id: UUID,
    server_id: UUID,
    *,
    confirmation_token: str | None,
) -> ServerRenewalView:
    """A drop-in ``renew_now`` that models an under-funded wallet."""
    return ServerRenewalView(
        server_id=server_id,
        reason="insufficient_funds",
        settled=False,
        amount_minor=PRICE,
        currency="EUR",
    )
