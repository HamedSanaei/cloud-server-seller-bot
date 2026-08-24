"""Tests for cost circuit breakers (M10-004).

Acceptance: global/provider-account daily thresholds can halt new spend.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.catalog.domain import OfferRef, OfferState
from cloud_platform.modules.compute.domain import (
    CostLimit,
    CostLimitScopeKind,
    CostLimitTrigger,
)
from cloud_platform.modules.compute.service import (
    CostCircuitBreakerService,
    CostLimitAdminService,
    CostLimitReachedError,
    CreateServerService,
)
from cloud_platform.modules.pricing.domain import (
    MarginRule,
    OfferCost,
    SellingPrice,
    ServerPriceSnapshot,
)
from cloud_platform.modules.provider_accounts.domain import ProviderAccount
from cloud_platform.modules.users.domain import (
    PermissionDeniedError,
    Role,
    User,
    UserStatus,
)
from cloud_platform.modules.wallet.domain import Hold, Wallet

NOW = datetime(2026, 8, 24, 15, 30, tzinfo=UTC)
USER_ID = uuid4()
OTHER_USER_ID = uuid4()
OFFER_ID = uuid4()
ACCOUNT_A = uuid4()
ACCOUNT_B = uuid4()
SERVER_A1 = uuid4()
SERVER_A2 = uuid4()
SERVER_B1 = uuid4()
WALLET_ID = uuid4()
KEY = "cmd-key-1"
REF = OfferRef(provider_key="hetzner", plan_id="cx22", location_id="fsn1")


class FakeLimitRepo:
    def __init__(self) -> None:
        self.global_limit: CostLimit | None = None
        self.account_limits: dict[UUID, CostLimit] = {}
        self.upserts: list[CostLimit] = []

    async def get_global(self) -> CostLimit | None:
        return self.global_limit

    async def get_for_account(self, provider_account_id: UUID) -> CostLimit | None:
        return self.account_limits.get(provider_account_id)

    async def list_all(self) -> list[CostLimit]:
        out: list[CostLimit] = []
        if self.global_limit is not None:
            out.append(self.global_limit)
        out.extend(self.account_limits.values())
        return out

    async def upsert(self, limit: CostLimit) -> CostLimit:
        self.upserts.append(limit)
        if limit.scope is CostLimitScopeKind.GLOBAL:
            self.global_limit = limit
        else:
            assert limit.provider_account_id is not None
            self.account_limits[limit.provider_account_id] = limit
        return limit

    async def remove(
        self, scope: CostLimitScopeKind, provider_account_id: UUID | None = None
    ) -> bool:
        if scope is CostLimitScopeKind.GLOBAL:
            if self.global_limit is None:
                return False
            self.global_limit = None
            return True
        if provider_account_id in self.account_limits:
            del self.account_limits[provider_account_id]
            return True
        return False


class FakeServerRepo:
    def __init__(self, all_ids: list[UUID], account_ids: dict[UUID, list[UUID]]) -> None:
        self._all_ids = all_ids
        self._account_ids = account_ids

    async def list_non_deleted_ids(self) -> list[UUID]:
        return list(self._all_ids)

    async def list_account_server_ids(self, provider_account_id: UUID) -> list[UUID]:
        return list(self._account_ids.get(provider_account_id, []))


class FakeAccrualRepo:
    """daily_cost_total sums per-server costs that fall in the window."""

    def __init__(self, costs_today: dict[UUID, int]) -> None:
        self._costs_today = costs_today
        self.queries: list[tuple[datetime, datetime, frozenset[UUID]]] = []

    async def daily_cost_total(
        self, day_start: datetime, day_end: datetime, server_ids: frozenset[UUID]
    ) -> int:
        self.queries.append((day_start, day_end, server_ids))
        if not server_ids:
            return 0
        return sum(self._costs_today.get(s, 0) for s in server_ids)


def _breaker(
    limit_repo: FakeLimitRepo,
    server_repo: FakeServerRepo,
    accrual_repo: FakeAccrualRepo,
) -> CostCircuitBreakerService:
    return CostCircuitBreakerService(
        limit_repo=limit_repo,
        server_repo=server_repo,  # type: ignore[arg-type]
        accrual_repo=accrual_repo,
        clock=lambda: NOW,
    )


def _fleet(costs: dict[UUID, int]) -> tuple[FakeServerRepo, FakeAccrualRepo]:
    return (
        FakeServerRepo(
            all_ids=list(costs),
            account_ids={
                ACCOUNT_A: [SERVER_A1, SERVER_A2],
                ACCOUNT_B: [SERVER_B1],
            },
        ),
        FakeAccrualRepo(costs),
    )


class TestCheck:
    async def test_no_limits_never_trips(self) -> None:
        servers, accruals = _fleet({SERVER_A1: 10**6})
        assert await _breaker(FakeLimitRepo(), servers, accruals).check(ACCOUNT_A) is None

    async def test_global_trip_blocks_every_account(self) -> None:
        limits = FakeLimitRepo()
        limits.global_limit = CostLimit(scope=CostLimitScopeKind.GLOBAL, limit_minor=1000)
        # account A spent 600, account B 500 -> global 1100 >= 1000
        servers, accruals = _fleet({SERVER_A1: 400, SERVER_A2: 200, SERVER_B1: 500})
        trigger = await _breaker(limits, servers, accruals).check(ACCOUNT_B)
        assert trigger is not None
        assert trigger.scope is CostLimitScopeKind.GLOBAL
        assert trigger.provider_account_id is None
        assert trigger.limit_minor == 1000
        assert trigger.spent_minor == 1100

    async def test_account_trip_blocks_only_that_account(self) -> None:
        limits = FakeLimitRepo()
        limits.account_limits[ACCOUNT_A] = CostLimit(
            scope=CostLimitScopeKind.PROVIDER_ACCOUNT,
            limit_minor=600,
            provider_account_id=ACCOUNT_A,
        )
        servers, accruals = _fleet({SERVER_A1: 400, SERVER_A2: 200, SERVER_B1: 500})
        assert await _breaker(limits, servers, accruals).check(ACCOUNT_B) is None
        trigger = await _breaker(limits, servers, accruals).check(ACCOUNT_A)
        assert trigger is not None
        assert trigger.scope is CostLimitScopeKind.PROVIDER_ACCOUNT
        assert trigger.provider_account_id == ACCOUNT_A
        assert trigger.spent_minor == 600

    async def test_account_trigger_wins_over_global(self) -> None:
        limits = FakeLimitRepo()
        limits.global_limit = CostLimit(scope=CostLimitScopeKind.GLOBAL, limit_minor=500)
        limits.account_limits[ACCOUNT_A] = CostLimit(
            scope=CostLimitScopeKind.PROVIDER_ACCOUNT,
            limit_minor=100,
            provider_account_id=ACCOUNT_A,
        )
        servers, accruals = _fleet({SERVER_A1: 300})
        trigger = await _breaker(limits, servers, accruals).check(ACCOUNT_A)
        # both tripped; the most specific (account) scope is reported
        assert trigger is not None
        assert trigger.scope is CostLimitScopeKind.PROVIDER_ACCOUNT
        assert trigger.limit_minor == 100

    async def test_disabled_or_zero_limit_never_trips(self) -> None:
        for limit in (
            CostLimit(
                scope=CostLimitScopeKind.PROVIDER_ACCOUNT,
                limit_minor=10,
                provider_account_id=ACCOUNT_A,
                enabled=False,
            ),
            CostLimit(
                scope=CostLimitScopeKind.PROVIDER_ACCOUNT,
                limit_minor=0,
                provider_account_id=ACCOUNT_A,
            ),
        ):
            limits = FakeLimitRepo()
            limits.account_limits[ACCOUNT_A] = limit
            servers, accruals = _fleet({SERVER_A1: 500})
            assert await _breaker(limits, servers, accruals).check(ACCOUNT_A) is None

    async def test_spend_counts_only_the_day_window_and_scope(self) -> None:
        # a limit that is NOT met: spend below the cap -> allowed
        limits = FakeLimitRepo()
        limits.global_limit = CostLimit(scope=CostLimitScopeKind.GLOBAL, limit_minor=1000)
        servers, accruals = _fleet({SERVER_A1: 400, SERVER_A2: 200, SERVER_B1: 300})
        assert await _breaker(limits, servers, accruals).check(ACCOUNT_B) is None
        # the window queried is the current UTC day
        (day_start, day_end, ids) = accruals.queries[0]
        assert day_start == datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
        assert day_end == datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
        assert ids == frozenset({SERVER_A1, SERVER_A2, SERVER_B1})

    async def test_per_account_scope_uses_only_its_servers(self) -> None:
        limits = FakeLimitRepo()
        limits.account_limits[ACCOUNT_B] = CostLimit(
            scope=CostLimitScopeKind.PROVIDER_ACCOUNT,
            limit_minor=500,
            provider_account_id=ACCOUNT_B,
        )
        servers, accruals = _fleet({SERVER_A1: 900, SERVER_A2: 900, SERVER_B1: 500})
        # account A's big spend must not trip account B's limit
        assert await _breaker(limits, servers, accruals).check(ACCOUNT_A) is None
        # account B is exactly at its limit -> tripped
        trigger = await _breaker(limits, servers, accruals).check(ACCOUNT_B)
        assert trigger is not None
        assert trigger.spent_minor == 500


class TestStatus:
    async def test_lists_active_limits_with_spent(self) -> None:
        limits = FakeLimitRepo()
        limits.global_limit = CostLimit(scope=CostLimitScopeKind.GLOBAL, limit_minor=1000)
        limits.account_limits[ACCOUNT_A] = CostLimit(
            scope=CostLimitScopeKind.PROVIDER_ACCOUNT,
            limit_minor=100,
            provider_account_id=ACCOUNT_A,
        )
        servers, accruals = _fleet({SERVER_A1: 400, SERVER_A2: 200, SERVER_B1: 500})
        status = await _breaker(limits, servers, accruals).status()
        assert {(lim.scope, spent) for lim, spent in status} == {
            (CostLimitScopeKind.GLOBAL, 1100),
            (CostLimitScopeKind.PROVIDER_ACCOUNT, 600),
        }

    async def test_inactive_limits_are_skipped(self) -> None:
        limits = FakeLimitRepo()
        limits.account_limits[ACCOUNT_A] = CostLimit(
            scope=CostLimitScopeKind.PROVIDER_ACCOUNT,
            limit_minor=10,
            provider_account_id=ACCOUNT_A,
            enabled=False,
        )
        servers, accruals = _fleet({SERVER_A1: 400})
        assert await _breaker(limits, servers, accruals).status() == []


class TestCreateCommandGate:
    def _deps(self):
        servers = AsyncMock()
        servers.get_by_idempotency_key = AsyncMock(return_value=None)
        servers.create = AsyncMock(side_effect=lambda s, i: s)
        servers.save = AsyncMock(side_effect=lambda s: s)
        servers.count_active = AsyncMock(return_value=0)
        servers.count_total = AsyncMock(return_value=0)
        accounts = AsyncMock()
        accounts.get_active = AsyncMock(
            return_value=ProviderAccount(id=ACCOUNT_A, user_id=USER_ID, provider_key="hetzner")
        )
        catalog = AsyncMock()
        catalog.get_offer = AsyncMock(
            return_value=OfferState(
                id=OFFER_ID,
                ref=REF,
                name="CX22",
                enabled=True,
                price_per_quantum=100,
                currency="EUR",
            )
        )
        books = MagicMock()
        books.sell_price = AsyncMock(
            return_value=SellingPrice(
                offer=OfferCost("hetzner", "cx22", "fsn1", 100, "EUR"),
                selling_minor=107,
                rule=MarginRule("*", "*", "*", Decimal("1.07")),
                book_name="retail-eur",
                version=1,
                priced_at=NOW,
            )
        )
        snaps = MagicMock()
        snaps.create_snapshot = AsyncMock(
            side_effect=lambda **kw: ServerPriceSnapshot(
                server_id=kw["server_id"],
                offer=kw["price"].offer,
                selling_minor=kw["price"].selling_minor,
                book_name=kw["price"].book_name,
                book_version=kw["price"].version,
                rule=kw["price"].rule,
                priced_at=kw["price"].priced_at,
                id=uuid4(),
            )
        )
        snaps.get_snapshot = AsyncMock(return_value=None)
        wallets = AsyncMock()
        wallets.get = AsyncMock(
            return_value=Wallet(user_id=USER_ID, id=WALLET_ID, balance=1000, currency="EUR")
        )
        holds = AsyncMock()
        holds.create_hold = AsyncMock(
            return_value=Hold(
                wallet_id=WALLET_ID,
                amount=107,
                currency="EUR",
                idempotency_key=f"server-create:{KEY}",
                id=uuid4(),
            )
        )
        holds.get_by_idempotency = AsyncMock(return_value=None)
        holds.release_hold = AsyncMock(return_value=None)
        audit = AsyncMock()
        audit.append = AsyncMock(side_effect=lambda e: e)

        class _Bundle:
            pass

        bundle = _Bundle()
        bundle.servers = servers
        bundle.accounts = accounts
        bundle.catalog = catalog
        bundle.books = books
        bundle.snaps = snaps
        bundle.wallets = wallets
        bundle.holds = holds
        bundle.audit = audit
        return bundle

    @staticmethod
    def _service(bundle, cost_breaker=None) -> CreateServerService:
        return CreateServerService(
            server_repo=bundle.servers,  # type: ignore[arg-type]
            account_repo=bundle.accounts,  # type: ignore[arg-type]
            catalog_repo=bundle.catalog,  # type: ignore[arg-type]
            price_book_service=bundle.books,  # type: ignore[arg-type]
            snapshot_service=bundle.snaps,  # type: ignore[arg-type]
            wallet_repo=bundle.wallets,  # type: ignore[arg-type]
            hold_repo=bundle.holds,  # type: ignore[arg-type]
            audit_repo=bundle.audit,  # type: ignore[arg-type]
            book_name="retail-eur",
            cost_breaker=cost_breaker,
        )

    async def test_tripped_breaker_halts_new_orders(self) -> None:
        class _TrippedBreaker:
            def __init__(self) -> None:
                self.checked: list[UUID] = []

            async def check(self, provider_account_id: UUID) -> CostLimitTrigger | None:
                self.checked.append(provider_account_id)
                return CostLimitTrigger(
                    scope=CostLimitScopeKind.GLOBAL,
                    provider_account_id=None,
                    limit_minor=1000,
                    spent_minor=1100,
                )

        bundle = self._deps()
        breaker = _TrippedBreaker()
        with pytest.raises(CostLimitReachedError, match="daily provider cost"):
            await self._service(bundle, cost_breaker=breaker).create_server(
                user=User(
                    id=USER_ID,
                    username="alice",
                    email="a@example.com",
                    role=Role.USER,
                    status=UserStatus.ACTIVE,
                ),
                offer_ref=REF,
                idempotency_key=KEY,
                at=NOW,
            )

        # consulted for the order's account, before any money was reserved
        assert breaker.checked == [ACCOUNT_A]
        assert bundle.holds.create_hold.call_count == 0
        assert bundle.servers.create.call_count == 0

    async def test_clear_breaker_allows_orders(self) -> None:
        class _ClearBreaker:
            async def check(self, provider_account_id: UUID) -> CostLimitTrigger | None:
                return None

        bundle = self._deps()
        result = await self._service(bundle, cost_breaker=_ClearBreaker()).create_server(
            user=User(
                id=USER_ID,
                username="alice",
                email="a@example.com",
                role=Role.USER,
                status=UserStatus.ACTIVE,
            ),
            offer_ref=REF,
            idempotency_key=KEY,
            at=NOW,
        )
        assert result.replayed is False
        assert result.hold is not None


class TestAdminService:
    def _service(self, repo: FakeLimitRepo):
        audit = AsyncMock()
        audit.append = AsyncMock(side_effect=lambda e: e)
        return CostLimitAdminService(limit_repo=repo, audit_repo=audit), audit  # type: ignore[arg-type]

    async def test_set_global_is_audited(self) -> None:
        repo = FakeLimitRepo()
        service, audit = self._service(repo)
        saved = await service.set_global(actor=None, limit_minor=2500, reason="cap the day")
        assert saved.limit_minor == 2500
        assert repo.global_limit is not None
        (event,) = [c.args[0] for c in audit.append.call_args_list]
        assert event.action == "cost_limit.set"
        assert event.metadata["scope"] == "global"
        assert event.metadata["limit_minor"] == "2500"

    async def test_set_for_account_requires_admin_actor(self) -> None:
        repo = FakeLimitRepo()
        service, _ = self._service(repo)
        non_admin = User(
            id=uuid4(),
            username="bob",
            email="b@example.com",
            role=Role.USER,
            status=UserStatus.ACTIVE,
        )
        with pytest.raises(PermissionDeniedError):
            await service.set_for_account(
                actor=non_admin,
                provider_account_id=ACCOUNT_A,
                limit_minor=100,
            )
        assert repo.account_limits == {}

    async def test_clear_removes_the_row(self) -> None:
        repo = FakeLimitRepo()
        repo.account_limits[ACCOUNT_A] = CostLimit(
            scope=CostLimitScopeKind.PROVIDER_ACCOUNT,
            limit_minor=100,
            provider_account_id=ACCOUNT_A,
        )
        service, _ = self._service(repo)
        assert await service.clear(actor=None, provider_account_id=ACCOUNT_A) is True
        assert repo.account_limits == {}
        assert await service.clear(actor=None, provider_account_id=ACCOUNT_A) is False

    async def test_negative_limit_rejected(self) -> None:
        repo = FakeLimitRepo()
        service, _ = self._service(repo)
        with pytest.raises(ValueError):
            await service.set_global(actor=None, limit_minor=-1)

    async def test_list_returns_all_rows(self) -> None:
        repo = FakeLimitRepo()
        repo.global_limit = CostLimit(scope=CostLimitScopeKind.GLOBAL, limit_minor=100)
        repo.account_limits[ACCOUNT_A] = CostLimit(
            scope=CostLimitScopeKind.PROVIDER_ACCOUNT,
            limit_minor=50,
            provider_account_id=ACCOUNT_A,
        )
        service, _ = self._service(repo)
        rows = await service.list()
        assert {r.scope for r in rows} == {
            CostLimitScopeKind.GLOBAL,
            CostLimitScopeKind.PROVIDER_ACCOUNT,
        }


class TestDomain:
    def test_global_limit_rejects_account_id(self) -> None:
        with pytest.raises(ValueError):
            CostLimit(
                scope=CostLimitScopeKind.GLOBAL,
                limit_minor=10,
                provider_account_id=ACCOUNT_A,
            )

    def test_account_limit_requires_account_id(self) -> None:
        with pytest.raises(ValueError):
            CostLimit(scope=CostLimitScopeKind.PROVIDER_ACCOUNT, limit_minor=10)

    def test_negative_limit_rejected(self) -> None:
        with pytest.raises(ValueError):
            CostLimit(scope=CostLimitScopeKind.GLOBAL, limit_minor=-1)

    def test_active_requires_enabled_positive_limit(self) -> None:
        assert CostLimit(scope=CostLimitScopeKind.GLOBAL, limit_minor=10).active is True
        assert CostLimit(scope=CostLimitScopeKind.GLOBAL, limit_minor=0).active is False
        assert (
            CostLimit(scope=CostLimitScopeKind.GLOBAL, limit_minor=10, enabled=False).active
            is False
        )
