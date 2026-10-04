"""Tests for the create-server application command (M07-001).

Acceptance: validates user/offer/balance and persists intent atomically.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.catalog.domain import (
    OfferNotFoundError,
    OfferRef,
    OfferState,
)
from cloud_platform.modules.compute.domain import (
    CloudServer,
    MaintenanceBlock,
    MaintenanceScope,
    QuotaPolicy,
    ServerCreateError,
    ServerCreateIntent,
    ServerLifecycleState,
)
from cloud_platform.modules.compute.service import (
    CreateServerCommandError,
    CreateServerService,
    MaintenanceBlockedError,
    MaintenanceSwitchService,
    NoWalletError,
    OfferDisabledError,
    ProviderAccountsFullError,
    ProviderInventoryUnavailableError,
    QuotaExceededError,
    UserNotActiveError,
)
from cloud_platform.modules.pricing.domain import (
    MarginRule,
    OfferCost,
    SellingPrice,
    ServerPriceSnapshot,
)
from cloud_platform.modules.provider_accounts.domain import (
    NoProviderAccountError,
    ProviderAccount,
)
from cloud_platform.modules.provider_routes.domain import ProviderRoute, RouteState
from cloud_platform.modules.provider_routes.service import ProviderRouteSelector
from cloud_platform.modules.users.domain import Role, User, UserStatus
from cloud_platform.modules.users.identity import IdentityRequiredError
from cloud_platform.modules.wallet.domain import (
    Hold,
    InsufficientHoldBalanceError,
    Wallet,
)
from cloud_platform.providers.base import AccountServerUsage
from cloud_platform.providers.errors import ProviderUnavailable
from cloud_platform.providers.routing import CredentialAccountState

USER_ID = uuid4()
OTHER_USER_ID = uuid4()
OFFER_ID = uuid4()
ACCOUNT_ID = uuid4()
WALLET_ID = uuid4()
KEY = "cmd-key-1"
NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
REF = OfferRef(provider_key="hetzner", plan_id="cx22", location_id="fsn1")


def _user(status: UserStatus = UserStatus.ACTIVE) -> User:
    return User(
        id=USER_ID,
        username="alice",
        email="a@example.com",
        role=Role.USER,
        status=status,
        telegram_user_id=12345,
        phone_number="+989123456789",
        phone_verified_at=NOW,
        national_id="1234567891",
    )


def _offer(enabled: bool = True, price: int = 100) -> OfferState:
    return OfferState(
        id=OFFER_ID,
        ref=REF,
        name="CX22",
        enabled=enabled,
        price_per_quantum=price,
        currency="EUR",
    )


def _account() -> ProviderAccount:
    return ProviderAccount(id=ACCOUNT_ID, user_id=USER_ID, provider_key="hetzner")


def _wallet() -> Wallet:
    return Wallet(user_id=USER_ID, id=WALLET_ID, balance=1000, currency="EUR")


def _price(selling: int = 107) -> SellingPrice:
    return SellingPrice(
        offer=OfferCost("hetzner", "cx22", "fsn1", 100, "EUR"),
        selling_minor=selling,
        rule=MarginRule("*", "*", "*", Decimal("1.07")),
        book_name="retail-eur",
        version=1,
        priced_at=NOW,
    )


def _hold(amount: int = 107) -> Hold:
    return Hold(
        wallet_id=WALLET_ID,
        amount=amount,
        currency="EUR",
        idempotency_key=f"server-create:{KEY}",
        id=uuid4(),
    )


def _snapshot(server_id: UUID, price: SellingPrice) -> ServerPriceSnapshot:
    return ServerPriceSnapshot(
        server_id=server_id,
        offer=price.offer,
        selling_minor=price.selling_minor,
        book_name=price.book_name,
        book_version=price.version,
        rule=price.rule,
        priced_at=price.priced_at,
        id=uuid4(),
    )


def _existing(user_id: UUID = USER_ID) -> CloudServer:
    return CloudServer(
        id=uuid4(),
        user_id=user_id,
        provider_key="hetzner",
        provider_account_id=ACCOUNT_ID,
        state=ServerLifecycleState.PROVISIONING,
    )


class _Deps:
    def __init__(self) -> None:
        self.servers = AsyncMock()
        self.servers.get_by_idempotency_key = AsyncMock(return_value=None)
        self.servers.create = AsyncMock(side_effect=lambda s, i: s)
        self.servers.save = AsyncMock(side_effect=lambda s: s)
        self.servers.count_active = AsyncMock(return_value=0)
        self.servers.count_total = AsyncMock(return_value=0)
        self.accounts = AsyncMock()
        self.accounts.get_active = AsyncMock(return_value=_account())
        self.catalog = AsyncMock()
        self.catalog.get_offer = AsyncMock(return_value=_offer())
        self.books = MagicMock()
        self.books.sell_price = AsyncMock(return_value=_price())
        self.snaps = MagicMock()
        self.snaps.create_snapshot = AsyncMock(
            side_effect=lambda **kw: _snapshot(kw["server_id"], kw["price"])
        )
        self.snaps.get_snapshot = AsyncMock(return_value=None)
        self.wallets = AsyncMock()
        self.wallets.get = AsyncMock(return_value=_wallet())
        self.holds = AsyncMock()
        self.holds.create_hold = AsyncMock(return_value=_hold())
        self.holds.get_by_idempotency = AsyncMock(return_value=None)
        self.holds.release_hold = AsyncMock(return_value=None)
        self.audit = AsyncMock()
        self.audit.append = AsyncMock(side_effect=lambda e: e)

    def service(
        self,
        quota: QuotaPolicy | None = None,
        maintenance: MaintenanceSwitchService | None = None,
        cost_breaker=None,
        fulfillment_routes=None,
    ) -> CreateServerService:
        return CreateServerService(
            server_repo=self.servers,  # type: ignore[arg-type]
            account_repo=self.accounts,  # type: ignore[arg-type]
            catalog_repo=self.catalog,  # type: ignore[arg-type]
            price_book_service=self.books,  # type: ignore[arg-type]
            snapshot_service=self.snaps,  # type: ignore[arg-type]
            wallet_repo=self.wallets,  # type: ignore[arg-type]
            hold_repo=self.holds,  # type: ignore[arg-type]
            audit_repo=self.audit,  # type: ignore[arg-type]
            book_name="retail-eur",
            quota=quota,
            maintenance=maintenance,
            cost_breaker=cost_breaker,
            fulfillment_routes=fulfillment_routes,
        )

    def run(self, **overrides: object):
        kwargs: dict[str, object] = {
            "user": _user(),
            "offer_ref": REF,
            "idempotency_key": KEY,
            "at": NOW,
        }
        kwargs.update(overrides)
        quota = kwargs.pop("quota", None)
        maintenance = kwargs.pop("maintenance", None)
        cost_breaker = kwargs.pop("cost_breaker", None)
        fulfillment_routes = kwargs.pop("fulfillment_routes", None)
        return (
            self.service(
                quota=quota,
                maintenance=maintenance,
                cost_breaker=cost_breaker,
                fulfillment_routes=fulfillment_routes,
            ).create_server(**kwargs)  # type: ignore[arg-type]
        )


def _audit_events(deps: _Deps) -> list:
    return [c.args[0] for c in deps.audit.append.call_args_list]


class TestHappyPath:
    async def test_validates_and_persists_intent(self) -> None:
        deps = _Deps()
        result = await deps.run()

        assert result.replayed is False
        assert result.server.state is ServerLifecycleState.REQUESTED
        assert result.server.user_id == USER_ID
        assert result.server.provider_key == "hetzner"
        assert result.server.provider_account_id == ACCOUNT_ID
        assert result.hold is not None
        assert result.hold.amount == 107
        assert result.hold.currency == "EUR"
        assert result.hold.idempotency_key == f"server-create:{KEY}"
        assert result.snapshot is not None
        assert result.snapshot.selling_minor == 107
        assert result.snapshot.book_version == 1

        # hold reserved from the user's wallet for the derived selling price
        deps.holds.create_hold.assert_awaited_once_with(
            WALLET_ID, 107, "EUR", f"server-create:{KEY}"
        )
        # server row pinned to the exact offer, with the command key
        created_server, intent = deps.servers.create.call_args[0]
        assert isinstance(intent, ServerCreateIntent)
        assert intent.catalog_id == OFFER_ID
        assert intent.cost_minor == 100
        assert intent.currency == "EUR"
        assert intent.idempotency_key == KEY
        assert created_server.state is ServerLifecycleState.REQUESTED
        # price came from the versioned book
        deps.books.sell_price.assert_awaited_once_with(
            book_name="retail-eur",
            offer=OfferCost("hetzner", "cx22", "fsn1", 100, "EUR"),
            at=NOW,
        )
        # audited with the user actor
        event = _audit_events(deps)[0]
        assert event.action == "server.create_requested"
        assert event.actor_type.value == "user"
        assert event.actor_id == USER_ID
        assert event.resource_type == "server"
        assert event.resource_id == str(result.server.id)
        assert event.metadata["offer"] == "hetzner/cx22/fsn1"
        assert event.metadata["selling_minor"] == "107"


class TestReplay:
    async def test_same_key_returns_original_without_side_effects(self) -> None:
        deps = _Deps()
        existing = _existing()
        deps.servers.get_by_idempotency_key = AsyncMock(return_value=existing)
        replay_hold = _hold()
        deps.holds.get_by_idempotency = AsyncMock(return_value=replay_hold)
        snap = _snapshot(existing.id, _price())
        deps.snaps.get_snapshot = AsyncMock(return_value=snap)

        result = await deps.run()

        assert result.replayed is True
        assert result.server is existing
        assert result.hold is replay_hold
        assert result.snapshot is snap
        deps.holds.create_hold.assert_not_awaited()
        deps.servers.create.assert_not_awaited()
        deps.audit.append.assert_not_awaited()

    async def test_key_owned_by_another_user_is_rejected(self) -> None:
        deps = _Deps()
        deps.servers.get_by_idempotency_key = AsyncMock(return_value=_existing(OTHER_USER_ID))

        with pytest.raises(CreateServerCommandError, match="another user"):
            await deps.run()
        deps.holds.create_hold.assert_not_awaited()
        deps.servers.create.assert_not_awaited()

    async def test_concurrent_duplicate_resolves_to_original(self) -> None:
        deps = _Deps()
        original = _existing()
        # replay check sees nothing yet, but the insert collides
        deps.servers.get_by_idempotency_key = AsyncMock(side_effect=[None, original])
        deps.servers.create = AsyncMock(side_effect=ServerCreateError("uq"))
        dup_hold = _hold()
        deps.holds.get_by_idempotency = AsyncMock(return_value=dup_hold)
        snap = _snapshot(original.id, _price())
        deps.snaps.get_snapshot = AsyncMock(return_value=snap)

        result = await deps.run()

        assert result.replayed is True
        assert result.server is original
        assert result.hold is dup_hold
        deps.audit.append.assert_not_awaited()


class TestValidation:
    async def test_frozen_user_rejected_before_any_reads(self) -> None:
        deps = _Deps()
        with pytest.raises(UserNotActiveError, match="frozen"):
            await deps.run(user=_user(UserStatus.FROZEN))
        deps.catalog.get_offer.assert_not_awaited()
        deps.holds.create_hold.assert_not_awaited()

    async def test_banned_user_rejected(self) -> None:
        deps = _Deps()
        with pytest.raises(UserNotActiveError, match="banned"):
            await deps.run(user=_user(UserStatus.BANNED))

    async def test_user_without_id_rejected(self) -> None:
        deps = _Deps()
        user = _user()
        user.id = None
        with pytest.raises(CreateServerCommandError, match="user id"):
            await deps.run(user=user)

    async def test_unknown_offer(self) -> None:
        deps = _Deps()
        deps.catalog.get_offer = AsyncMock(return_value=None)
        with pytest.raises(OfferNotFoundError, match="hetzner/cx22/fsn1"):
            await deps.run()
        deps.holds.create_hold.assert_not_awaited()

    async def test_disabled_offer(self) -> None:
        deps = _Deps()
        deps.catalog.get_offer = AsyncMock(return_value=_offer(enabled=False))
        with pytest.raises(OfferDisabledError, match="not enabled"):
            await deps.run()
        deps.holds.create_hold.assert_not_awaited()

    async def test_no_active_provider_account(self) -> None:
        deps = _Deps()
        deps.accounts.get_active = AsyncMock(return_value=None)
        with pytest.raises(NoProviderAccountError, match="no active hetzner account"):
            await deps.run()
        deps.holds.create_hold.assert_not_awaited()

    async def test_no_wallet(self) -> None:
        deps = _Deps()
        deps.wallets.get = AsyncMock(return_value=None)
        with pytest.raises(NoWalletError, match="no wallet"):
            await deps.run()
        deps.holds.create_hold.assert_not_awaited()

    async def test_insufficient_balance(self) -> None:
        deps = _Deps()
        deps.holds.create_hold = AsyncMock(
            side_effect=InsufficientHoldBalanceError("balance 1 < required 107")
        )
        with pytest.raises(InsufficientHoldBalanceError):
            await deps.run()
        deps.servers.create.assert_not_awaited()
        deps.audit.append.assert_not_awaited()


class TestCompensation:
    async def test_constraint_failure_other_than_duplicate_releases_hold(self) -> None:
        deps = _Deps()
        deps.servers.create = AsyncMock(side_effect=ServerCreateError("fk violation"))
        deps.servers.get_by_idempotency_key = AsyncMock(return_value=None)

        with pytest.raises(ServerCreateError):
            await deps.run()
        deps.holds.release_hold.assert_awaited_once()
        deps.audit.append.assert_not_awaited()

    async def test_snapshot_failure_marks_intent_error_and_releases_hold(self) -> None:
        deps = _Deps()
        deps.snaps.create_snapshot = AsyncMock(side_effect=RuntimeError("db down"))

        with pytest.raises(RuntimeError, match="db down"):
            await deps.run()

        # intent moved to ERROR and the hold released
        saved = deps.servers.save.call_args[0][0]
        assert saved.state is ServerLifecycleState.ERROR
        deps.holds.release_hold.assert_awaited_once()
        deps.audit.append.assert_not_awaited()


class TestQuota:
    async def test_concurrent_quota_exceeded(self) -> None:
        deps = _Deps()
        deps.servers.count_active = AsyncMock(return_value=10)  # default max 10
        with pytest.raises(QuotaExceededError, match="concurrent limit"):
            await deps.run()
        deps.holds.create_hold.assert_not_awaited()
        deps.servers.create.assert_not_awaited()

    async def test_lifetime_quota_exceeded(self) -> None:
        deps = _Deps()
        deps.servers.count_active = AsyncMock(return_value=0)
        deps.servers.count_total = AsyncMock(return_value=50)  # default max 50
        with pytest.raises(QuotaExceededError, match="lifetime limit"):
            await deps.run()
        deps.holds.create_hold.assert_not_awaited()

    async def test_concurrent_quota_checked_first(self) -> None:
        deps = _Deps()
        deps.servers.count_active = AsyncMock(return_value=10)
        deps.servers.count_total = AsyncMock(return_value=50)
        with pytest.raises(QuotaExceededError, match="concurrent limit"):
            await deps.run()

    async def test_just_under_quota_succeeds(self) -> None:
        deps = _Deps()
        deps.servers.count_active = AsyncMock(return_value=9)
        deps.servers.count_total = AsyncMock(return_value=49)

        result = await deps.run()

        assert result.replayed is False
        deps.servers.create.assert_awaited_once()
        deps.holds.create_hold.assert_awaited_once()

    async def test_custom_quota(self) -> None:
        deps = _Deps()
        deps.servers.count_active = AsyncMock(return_value=1)
        with pytest.raises(QuotaExceededError, match="concurrent limit 1"):
            await deps.run(quota=QuotaPolicy(max_active=1, max_total=100))
        deps.servers.count_active = AsyncMock(return_value=0)
        result = await deps.run(quota=QuotaPolicy(max_active=1, max_total=100))
        assert result.replayed is False

    async def test_zero_quota_blocks_everything(self) -> None:
        deps = _Deps()
        deps.servers.count_active = AsyncMock(return_value=0)
        with pytest.raises(QuotaExceededError, match="concurrent limit 0"):
            await deps.run(quota=QuotaPolicy(max_active=0, max_total=0))

    async def test_replay_is_not_blocked_by_quota(self) -> None:
        deps = _Deps()
        existing = _existing()
        deps.servers.get_by_idempotency_key = AsyncMock(return_value=existing)
        deps.servers.count_active = AsyncMock(return_value=10)  # would exceed

        result = await deps.run()

        assert result.replayed is True
        assert result.server.id == existing.id
        deps.servers.count_active.assert_not_awaited()

    async def test_negative_quota_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_active"):
            QuotaPolicy(max_active=-1)
        with pytest.raises(ValueError, match="max_total"):
            QuotaPolicy(max_total=-1)


class _MiniSwitchRepo:
    """Just enough of MaintenanceSwitchRepository for the create command."""

    def __init__(self, blocked: list[MaintenanceScope]) -> None:
        self._blocked = blocked

    async def list_blocks(self) -> list[MaintenanceBlock]:
        return [
            MaintenanceBlock(scope=s, reason="x", created_by=None, created_at=NOW)
            for s in self._blocked
        ]

    async def save_block(self, block: MaintenanceBlock) -> MaintenanceBlock:
        raise AssertionError("create command must not write switches")

    async def remove_block(self, scope: MaintenanceScope) -> bool:
        raise AssertionError("create command must not write switches")


def _maintenance(*blocked: MaintenanceScope) -> MaintenanceSwitchService:
    return MaintenanceSwitchService(_MiniSwitchRepo(list(blocked)), AsyncMock())  # type: ignore[arg-type]


class TestMaintenanceBlocked:
    async def test_provider_block_stops_new_orders_before_account_or_hold(self) -> None:
        deps = _Deps()
        maintenance = _maintenance(MaintenanceScope(provider_key="hetzner"))

        with pytest.raises(MaintenanceBlockedError, match="provider hetzner"):
            await deps.run(maintenance=maintenance)

        deps.accounts.get_active.assert_not_awaited()
        deps.holds.create_hold.assert_not_awaited()
        deps.servers.create.assert_not_awaited()

    async def test_location_block_only_blocks_that_location(self) -> None:
        deps = _Deps()
        maintenance = _maintenance(
            MaintenanceScope(provider_key="hetzner", location_id=REF.location_id)
        )

        with pytest.raises(MaintenanceBlockedError, match="location fsn1"):
            await deps.run(maintenance=maintenance)

        # A different location of the same provider still orders fine.
        other_ref = OfferRef(provider_key="hetzner", plan_id="cx22", location_id="nbg1")
        deps.catalog.get_offer = AsyncMock(side_effect=lambda ref: _offer())
        result = await deps.run(maintenance=maintenance, offer_ref=other_ref)
        assert result.replayed is False

    async def test_unrelated_provider_block_does_not_interfere(self) -> None:
        deps = _Deps()
        maintenance = _maintenance(MaintenanceScope(provider_key="ovh"))
        result = await deps.run(maintenance=maintenance)
        assert result.replayed is False

    async def test_no_maintenance_service_means_no_check(self) -> None:
        deps = _Deps()
        result = await deps.run()  # maintenance defaults to None
        assert result.replayed is False


def _capacity_routes(
    routes: list[ProviderRoute],
    usage: dict[str, AccountServerUsage | Exception],
) -> tuple[ProviderRouteSelector, AsyncMock, AsyncMock]:
    repository = AsyncMock()
    repository.list_for_location.return_value = routes
    reader = MagicMock()
    reader.accepts_new_orders.return_value = True

    async def server_usage(account_id: str) -> AccountServerUsage:
        outcome = usage[account_id]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    reader.server_usage = AsyncMock(side_effect=server_usage)
    selector = ProviderRouteSelector(repository=repository, usage_readers={"hetzner": reader})
    return selector, reader, repository


def _route(
    account_id: str,
    priority: int,
    *,
    state: RouteState = RouteState.ELIGIBLE_AVAILABLE,
    account_state: CredentialAccountState = CredentialAccountState.ACTIVE,
) -> ProviderRoute:
    # Legacy catalog intents select a proven location, then their worker
    # independently validates the original catalog plan before sending POST.
    return ProviderRoute(
        provider_key="hetzner",
        credential_account_id=account_id,
        location_id=REF.location_id,
        state=state,
        priority=priority,
        account_state=account_state,
    )


class TestCapacityAwareFulfillment:
    async def test_full_first_account_selects_second_before_hold_or_intent(self) -> None:
        deps = _Deps()
        selector, reader, _ = _capacity_routes(
            [_route("hz-b", 20), _route("hz-a", 10)],
            {
                "hz-a": AccountServerUsage("hz-a", 5, 5),
                "hz-b": AccountServerUsage("hz-b", 2, 5),
            },
        )
        hold = _hold()

        async def reserve(*args) -> Hold:
            assert [call.args[0] for call in reader.server_usage.await_args_list] == [
                "hz-a",
                "hz-b",
            ]
            deps.servers.create.assert_not_awaited()
            deps.snaps.create_snapshot.assert_not_awaited()
            return hold

        deps.holds.create_hold.side_effect = reserve
        result = await deps.run(fulfillment_routes=selector)

        assert result.server.credential_account_id == "hz-b"
        assert result.server.provider_account_id == ACCOUNT_ID
        assert result.hold is hold
        deps.holds.create_hold.assert_awaited_once_with(
            WALLET_ID, 107, "EUR", f"server-create:{KEY}"
        )
        created, intent = deps.servers.create.await_args.args
        assert created.credential_account_id == "hz-b"
        assert intent.catalog_id == OFFER_ID
        assert intent.cost_minor == 100
        assert result.snapshot is not None
        assert result.snapshot.offer == _price().offer
        assert result.snapshot.selling_minor == 107

    async def test_all_full_creates_no_hold_intent_or_snapshot(self) -> None:
        deps = _Deps()
        selector, reader, _ = _capacity_routes(
            [_route("hz-a", 10), _route("hz-b", 20)],
            {
                "hz-a": AccountServerUsage("hz-a", 5, 5),
                "hz-b": AccountServerUsage("hz-b", 6, 5),
            },
        )

        with pytest.raises(ProviderAccountsFullError) as caught:
            await deps.run(fulfillment_routes=selector)

        assert caught.value.__suppress_context__
        assert [call.args[0] for call in reader.server_usage.await_args_list] == [
            "hz-a",
            "hz-b",
        ]
        deps.wallets.get.assert_not_awaited()
        deps.holds.create_hold.assert_not_awaited()
        deps.holds.release_hold.assert_not_awaited()
        deps.servers.create.assert_not_awaited()
        deps.snaps.create_snapshot.assert_not_awaited()
        deps.audit.append.assert_not_awaited()

    @pytest.mark.parametrize(
        "unproved_usage",
        [
            ProviderUnavailable("incomplete inventory containing private-provider-context"),
            AccountServerUsage("wrong-account", 0, 5),
        ],
    )
    async def test_unreadable_capacity_is_not_full_and_touches_no_funds(
        self, unproved_usage: AccountServerUsage | Exception
    ) -> None:
        deps = _Deps()
        selector, _, _ = _capacity_routes(
            [_route("hz-a", 10), _route("hz-b", 20)],
            {
                "hz-a": AccountServerUsage("hz-a", 5, 5),
                "hz-b": unproved_usage,
            },
        )

        with pytest.raises(ProviderInventoryUnavailableError) as caught:
            await deps.run(fulfillment_routes=selector)

        assert caught.value.__suppress_context__
        assert "private-provider-context" not in str(caught.value)
        assert "hz-a" not in str(caught.value)
        assert "hz-b" not in str(caught.value)
        deps.wallets.get.assert_not_awaited()
        deps.holds.create_hold.assert_not_awaited()
        deps.holds.release_hold.assert_not_awaited()
        deps.servers.create.assert_not_awaited()
        deps.snaps.create_snapshot.assert_not_awaited()

    @pytest.mark.parametrize(
        "routes",
        [
            [],
            [_route("hz-a", 10, account_state=CredentialAccountState.DRAINING)],
            [_route("hz-a", 10, state=RouteState.TRANSIENT_UNKNOWN)],
        ],
    )
    async def test_missing_or_unproved_route_is_unavailable_before_hold(
        self, routes: list[ProviderRoute]
    ) -> None:
        deps = _Deps()
        selector, reader, _ = _capacity_routes(routes, {})

        with pytest.raises(ProviderInventoryUnavailableError) as caught:
            await deps.run(fulfillment_routes=selector)

        assert caught.value.__suppress_context__
        reader.server_usage.assert_not_awaited()
        deps.wallets.get.assert_not_awaited()
        deps.holds.create_hold.assert_not_awaited()
        deps.servers.create.assert_not_awaited()
        deps.snaps.create_snapshot.assert_not_awaited()

    @pytest.mark.parametrize("credential_account_id", [None, "default", "hz-original"])
    @pytest.mark.parametrize("current_route", ["full", "draining", "missing"])
    async def test_replay_retains_original_contract_without_selector_calls(
        self, credential_account_id: str | None, current_route: str
    ) -> None:
        deps = _Deps()
        original = _existing()
        original.credential_account_id = credential_account_id
        deps.servers.get_by_idempotency_key.return_value = original
        original_hold = _hold()
        original_snapshot = _snapshot(original.id, _price(199))
        deps.holds.get_by_idempotency.return_value = original_hold
        deps.snaps.get_snapshot.return_value = original_snapshot
        deps.catalog.get_offer.side_effect = AssertionError("replay must not read today's offer")
        deps.accounts.get_active.side_effect = AssertionError("replay must not allocate ownership")
        deps.books.sell_price.side_effect = AssertionError("replay must not reprice")
        routes = (
            []
            if current_route == "missing"
            else [
                _route(
                    "hz-original",
                    10,
                    account_state=(
                        CredentialAccountState.DRAINING
                        if current_route == "draining"
                        else CredentialAccountState.ACTIVE
                    ),
                )
            ]
        )
        selector, reader, repository = _capacity_routes(
            routes, {"hz-original": AccountServerUsage("hz-original", 5, 5)}
        )
        selector.supports_capacity_failover = MagicMock(wraps=selector.supports_capacity_failover)
        selector.account_for = AsyncMock(wraps=selector.account_for)

        result = await deps.run(fulfillment_routes=selector)

        assert result.replayed
        assert result.server is original
        assert result.server.credential_account_id == credential_account_id
        assert result.server.provider_account_id == ACCOUNT_ID
        assert result.snapshot is original_snapshot
        assert result.snapshot.selling_minor == 199
        assert result.hold is original_hold
        selector.supports_capacity_failover.assert_not_called()
        selector.account_for.assert_not_awaited()
        repository.list_for_location.assert_not_awaited()
        reader.server_usage.assert_not_awaited()
        deps.holds.create_hold.assert_not_awaited()
        deps.holds.release_hold.assert_not_awaited()
        deps.servers.create.assert_not_awaited()
        deps.snaps.create_snapshot.assert_not_awaited()

    async def test_other_provider_keeps_existing_command_behavior(self) -> None:
        deps = _Deps()
        selector, reader, repository = _capacity_routes(
            [_route("hz-a", 10)], {"hz-a": AccountServerUsage("hz-a", 5, 5)}
        )
        ref = OfferRef(provider_key="ovh", plan_id="vps", location_id="gra")
        deps.catalog.get_offer.return_value = replace(_offer(), ref=ref)
        deps.books.sell_price.return_value = replace(
            _price(), offer=OfferCost("ovh", "vps", "gra", 100, "EUR")
        )
        deps.accounts.get_active.return_value = ProviderAccount(
            id=ACCOUNT_ID, user_id=USER_ID, provider_key=ref.provider_key
        )
        selector.account_for = AsyncMock(wraps=selector.account_for)

        result = await deps.run(offer_ref=ref, fulfillment_routes=selector)

        assert not result.replayed
        assert result.server.provider_key == "ovh"
        assert result.server.provider_account_id == ACCOUNT_ID
        assert result.server.credential_account_id is None
        assert result.hold is not None
        assert result.hold.amount == 107
        deps.holds.create_hold.assert_awaited_once()
        deps.servers.create.assert_awaited_once()
        deps.snaps.create_snapshot.assert_awaited_once()
        selector.account_for.assert_not_awaited()
        repository.list_for_location.assert_not_awaited()
        reader.server_usage.assert_not_awaited()


async def test_unverified_identity_blocks_generic_purchase_before_effects() -> None:
    deps = _Deps()
    with pytest.raises(IdentityRequiredError):
        await deps.run(user=replace(_user(), phone_verified_at=None))
    deps.catalog.get_offer.assert_not_awaited()
    deps.wallets.get.assert_not_awaited()
    deps.holds.create_hold.assert_not_awaited()
    deps.servers.create.assert_not_awaited()
