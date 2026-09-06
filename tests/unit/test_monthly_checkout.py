"""Monthly checkout safety tests (LEASEWEB-MVP).

Acceptance: the checkout command validates the offer/OS/user, holds the
exact monthly price, and persists the server + order + operation intents
BEFORE any provider call; a repeated callback with the same key replays
the original intent instead of double-charging or double-ordering.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.audit.domain import AuditEvent
from cloud_platform.modules.checkout.service import (
    CheckoutReplayError,
    MonthlyCheckoutService,
    OfferCatalogViewService,
    OfferUnavailableError,
    OsUnavailableError,
    UserNotActiveError,
)
from cloud_platform.modules.compute.domain import (
    BILLING_MODEL_PREPAID_MONTHLY,
    CloudServer,
    ServerLifecycleState,
)
from cloud_platform.modules.offers.domain import SellableOffer
from cloud_platform.modules.operations.domain import Operation, OperationStatus
from cloud_platform.modules.users.domain import User, UserStatus
from cloud_platform.modules.wallet.domain import (
    Hold,
    HoldStatus,
    InsufficientHoldBalanceError,
    Wallet,
)

USER_ID = uuid4()
WALLET_ID = uuid4()
OFFER_ID = uuid4()


def _offer(*, sellable: bool = True, price: int = 1299) -> SellableOffer:
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
        selling_price_minor=price,
        selling_currency="EUR",
        billing_parameters={},
        provider_available=sellable,
        enabled=sellable,
    )


class FakeAuditRepo:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def append(self, event: AuditEvent) -> AuditEvent:
        self.events.append(event)
        return event


class FakeOfferRepo:
    def __init__(self, offer: SellableOffer | None) -> None:
        self._offer = offer

    async def get(self, offer_id: UUID) -> SellableOffer | None:
        return self._offer if offer_id == OFFER_ID else None


class FakeAccountRepo:
    def __init__(self) -> None:
        self.accounts: dict[UUID, UUID] = {}

    async def get_or_create_active(self, user_id: UUID, provider_key: str) -> Any:
        account_id = self.accounts.get(user_id) or uuid4()
        self.accounts[user_id] = account_id
        return type("Account", (), {"id": account_id})()


class FakeWalletRepo:
    def __init__(self, balance: int) -> None:
        self.wallet = Wallet(user_id=USER_ID, id=WALLET_ID, balance=balance, currency="EUR")

    async def get(self, user_id: UUID) -> Wallet | None:
        return self.wallet if user_id == USER_ID else None


class FakeHoldRepo:
    def __init__(self, wallet: FakeWalletRepo) -> None:
        self._wallet = wallet
        self.holds: dict[str, Hold] = {}
        self.by_id: dict[UUID, Hold] = {}

    async def create_hold(
        self, wallet_id: UUID, amount: int, currency: str, idempotency_key: str
    ) -> Hold:
        if wallet_id != WALLET_ID:
            raise ValueError("unknown wallet")
        existing = self.holds.get(idempotency_key)
        if existing is not None:
            return existing
        if self._wallet.wallet.balance < amount:
            raise InsufficientHoldBalanceError(
                f"available balance {self._wallet.wallet.balance} < hold amount {amount}"
            )
        hold = Hold(
            wallet_id=wallet_id,
            amount=amount,
            currency=currency,
            idempotency_key=idempotency_key,
            id=uuid4(),
        )
        self.holds[idempotency_key] = hold
        self.by_id[hold.id] = hold
        return hold

    async def get_by_idempotency(self, wallet_id: UUID, idempotency_key: str) -> Hold | None:
        return self.holds.get(idempotency_key)

    async def release_hold(self, hold_id: UUID) -> Hold | None:
        hold = self.by_id.get(hold_id)
        if hold is None or hold.status is not HoldStatus.CREATED:
            return None
        hold.release()
        return hold

    async def capture_hold(self, hold_id: UUID) -> Hold | None:
        hold = self.by_id.get(hold_id)
        if hold is None or hold.status is not HoldStatus.CREATED:
            return None
        hold.capture()
        return hold


class FakeServerRepo:
    def __init__(self) -> None:
        self.servers: list[CloudServer] = []
        self.by_key: dict[str, CloudServer] = {}

    async def get_by_idempotency_key(self, idempotency_key: str) -> CloudServer | None:
        return self.by_key.get(idempotency_key)

    async def create(self, server: CloudServer, intent: Any) -> CloudServer:
        self.servers.append(server)
        self.by_key[intent.idempotency_key] = server
        server.created_at = datetime.now(UTC)
        return server

    async def save(self, server: CloudServer) -> CloudServer:
        return server


class FakeOrdersRepo:
    def __init__(self) -> None:
        self.by_server: dict[UUID, Any] = {}

    async def get_by_server(self, server_id: UUID) -> Any:
        return self.by_server.get(server_id)

    async def create(
        self,
        *,
        server_id: UUID,
        operation_key: str,
        provider_key: str,
        offer_id: UUID,
        **snapshots: Any,
    ) -> Any:
        # Release hardening: the checkout snapshots every provider-side fact
        # (product id, location, OS, term, cycle, provider cost, selling
        # price) on the order row BEFORE any provider call.
        order = type(
            "Order",
            (),
            {
                "id": uuid4(),
                "server_id": server_id,
                "operation_key": operation_key,
                "provider_key": provider_key,
                "offer_id": offer_id,
                **snapshots,
            },
        )()
        self.by_server[server_id] = order
        return order


class FakeOperationRepo:
    def __init__(self) -> None:
        self.ops: dict[str, Operation] = {}

    async def get_or_create(
        self,
        *,
        operation_key: str,
        operation_type: Any,
        resource_type: str,
        resource_id: UUID,
        provider_key: str,
    ) -> Operation:
        existing = self.ops.get(operation_key)
        if existing is not None:
            return existing
        op = Operation(
            id=uuid4(),
            operation_key=operation_key,
            operation_type=operation_type,
            resource_type=resource_type,
            resource_id=resource_id,
            provider_key=provider_key,
        )
        self.ops[operation_key] = op
        return op


class FakeOrderingProvider:
    key = "leaseweb"

    def __init__(self, *, os_allowed: set[str] | None = None, product_ok: bool = True) -> None:
        self._os_allowed = os_allowed or {"Ubuntu 24.04"}
        self._product_ok = product_ok
        self.place_order_calls = 0

    async def get_product(self, location_id: str, product_id: str) -> Any:
        if not self._product_ok:
            from cloud_platform.providers.errors import ProviderUnavailable

            raise ProviderUnavailable("product endpoint down")
        return type("Detail", (), {})()  # os_name_allowed decides

    def os_name_allowed(self, detail: Any, os_name: str) -> bool:
        return os_name in self._os_allowed

    async def place_order(self, request: Any, idempotency_key: Any) -> Any:
        self.place_order_calls += 1
        raise AssertionError("checkout must not call the provider")

    async def get_order(self, provider_order_id: str) -> Any:
        raise AssertionError("checkout must not call the provider")


def _user(status: UserStatus = UserStatus.ACTIVE) -> User:
    return User(
        id=USER_ID,
        username="customer",
        email="customer@t.me",
        status=status,
    )


def _make_service(
    *,
    offer: SellableOffer | None = None,
    balance: int = 10_000,
    user: User | None = None,
    ordering: FakeOrderingProvider | None = None,
    server_repo: FakeServerRepo | None = None,
    hold_repo: FakeHoldRepo | None = None,
) -> tuple[MonthlyCheckoutService, dict[str, Any]]:
    wallet_repo = FakeWalletRepo(balance)
    if hold_repo is None:
        hold_repo = FakeHoldRepo(wallet_repo)
    server_repo = server_repo or FakeServerRepo()
    orders_repo = FakeOrdersRepo()
    ops_repo = FakeOperationRepo()
    ordering = ordering or FakeOrderingProvider()
    registry = type(
        "Registry",
        (),
        {
            "get": lambda self, key: (
                ordering if key == "leaseweb" else (_ for _ in ()).throw(KeyError(key))
            )
        },
    )()
    service = MonthlyCheckoutService(
        server_repo=server_repo,
        offers_repo=FakeOfferRepo(offer),
        account_repo=FakeAccountRepo(),
        wallet_repo=wallet_repo,
        hold_repo=hold_repo,
        orders_repo=orders_repo,
        operation_repo=ops_repo,
        audit_repo=FakeAuditRepo(),
        provider_registry=registry,
    )
    deps = {
        "wallet": wallet_repo,
        "holds": hold_repo,
        "servers": server_repo,
        "orders": orders_repo,
        "ops": ops_repo,
        "ordering": ordering,
    }
    return service, deps


class TestCheckoutSafety:
    async def test_insufficient_balance_blocks_checkout(self) -> None:
        service, deps = _make_service(offer=_offer(), balance=100)
        with pytest.raises(InsufficientHoldBalanceError):
            await service.create_order(
                user=_user(), offer_id=OFFER_ID, os_name="Ubuntu 24.04", idempotency_key="k1"
            )
        assert deps["servers"].servers == []  # no intent persisted
        assert deps["orders"].by_server == {}

    async def test_success_creates_intents_without_provider_call(self) -> None:
        service, deps = _make_service(offer=_offer())
        result = await service.create_order(
            user=_user(), offer_id=OFFER_ID, os_name="Ubuntu 24.04", idempotency_key="k1"
        )
        assert result.replayed is False
        server = result.server
        assert server.state is ServerLifecycleState.REQUESTED
        assert server.billing_model == BILLING_MODEL_PREPAID_MONTHLY
        assert server.os == "Ubuntu 24.04"
        assert server.user_id == USER_ID
        # Hold for the exact monthly price.
        assert result.hold is not None and result.hold.amount == 1299
        # Order intent with the deterministic operation key.
        assert result.order.operation_key == f"order-create:{server.id}"
        assert deps["orders"].by_server[server.id] is result.order
        # Release hardening: provider-side facts are snapshotted BEFORE any
        # provider call (provider cost and selling price stay separate).
        order_row = deps["orders"].by_server[server.id]
        assert order_row.product_id == "VPS02_1"
        assert order_row.location_id == "AMS-01"
        assert order_row.os_name == "Ubuntu 24.04"
        assert order_row.contract_term == "1_MONTH"
        assert order_row.billing_cycle == "1_MONTH"
        assert order_row.provider_cost_minor == 999
        assert order_row.provider_cost_currency == "EUR"
        assert order_row.selling_price_minor == 1299
        assert order_row.selling_currency == "EUR"
        # Operation ledger row exists.
        op = await deps["ops"].get_or_create(
            operation_key=f"order-create:{server.id}",
            operation_type="order_create",
            resource_type="server_order",
            resource_id=server.id,
            provider_key="leaseweb",
        )
        assert op.status is OperationStatus.PENDING
        # The provider was never called.
        assert deps["ordering"].place_order_calls == 0

    async def test_repeated_callback_replays_original_intent(self) -> None:
        service, deps = _make_service(offer=_offer())
        first = await service.create_order(
            user=_user(), offer_id=OFFER_ID, os_name="Ubuntu 24.04", idempotency_key="dup"
        )
        second = await service.create_order(
            user=_user(), offer_id=OFFER_ID, os_name="Ubuntu 24.04", idempotency_key="dup"
        )
        assert second.replayed is True
        assert second.server.id == first.server.id
        assert second.order.id == first.order.id
        assert second.hold is not None and second.hold.id == first.hold.id
        assert len(deps["servers"].servers) == 1  # exactly one server row
        assert len(deps["holds"].holds) == 1  # exactly one hold
        assert deps["ordering"].place_order_calls == 0

    async def test_replay_with_another_users_key_is_rejected(self) -> None:
        service, _ = _make_service(offer=_offer())
        await service.create_order(
            user=_user(), offer_id=OFFER_ID, os_name="Ubuntu 24.04", idempotency_key="mine"
        )
        other = User(id=uuid4(), username="other", email="other@t.me")
        with pytest.raises(CheckoutReplayError):
            await service.create_order(
                user=other, offer_id=OFFER_ID, os_name="Ubuntu 24.04", idempotency_key="mine"
            )

    async def test_os_not_available_is_rejected_before_hold(self) -> None:
        service, deps = _make_service(
            offer=_offer(), ordering=FakeOrderingProvider(os_allowed=set())
        )
        with pytest.raises(OsUnavailableError):
            await service.create_order(
                user=_user(), offer_id=OFFER_ID, os_name="Debian 12", idempotency_key="k2"
            )
        assert deps["holds"].holds == {}  # no hold was created
        assert deps["servers"].servers == []

    async def test_inactive_user_is_rejected(self) -> None:
        service, deps = _make_service(offer=_offer())
        with pytest.raises(UserNotActiveError):
            await service.create_order(
                user=_user(UserStatus.FROZEN),
                offer_id=OFFER_ID,
                os_name="Ubuntu 24.04",
                idempotency_key="k3",
            )
        assert deps["servers"].servers == []

    async def test_unsellable_offer_is_rejected(self) -> None:
        service, deps = _make_service(offer=_offer(sellable=False))
        with pytest.raises(OfferUnavailableError):
            await service.create_order(
                user=_user(), offer_id=OFFER_ID, os_name="Ubuntu 24.04", idempotency_key="k4"
            )
        assert deps["servers"].servers == []

    async def test_product_api_down_releases_hold(self) -> None:
        service, deps = _make_service(
            offer=_offer(),
            ordering=FakeOrderingProvider(product_ok=False),
        )
        with pytest.raises(OfferUnavailableError):
            await service.create_order(
                user=_user(), offer_id=OFFER_ID, os_name="Ubuntu 24.04", idempotency_key="k5"
            )
        # OS validation happens BEFORE the hold; nothing persisted.
        assert deps["servers"].servers == []
        assert deps["holds"].holds == {}


class TestCheckoutErrorPaths:
    async def test_user_without_id_is_rejected(self) -> None:
        service, _ = _make_service(offer=_offer())
        user = _user()
        user.id = None
        from cloud_platform.modules.checkout.service import CheckoutError

        with pytest.raises(CheckoutError):
            await service.create_order(
                user=user, offer_id=OFFER_ID, os_name="Ubuntu 24.04", idempotency_key="k-1"
            )

    async def test_provider_not_configured_is_rejected(self) -> None:
        from cloud_platform.modules.checkout.service import OfferUnavailableError

        wallet_repo = FakeWalletRepo(10_000)
        hold_repo = FakeHoldRepo(wallet_repo)
        registry = type(
            "Registry",
            (),
            {"get": lambda self, key: (_ for _ in ()).throw(KeyError(key))},
        )()
        service = MonthlyCheckoutService(
            server_repo=FakeServerRepo(),
            offers_repo=FakeOfferRepo(_offer()),
            account_repo=FakeAccountRepo(),
            wallet_repo=wallet_repo,
            hold_repo=hold_repo,
            orders_repo=FakeOrdersRepo(),
            operation_repo=FakeOperationRepo(),
            audit_repo=FakeAuditRepo(),
            provider_registry=registry,
        )
        with pytest.raises(OfferUnavailableError):
            await service.create_order(
                user=_user(), offer_id=OFFER_ID, os_name="Ubuntu 24.04", idempotency_key="k-2"
            )

    async def test_provider_without_ordering_port_is_rejected(self) -> None:
        from cloud_platform.modules.checkout.service import OfferUnavailableError

        class NoOrdering:
            key = "leaseweb"

        wallet_repo = FakeWalletRepo(10_000)
        registry = type("Registry", (), {"get": lambda self, key: NoOrdering()})()
        service = MonthlyCheckoutService(
            server_repo=FakeServerRepo(),
            offers_repo=FakeOfferRepo(_offer()),
            account_repo=FakeAccountRepo(),
            wallet_repo=wallet_repo,
            hold_repo=FakeHoldRepo(wallet_repo),
            orders_repo=FakeOrdersRepo(),
            operation_repo=FakeOperationRepo(),
            audit_repo=FakeAuditRepo(),
            provider_registry=registry,
        )
        with pytest.raises(OfferUnavailableError):
            await service.create_order(
                user=_user(), offer_id=OFFER_ID, os_name="Ubuntu 24.04", idempotency_key="k-3"
            )

    async def test_product_api_down_is_rejected(self) -> None:
        service, _ = _make_service(ordering=FakeOrderingProvider(product_ok=False))
        with pytest.raises(OfferUnavailableError):
            await service.create_order(
                user=_user(), offer_id=OFFER_ID, os_name="Ubuntu 24.04", idempotency_key="k-4"
            )

    async def test_no_wallet_is_rejected(self) -> None:
        from cloud_platform.modules.checkout.service import NoWalletError

        service, _ = _make_service(offer=_offer())

        class NoWalletRepo:
            async def get(self, user_id: UUID) -> None:
                return None

        service._wallets = NoWalletRepo()  # type: ignore[assignment]
        with pytest.raises(NoWalletError):
            await service.create_order(
                user=_user(), offer_id=OFFER_ID, os_name="Ubuntu 24.04", idempotency_key="k-5"
            )

    async def test_order_create_failure_compensates(self) -> None:
        service, deps = _make_service(offer=_offer())

        class BrokenOrdersRepo(FakeOrdersRepo):
            async def create(self, **kwargs: Any) -> Any:
                raise RuntimeError("db constraint")

        service._orders = BrokenOrdersRepo()  # type: ignore[assignment]
        with pytest.raises(RuntimeError):
            await service.create_order(
                user=_user(), offer_id=OFFER_ID, os_name="Ubuntu 24.04", idempotency_key="k-6"
            )
        # Compensation: the created server is ERROR and the hold is released.
        assert deps["servers"].servers[-1].state is ServerLifecycleState.ERROR
        assert len(deps["holds"].holds) == 1
        assert next(iter(deps["holds"].holds.values())).status is HoldStatus.RELEASED

    async def test_operation_create_failure_compensates(self) -> None:
        service, deps = _make_service(offer=_offer())

        class BrokenOpsRepo(FakeOperationRepo):
            async def get_or_create(self, **kwargs: Any) -> Any:
                raise RuntimeError("db constraint")

        service._ops = BrokenOpsRepo()  # type: ignore[assignment]
        with pytest.raises(RuntimeError):
            await service.create_order(
                user=_user(), offer_id=OFFER_ID, os_name="Ubuntu 24.04", idempotency_key="k-7"
            )
        assert deps["servers"].servers[-1].state is ServerLifecycleState.ERROR
        assert next(iter(deps["holds"].holds.values())).status is HoldStatus.RELEASED

    async def test_concurrent_duplicate_replays_original(self) -> None:
        service, deps = _make_service(offer=_offer())

        class RacingServerRepo(FakeServerRepo):
            async def create(self, server: CloudServer, intent: Any) -> CloudServer:
                from cloud_platform.modules.compute.domain import ServerCreateError

                raise ServerCreateError("duplicate idempotency key")

        racing = RacingServerRepo()
        racing.by_key["k-8"] = deps["servers"].servers[0] if deps["servers"].servers else None
        # Seed the original intent exactly like the first checkout would.
        first, _ = _make_service(offer=_offer())
        result = await first.create_order(
            user=_user(), offer_id=OFFER_ID, os_name="Ubuntu 24.04", idempotency_key="k-8"
        )
        assert result.replayed is False
        racing.by_key["k-8"] = result.server
        service._servers = racing  # type: ignore[assignment]
        service._orders.by_server[result.server.id] = result.order  # type: ignore[attr-defined]
        replay = await service.create_order(
            user=_user(), offer_id=OFFER_ID, os_name="Ubuntu 24.04", idempotency_key="k-8"
        )
        assert replay.replayed is True
        assert replay.server.id == result.server.id

    async def test_replay_without_wallet_returns_hold_none(self) -> None:
        service, _ = _make_service(offer=_offer())
        first = await service.create_order(
            user=_user(), offer_id=OFFER_ID, os_name="Ubuntu 24.04", idempotency_key="k-9"
        )
        assert first.replayed is False

        class NoWalletRepo:
            async def get(self, user_id: UUID) -> None:
                return None

        service._wallets = NoWalletRepo()  # type: ignore[assignment]
        replay = await service.create_order(
            user=_user(), offer_id=OFFER_ID, os_name="Ubuntu 24.04", idempotency_key="k-9"
        )
        assert replay.replayed is True
        assert replay.hold is None

    async def test_release_hold_without_id_is_noop(self) -> None:
        service, _ = _make_service(offer=_offer())
        hold = Hold(
            wallet_id=WALLET_ID,
            amount=1299,
            currency="EUR",
            idempotency_key="orphan",
            id=None,
        )
        await service._release_hold(hold)  # must not raise


class FakeListOfferRepo(FakeOfferRepo):
    def __init__(self, offers: list[SellableOffer]) -> None:
        super().__init__(offers[0] if offers else None)
        self._offers = offers

    async def list_sellable(self, provider_key: str) -> list[SellableOffer]:
        return [o for o in self._offers if o.sellable]


def _detail() -> Any:
    from cloud_platform.providers.leaseweb.ordering import (
        LeasewebProduct,
        LeasewebProductDetail,
        LeasewebProductOption,
    )

    product = LeasewebProduct(
        id="VPS02_1",
        name="VPS S",
        location="AMS-01",
        vcpu=2,
        ram_gb=4,
        disk_gb=100,
        traffic="10 TB",
        currency="EUR",
        monthly_price_minor=999,
        provider_price_minor=999,
    )
    return LeasewebProductDetail(
        product=product,
        os_options=(
            LeasewebProductOption(
                name="Ubuntu 24.04", price_minor=0, currency="EUR", selected=False
            ),
            LeasewebProductOption(name="Debian 12", price_minor=0, currency="EUR", selected=False),
            LeasewebProductOption(
                name="Windows 2022", price_minor=1500, currency="EUR", selected=False
            ),
        ),
        control_panels=(),
        disk_upgrades=(),
        slas=(),
        available_locations=("AMS-01",),
        contract_terms={"1_MONTH": 999},
        billing_cycles={"1_MONTH": 999},
    )


def _view_service(
    *,
    offers: list[SellableOffer] | None = None,
    detail: Any | None = None,
    no_ordering: bool = False,
    missing_provider: bool = False,
) -> OfferCatalogViewService:
    from cloud_platform.modules.checkout.service import OfferCatalogViewService

    offers = offers if offers is not None else [_offer()]

    class FakeOrdering:
        key = "leaseweb"

        async def get_product(self, location_id: str, product_id: str) -> Any:
            return detail if detail is not None else _detail()

        async def place_order(self, request: Any, idempotency_key: Any) -> Any:
            raise AssertionError("view service must not place orders")

        async def get_order(self, provider_order_id: str) -> Any:
            raise AssertionError("view service must not poll orders")

    class NoOrdering:
        key = "leaseweb"

    def _get(self, key: str) -> Any:
        if missing_provider:
            raise KeyError(key)
        return NoOrdering() if no_ordering else FakeOrdering()

    registry = type("Registry", (), {"get": _get})()
    wallet_repo = FakeWalletRepo(10_000)
    return OfferCatalogViewService(
        offers_repo=FakeListOfferRepo(offers),
        provider_registry=registry,
        wallet_repo=wallet_repo,
        signing_key="test-signing-key",
    )


class TestOfferCatalogViews:
    def test_empty_signing_key_rejected(self) -> None:
        from cloud_platform.modules.checkout.service import OfferCatalogViewService

        with pytest.raises(ValueError):
            OfferCatalogViewService(
                offers_repo=FakeListOfferRepo([_offer()]),
                provider_registry=type("R", (), {"get": lambda self, k: None})(),
                wallet_repo=FakeWalletRepo(100),
                signing_key="",
            )

    async def test_os_options_provider_missing(self) -> None:
        service = _view_service(missing_provider=True)
        with pytest.raises(OfferUnavailableError):
            await service.os_options(_offer())

    async def test_os_options_no_ordering_port(self) -> None:
        service = _view_service(no_ordering=True)
        with pytest.raises(OfferUnavailableError):
            await service.os_options(_offer())

    async def test_os_options_free_only_by_default(self) -> None:
        service = _view_service()
        options = await service.os_options(_offer())
        assert [o.name for o in options] == ["Ubuntu 24.04", "Debian 12"]
        assert options[0].select_callback  # signed callback present
        assert options[1].index == 1

    async def test_os_options_all_when_flag_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import cloud_platform.modules.checkout.service as checkout_mod

        settings = type("S", (), {"leaseweb_order_os_only_free": False})()
        monkeypatch.setattr(checkout_mod, "get_settings", lambda: settings)
        service = _view_service()
        options = await service.os_options(_offer())
        assert [o.name for o in options] == [
            "Ubuntu 24.04",
            "Debian 12",
            "Windows 2022",
        ]

    async def test_os_by_index_out_of_range(self) -> None:
        service = _view_service()
        with pytest.raises(OsUnavailableError):
            await service.os_by_index(_offer(), 7)

    async def test_confirmation_unsellable_offer(self) -> None:
        service = _view_service(offers=[_offer(sellable=False)])
        with pytest.raises(OfferUnavailableError):
            await service.confirmation(user_id=USER_ID, offer_id=OFFER_ID, os_index=0)

    async def test_confirmation_shows_exact_price_and_sufficiency(self) -> None:
        service = _view_service(offers=[_offer(price=1299)])
        view = await service.confirmation(user_id=USER_ID, offer_id=OFFER_ID, os_index=0)
        assert view.os_name == "Ubuntu 24.04"
        assert view.offer.monthly_price_minor == 1299
        assert view.balance_minor == 10_000
        assert view.sufficient is True
        assert view.confirm_callback and view.back_callback and view.cancel_callback

        poor = _view_service(offers=[_offer(price=1299)])
        poor._wallets = FakeWalletRepo(500)  # type: ignore[assignment]
        low = await poor.confirmation(user_id=USER_ID, offer_id=OFFER_ID, os_index=0)
        assert low.sufficient is False

    async def test_os_screen_unsellable(self) -> None:
        service = _view_service(offers=[_offer(sellable=False)])
        with pytest.raises(OfferUnavailableError):
            await service.os_screen(offer_id=OFFER_ID)

    async def test_os_screen_no_free_options(self) -> None:
        from cloud_platform.providers.leaseweb.ordering import (
            LeasewebProduct,
            LeasewebProductDetail,
            LeasewebProductOption,
        )

        product = LeasewebProduct(
            id="VPS02_1",
            name="VPS S",
            location="AMS-01",
            vcpu=2,
            ram_gb=4,
            disk_gb=100,
            traffic="10 TB",
            currency="EUR",
            monthly_price_minor=999,
            provider_price_minor=999,
        )
        paid_only = LeasewebProductDetail(
            product=product,
            os_options=(
                LeasewebProductOption(
                    name="Windows 2022", price_minor=1500, currency="EUR", selected=False
                ),
            ),
            control_panels=(),
            disk_upgrades=(),
            slas=(),
            available_locations=("AMS-01",),
            contract_terms={"1_MONTH": 999},
            billing_cycles={"1_MONTH": 999},
        )
        service = _view_service(detail=paid_only)
        with pytest.raises(OsUnavailableError):
            await service.os_screen(offer_id=OFFER_ID)

    async def test_os_screen_returns_data(self) -> None:
        service = _view_service()
        view, options, back, cancel = await service.os_screen(offer_id=OFFER_ID)
        assert view.product_id == "VPS02_1"
        assert len(options) == 2
        assert back and cancel

    async def test_plans_screen_no_offers(self) -> None:
        service = _view_service(offers=[])
        with pytest.raises(OfferUnavailableError):
            await service.plans_screen("AMS-01")

    async def test_plans_screen_sorted_by_name(self) -> None:
        # _offer() has a fixed OFFER_ID; build a second sellable offer.
        other = _offer()
        object.__setattr__(other, "id", uuid4())
        object.__setattr__(other, "name", "VPS L")
        object.__setattr__(other, "vcpu", 4)
        service = _view_service(offers=[other, _offer()])
        views, back, cancel = await service.plans_screen("AMS-01")
        assert [v.name for v in views] == ["VPS L", "VPS S"]
        assert back and cancel

    async def test_plan_callback_returns_signed(self) -> None:
        service = _view_service()
        callback = service.plan_callback(OFFER_ID)
        assert "offers:plans" in callback
