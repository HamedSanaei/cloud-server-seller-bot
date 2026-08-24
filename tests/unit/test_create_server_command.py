"""Tests for the create-server application command (M07-001).

Acceptance: validates user/offer/balance and persists intent atomically.
"""

from __future__ import annotations

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
from cloud_platform.modules.users.domain import Role, User, UserStatus
from cloud_platform.modules.wallet.domain import (
    Hold,
    InsufficientHoldBalanceError,
    Wallet,
)

USER_ID = uuid4()
OTHER_USER_ID = uuid4()
OFFER_ID = uuid4()
ACCOUNT_ID = uuid4()
WALLET_ID = uuid4()
KEY = "cmd-key-1"
NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
REF = OfferRef(provider_key="hetzner", plan_id="cx22", location_id="fsn1")


def _user(status: UserStatus = UserStatus.ACTIVE) -> User:
    return User(id=USER_ID, username="alice", email="a@example.com", role=Role.USER, status=status)


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
        return (
            self.service(
                quota=quota, maintenance=maintenance, cost_breaker=cost_breaker
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
