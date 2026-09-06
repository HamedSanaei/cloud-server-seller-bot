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
        self, *, server_id: UUID, operation_key: str, provider_key: str, offer_id: UUID
    ) -> Any:
        order = type(
            "Order",
            (),
            {
                "id": uuid4(),
                "server_id": server_id,
                "operation_key": operation_key,
                "provider_key": provider_key,
                "offer_id": offer_id,
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
