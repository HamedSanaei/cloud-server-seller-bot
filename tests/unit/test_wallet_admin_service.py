"""Application authorization and validation for privileged wallet mutations."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cloud_platform.modules.users.domain import PermissionDeniedError, Role, User, UserStatus
from cloud_platform.modules.wallet.domain import Hold, HoldStateConflictError, HoldStatus, Wallet
from cloud_platform.modules.wallet.service import HoldAdminService, WalletAdminService

ADMIN_ID = uuid4()
USER_ID = uuid4()
WALLET_ID = uuid4()


def _admin(role: Role = Role.ADMIN) -> User:
    return User(
        id=ADMIN_ID, username="boss", email="boss@example.com", role=role, telegram_user_id=77110022
    )


@pytest.mark.parametrize(
    "role,status",
    [
        (Role.USER, UserStatus.ACTIVE),
        (Role.ADMIN, UserStatus.FROZEN),
        (Role.ADMIN, UserStatus.BANNED),
    ],
)
async def test_wallet_adjustment_requires_active_admin(role: Role, status: UserStatus) -> None:
    admin = _admin(role)
    admin.status = status
    wallet = AsyncMock()
    service = WalletAdminService(wallet, AsyncMock(), configured_ids=(77110022,))
    with pytest.raises(PermissionDeniedError):
        await service.adjust_balance(
            admin=admin, user_id=USER_ID, amount=500, reason="credit", idempotency_key="guard"
        )
    wallet.adjust.assert_not_awaited()


async def test_superadmin_cannot_credit_own_account() -> None:
    wallet = AsyncMock()
    service = WalletAdminService(wallet, AsyncMock(), configured_ids=(77110022,))
    with pytest.raises(ValueError, match="self top-up"):
        await service.adjust_balance(
            admin=_admin(), user_id=ADMIN_ID, amount=500, reason="credit", idempotency_key="self"
        )
    wallet.adjust.assert_not_awaited()


@pytest.mark.parametrize("amount", [0, True, 1.5, 2**63, -(2**63) - 1])
async def test_invalid_adjustment_amount_is_rejected_before_money(amount: object) -> None:
    wallet = AsyncMock()
    service = WalletAdminService(wallet, AsyncMock(), configured_ids=(77110022,))
    with pytest.raises(ValueError):
        await service.adjust_balance(
            admin=_admin(), user_id=USER_ID, amount=amount, reason="credit", idempotency_key="bad"
        )
    wallet.adjust.assert_not_awaited()


async def test_empty_adjustment_reason_is_rejected() -> None:
    wallet = AsyncMock()
    service = WalletAdminService(wallet, AsyncMock(), configured_ids=(77110022,))
    with pytest.raises(ValueError, match="reason"):
        await service.adjust_balance(
            admin=_admin(), user_id=USER_ID, amount=100, reason=" ", idempotency_key="reason"
        )
    wallet.adjust.assert_not_awaited()


async def test_unconfigured_legacy_admin_and_forged_actor_never_adjust_wallet() -> None:
    wallet = AsyncMock()
    for actor in (
        _admin(),
        SimpleNamespace(
            id=ADMIN_ID, role=Role.ADMIN, status=UserStatus.ACTIVE, telegram_user_id=77110022
        ),
    ):
        with pytest.raises(PermissionDeniedError):
            await WalletAdminService(wallet, AsyncMock()).adjust_balance(
                admin=actor, user_id=USER_ID, amount=100, reason="review", idempotency_key="guard"
            )
    wallet.adjust.assert_not_awaited()


@pytest.mark.parametrize("change", ["revoked", "identity", "missing"])
async def test_fresh_actor_recheck_rejects_stale_privileges(change: str) -> None:
    actor = _admin()
    fresh = {
        "revoked": replace(actor, role=Role.USER),
        "identity": replace(actor, telegram_user_id=99110022),
        "missing": None,
    }[change]
    users = SimpleNamespace(get=AsyncMock(return_value=fresh))
    wallet = AsyncMock()
    service = WalletAdminService(wallet, AsyncMock(), user_repo=users, configured_ids=(77110022,))
    with pytest.raises(PermissionDeniedError):
        await service.adjust_balance(
            admin=actor, user_id=USER_ID, amount=100, reason="review", idempotency_key="guard"
        )
    wallet.adjust.assert_not_awaited()


async def test_confirmed_currency_reason_actor_and_policy_reach_atomic_repository() -> None:
    result = Wallet(id=WALLET_ID, user_id=USER_ID, currency="USD", balance=100)
    wallets = AsyncMock()
    wallets.adjust.return_value = (result, True)
    ledger = AsyncMock()
    ledger.get_entry_by_idempotency.return_value = object()
    actor = _admin()
    users = SimpleNamespace(
        get=AsyncMock(side_effect=lambda uid: actor if uid == ADMIN_ID else None)
    )
    service = WalletAdminService(wallets, ledger, user_repo=users, configured_ids=(77110022,))
    await service.adjust_balance(
        admin=actor,
        user_id=USER_ID,
        amount=100,
        reason="support case 42",
        idempotency_key="confirmed",
        expected_currency="USD",
    )
    kwargs = wallets.adjust.await_args.kwargs
    assert kwargs["expected_currency"] == "USD"
    assert kwargs["description"] == "support case 42"
    assert kwargs["audit_actor_id"] == ADMIN_ID
    assert kwargs["admin_configured_ids"] == (77110022,)


class TestForceRelease:
    """M10-002: admin hold releases are authorized and audited."""

    HOLD_ID = uuid4()

    def _hold(self, status: HoldStatus = HoldStatus.RELEASED) -> Hold:
        return Hold(
            wallet_id=WALLET_ID,
            amount=500,
            currency="EUR",
            idempotency_key="hold-key-1",
            id=self.HOLD_ID,
            status=status,
        )

    def _service(self, hold_service: AsyncMock, audit_repo: AsyncMock) -> HoldAdminService:
        return HoldAdminService(hold_service, audit_repo)

    async def test_non_admin_cannot_force_release(self) -> None:
        hold_service = AsyncMock()
        audit_repo = AsyncMock()
        service = self._service(hold_service, audit_repo)

        with pytest.raises(PermissionDeniedError):
            await service.force_release(
                admin=_admin(Role.USER),
                wallet_id=WALLET_ID,
                hold_id=self.HOLD_ID,
                reason="dispute",
                idempotency_key="fr-1",
            )
        hold_service.release_hold.assert_not_awaited()
        audit_repo.append.assert_not_awaited()

    async def test_empty_reason_rejected_before_release(self) -> None:
        hold_service = AsyncMock()
        audit_repo = AsyncMock()
        service = self._service(hold_service, audit_repo)

        with pytest.raises(ValueError, match="reason"):
            await service.force_release(
                admin=_admin(),
                wallet_id=WALLET_ID,
                hold_id=self.HOLD_ID,
                reason="",
                idempotency_key="fr-1",
            )
        hold_service.release_hold.assert_not_awaited()

    async def test_happy_path_releases_and_audits(self) -> None:
        hold_service = AsyncMock()
        hold_service.release_hold = AsyncMock(return_value=self._hold())
        audit_repo = AsyncMock()
        service = self._service(hold_service, audit_repo)

        released = await service.force_release(
            admin=_admin(),
            wallet_id=WALLET_ID,
            hold_id=self.HOLD_ID,
            reason="customer dispute resolution",
            idempotency_key="fr-1",
        )

        hold_service.release_hold.assert_awaited_once_with(WALLET_ID, self.HOLD_ID, "fr-1")
        assert released.status is HoldStatus.RELEASED

        event = audit_repo.append.call_args[0][0]
        assert event.action == "wallet.force_release"
        assert event.actor_type.value == "admin"
        assert event.actor_id == ADMIN_ID
        assert event.resource_id == str(WALLET_ID)
        assert event.reason == "customer dispute resolution"
        assert event.metadata == {
            "hold_id": str(self.HOLD_ID),
            "amount": "500",
            "currency": "EUR",
        }

    async def test_state_conflict_propagates_without_audit(self) -> None:
        hold_service = AsyncMock()
        hold_service.release_hold = AsyncMock(
            side_effect=HoldStateConflictError("already captured")
        )
        audit_repo = AsyncMock()
        service = self._service(hold_service, audit_repo)

        with pytest.raises(HoldStateConflictError):
            await service.force_release(
                admin=_admin(),
                wallet_id=WALLET_ID,
                hold_id=self.HOLD_ID,
                reason="dispute",
                idempotency_key="fr-2",
            )
        audit_repo.append.assert_not_awaited()
