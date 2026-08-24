"""Tests for the low-balance policy (M06-007).

Acceptance: warn/grace/auto-delete decisions are deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.billing.service import (
    LowBalanceDecision,
    LowBalancePolicyConfig,
    LowBalancePolicyService,
    decide_low_balance,
)
from cloud_platform.modules.compute.domain import CloudServer, ServerLifecycleState
from cloud_platform.modules.wallet.domain import Wallet

T0 = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
SERVER_ID = uuid4()
USER_ID = uuid4()
WALLET_ID = uuid4()
CONFIG = LowBalancePolicyConfig(threshold_minor=1000, grace_hours=24)


def _server(**kw: object) -> CloudServer:
    defaults: dict[str, object] = dict(
        id=SERVER_ID,
        user_id=USER_ID,
        provider_key="hetzner",
        provider_account_id=uuid4(),
        state=ServerLifecycleState.RUNNING,
        idempotency_key="ik",
        created_at=T0,
    )
    defaults.update(kw)
    return CloudServer(**defaults)  # type: ignore[arg-type]


class TestDecideDeterminism:
    """The pure decision function: same inputs, same decision."""

    def test_healthy_is_none(self) -> None:
        assert decide_low_balance(1000, CONFIG, None, T0) is LowBalanceDecision.NONE
        assert decide_low_balance(10**9, CONFIG, None, T0) is LowBalanceDecision.NONE

    def test_threshold_boundary_is_healthy(self) -> None:
        assert (
            decide_low_balance(CONFIG.threshold_minor, CONFIG, None, T0) is LowBalanceDecision.NONE
        )

    def test_just_below_is_warn(self) -> None:
        assert (
            decide_low_balance(CONFIG.threshold_minor - 1, CONFIG, None, T0)
            is LowBalanceDecision.WARN
        )

    def test_within_grace_is_grace(self) -> None:
        since = T0
        now = T0 + timedelta(hours=23, minutes=59, seconds=59)
        assert decide_low_balance(5, CONFIG, since, now) is LowBalanceDecision.GRACE

    def test_grace_boundary_is_auto_delete(self) -> None:
        since = T0
        now = T0 + timedelta(hours=24)  # exactly the grace window
        assert decide_low_balance(5, CONFIG, since, now) is LowBalanceDecision.AUTO_DELETE

    def test_beyond_grace_is_auto_delete(self) -> None:
        since = T0
        assert (
            decide_low_balance(5, CONFIG, since, T0 + timedelta(hours=48))
            is LowBalanceDecision.AUTO_DELETE
        )

    def test_recovery_clears(self) -> None:
        since = T0
        assert (
            decide_low_balance(CONFIG.threshold_minor, CONFIG, since, T0 + timedelta(hours=1))
            is LowBalanceDecision.RECOVERED
        )

    def test_same_inputs_same_decision(self) -> None:
        """Determinism: repeated calls with identical inputs never diverge."""
        for balance in (0, 999, 1000, 1001):
            for since in (None, T0):
                decisions = {
                    decide_low_balance(balance, CONFIG, since, T0 + timedelta(hours=i))
                    for i in range(10)
                }
                assert len(decisions) == 1

    def test_naive_watermark_treated_as_utc(self) -> None:
        since = datetime(2026, 8, 24, 12, 0)  # naive
        assert (
            decide_low_balance(5, CONFIG, since, T0 + timedelta(hours=24))
            is LowBalanceDecision.AUTO_DELETE
        )

    def test_config_validation(self) -> None:
        with pytest.raises(ValueError):
            LowBalancePolicyConfig(threshold_minor=-1, grace_hours=1)
        with pytest.raises(ValueError):
            LowBalancePolicyConfig(threshold_minor=0, grace_hours=-1)


class Fakes:
    def __init__(self, servers: list[CloudServer], balances: dict[UUID, int]) -> None:
        self.servers = servers
        self.balances = balances
        self.saved: list[CloudServer] = []
        self.notified: list[tuple[UUID, UUID, LowBalanceDecision, int]] = []

    async def list_running(self) -> list[CloudServer]:
        return list(self.servers)

    async def save(self, server: CloudServer) -> CloudServer:
        self.saved.append(server)
        return server

    async def wallet_get(self, user_id) -> Wallet | None:
        return Wallet(USER_ID, id=WALLET_ID, balance=self.balances.get(user_id, 0))

    async def notify(self, user_id, server_id, decision, balance_minor) -> None:
        self.notified.append((user_id, server_id, decision, balance_minor))


def _service(fakes: Fakes) -> LowBalancePolicyService:
    h = fakes

    class _WalletRepo:
        async def get(self, user_id):
            return await h.wallet_get(user_id)

    return LowBalancePolicyService(
        server_repo=fakes,
        wallet_repo=_WalletRepo(),
        audit_repo=AsyncMock(),
        notifier=fakes,
    )


class TestServiceStateMachine:
    async def test_healthy_server_untouched(self) -> None:
        s = _server()
        fakes = Fakes([s], {USER_ID: 5000})
        report = await _service(fakes).evaluate(CONFIG, now=T0)

        assert report.none == 1
        assert fakes.saved == []
        assert fakes.notified == []
        assert s.low_balance_since is None

    async def test_falling_server_warns_and_starts_clock(self) -> None:
        s = _server()
        fakes = Fakes([s], {USER_ID: 400})
        report = await _service(fakes).evaluate(CONFIG, now=T0)

        assert report.warn == 1
        assert s.low_balance_since == T0
        assert len(fakes.saved) == 1
        assert fakes.notified == [(USER_ID, SERVER_ID, LowBalanceDecision.WARN, 400)]
        assert s.state is ServerLifecycleState.RUNNING

    async def test_within_grace_is_silent(self) -> None:
        s = _server(low_balance_since=T0)
        fakes = Fakes([s], {USER_ID: 400})
        report = await _service(fakes).evaluate(CONFIG, now=T0 + timedelta(hours=5))

        assert report.grace == 1
        assert fakes.notified == []  # already warned when the window opened
        assert fakes.saved == []  # watermark unchanged
        assert s.state is ServerLifecycleState.RUNNING

    async def test_grace_exhausted_requests_deletion(self) -> None:
        s = _server(low_balance_since=T0)
        fakes = Fakes([s], {USER_ID: 400})
        report = await _service(fakes).evaluate(CONFIG, now=T0 + timedelta(hours=25))

        assert report.auto_delete == 1
        assert s.state is ServerLifecycleState.DELETE_REQUESTED  # the saga takes over
        assert fakes.notified == [(USER_ID, SERVER_ID, LowBalanceDecision.AUTO_DELETE, 400)]
        assert len(fakes.saved) == 1

    async def test_recovery_clears_watermark(self) -> None:
        s = _server(low_balance_since=T0)
        fakes = Fakes([s], {USER_ID: 5000})
        report = await _service(fakes).evaluate(CONFIG, now=T0 + timedelta(hours=2))

        assert report.recovered == 1
        assert s.low_balance_since is None
        assert len(fakes.saved) == 1
        assert fakes.notified == []
        assert s.state is ServerLifecycleState.RUNNING

    async def test_no_wallet_counts_as_below_threshold(self) -> None:
        s = _server()
        fakes = Fakes([s], {})  # no balance entry -> balance 0
        report = await _service(fakes).evaluate(CONFIG, now=T0)

        assert report.warn == 1
        assert s.low_balance_since == T0

    async def test_mixed_fleet(self) -> None:
        healthy = _server()
        warning = _server(id=uuid4(), user_id=uuid4(), idempotency_key="b")
        in_grace = _server(
            id=uuid4(),
            user_id=uuid4(),
            idempotency_key="c",
            low_balance_since=T0 + timedelta(hours=20),  # 10h into the window at T0+30h
        )
        expired = _server(id=uuid4(), user_id=uuid4(), idempotency_key="d", low_balance_since=T0)
        fakes = Fakes(
            [healthy, warning, in_grace, expired],
            {
                USER_ID: 5000,
                warning.user_id: 100,
                in_grace.user_id: 100,
                expired.user_id: 100,
            },
        )
        report = await _service(fakes).evaluate(CONFIG, now=T0 + timedelta(hours=30))

        assert report.servers_checked == 4
        assert report.none == 1
        assert report.warn == 1
        assert report.grace == 1
        assert report.auto_delete == 1
        assert expired.state is ServerLifecycleState.DELETE_REQUESTED
        assert warning.state is ServerLifecycleState.RUNNING
        assert in_grace.state is ServerLifecycleState.RUNNING

    async def test_audit_action_per_decision(self) -> None:
        s = _server()
        fakes = Fakes([s], {USER_ID: 400})
        audit = AsyncMock()

        class _WalletRepo:
            async def get(self, user_id):
                return await fakes.wallet_get(user_id)

        service = LowBalancePolicyService(
            server_repo=fakes, wallet_repo=_WalletRepo(), audit_repo=audit, notifier=fakes
        )
        await service.evaluate(CONFIG, now=T0)

        event = audit.append.call_args.args[0]
        assert event.action == "billing.low_balance_warn"
        assert event.metadata["balance"] == "400"
        assert event.metadata["threshold"] == "1000"
