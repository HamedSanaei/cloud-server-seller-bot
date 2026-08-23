"""Tests for WalletAdminService: audited admin credit/debit adjustments (M05-006)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cloud_platform.core.money import Money
from cloud_platform.modules.users.domain import (
    PermissionDeniedError,
    Role,
    User,
)
from cloud_platform.modules.wallet.domain import (
    Hold,
    HoldStateConflictError,
    HoldStatus,
    InsufficientBalanceError,
    LedgerEntry,
    LedgerEntryType,
    Wallet,
)
from cloud_platform.modules.wallet.service import HoldAdminService, WalletAdminService

ADMIN_ID = uuid4()
USER_ID = uuid4()
WALLET_ID = uuid4()


def _admin(role: Role = Role.ADMIN) -> User:
    return User(
        id=ADMIN_ID,
        username="boss",
        email="boss@example.com",
        role=role,
    )


def _wallet(balance: int = 1000) -> Wallet:
    return Wallet(user_id=USER_ID, id=WALLET_ID, balance=balance, currency="EUR")


def _entry(amount: int = 500) -> LedgerEntry:
    return LedgerEntry(
        id=uuid4(),
        wallet_id=WALLET_ID,
        entry_type=LedgerEntryType.ADJUSTMENT,
        amount=Money(Decimal(amount), "EUR"),
        reference_type="admin_adjustment",
        reference_id=str(ADMIN_ID),
        idempotency_key="adj-key-1",
    )


def _service(
    wallet_repo: AsyncMock, ledger_repo: AsyncMock, audit_repo: AsyncMock
) -> WalletAdminService:
    return WalletAdminService(wallet_repo, ledger_repo, audit_repo)


class TestAuthorization:
    async def test_non_admin_cannot_adjust(self) -> None:
        """The WALLET_ADJUST permission gate runs before any persistence."""
        wallet_repo = AsyncMock()
        ledger_repo = AsyncMock()
        audit_repo = AsyncMock()
        service = _service(wallet_repo, ledger_repo, audit_repo)

        with pytest.raises(PermissionDeniedError):
            await service.adjust_balance(
                admin=_admin(Role.USER),
                user_id=USER_ID,
                amount=500,
                reason="goodwill",
                idempotency_key="k1",
            )
        wallet_repo.get.assert_not_awaited()
        ledger_repo.post_entry.assert_not_awaited()
        audit_repo.append.assert_not_awaited()

    async def test_admin_can_adjust(self) -> None:
        wallet_repo = AsyncMock()
        wallet_repo.get = AsyncMock(return_value=_wallet())
        wallet_repo.add_funds = AsyncMock(return_value=_wallet(1500))
        ledger_repo = AsyncMock()
        ledger_repo.get_entry_by_idempotency = AsyncMock(return_value=None)
        ledger_repo.post_entry = AsyncMock(return_value=_entry())
        audit_repo = AsyncMock()

        service = _service(wallet_repo, ledger_repo, audit_repo)
        updated, _entry_result = await service.adjust_balance(
            admin=_admin(),
            user_id=USER_ID,
            amount=500,
            reason="goodwill credit",
            idempotency_key="k1",
        )
        assert updated.balance == 1500


class TestValidation:
    async def test_zero_amount_rejected(self) -> None:
        service = _service(AsyncMock(), AsyncMock(), AsyncMock())
        with pytest.raises(ValueError, match="zero"):
            await service.adjust_balance(
                admin=_admin(), user_id=USER_ID, amount=0, reason="r", idempotency_key="k"
            )

    async def test_empty_reason_rejected(self) -> None:
        service = _service(AsyncMock(), AsyncMock(), AsyncMock())
        with pytest.raises(ValueError, match="reason"):
            await service.adjust_balance(
                admin=_admin(), user_id=USER_ID, amount=100, reason="   ", idempotency_key="k"
            )

    async def test_missing_wallet_raises(self) -> None:
        wallet_repo = AsyncMock()
        wallet_repo.get = AsyncMock(return_value=None)
        service = _service(wallet_repo, AsyncMock(), AsyncMock())

        with pytest.raises(ValueError, match="no wallet"):
            await service.adjust_balance(
                admin=_admin(), user_id=USER_ID, amount=100, reason="r", idempotency_key="k"
            )


class TestAdjustmentApplication:
    async def test_credit_posts_ledger_and_audit(self) -> None:
        wallet_repo = AsyncMock()
        wallet_repo.get = AsyncMock(return_value=_wallet(1000))
        wallet_repo.add_funds = AsyncMock(return_value=_wallet(1500))
        ledger_repo = AsyncMock()
        ledger_repo.get_entry_by_idempotency = AsyncMock(return_value=None)
        ledger_repo.post_entry = AsyncMock(return_value=_entry(500))
        audit_repo = AsyncMock()

        service = _service(wallet_repo, ledger_repo, audit_repo)
        updated, entry = await service.adjust_balance(
            admin=_admin(),
            user_id=USER_ID,
            amount=500,
            reason="goodwill credit",
            idempotency_key="adj-key-1",
        )

        # Balance applied
        wallet_repo.add_funds.assert_awaited_once_with(USER_ID, 500, "adj-key-1")
        assert updated.balance == 1500

        # Ledger entry: type, amount, key, description carries the reason
        args, kwargs = ledger_repo.post_entry.call_args
        assert args[0] == WALLET_ID
        assert args[1] == 500
        assert args[3] is LedgerEntryType.ADJUSTMENT
        assert args[4] == "adj-key-1"
        assert kwargs["description"] == "goodwill credit"

        # Audit trail: actor + reason are recorded
        audit_repo.append.assert_awaited_once()
        event = audit_repo.append.call_args[0][0]
        assert event.action == "wallet.adjust"
        assert event.actor_type.value == "admin"
        assert event.actor_id == ADMIN_ID
        assert event.resource_type == "wallet"
        assert event.resource_id == str(WALLET_ID)
        assert event.reason == "goodwill credit"
        assert event.metadata == {"amount": "500", "currency": "EUR"}
        assert entry.idempotency_key == "adj-key-1"

    async def test_debit_uses_absolute_amount(self) -> None:
        wallet_repo = AsyncMock()
        wallet_repo.get = AsyncMock(return_value=_wallet(1000))
        wallet_repo.debit = AsyncMock(return_value=_wallet(700))
        ledger_repo = AsyncMock()
        ledger_repo.get_entry_by_idempotency = AsyncMock(return_value=None)
        ledger_repo.post_entry = AsyncMock(return_value=_entry(300))
        audit_repo = AsyncMock()

        service = _service(wallet_repo, ledger_repo, audit_repo)
        updated, _ = await service.adjust_balance(
            admin=_admin(),
            user_id=USER_ID,
            amount=-300,
            reason="chargeback correction",
            idempotency_key="adj-key-2",
        )

        wallet_repo.debit.assert_awaited_once_with(USER_ID, 300, "adj-key-2")
        assert updated.balance == 700
        args, _kwargs = ledger_repo.post_entry.call_args
        assert args[1] == 300  # positive magnitude in the ledger

    async def test_insufficient_funds_propagates_without_ledger_or_audit(self) -> None:
        wallet_repo = AsyncMock()
        wallet_repo.get = AsyncMock(return_value=_wallet(100))
        wallet_repo.debit = AsyncMock(
            side_effect=InsufficientBalanceError("balance 100 < required 2000")
        )
        ledger_repo = AsyncMock()
        ledger_repo.get_entry_by_idempotency = AsyncMock(return_value=None)
        ledger_repo.post_entry = AsyncMock()
        audit_repo = AsyncMock()

        service = _service(wallet_repo, ledger_repo, audit_repo)
        with pytest.raises(InsufficientBalanceError):
            await service.adjust_balance(
                admin=_admin(),
                user_id=USER_ID,
                amount=-2000,
                reason="correction",
                idempotency_key="adj-key-3",
            )
        ledger_repo.post_entry.assert_not_awaited()
        audit_repo.append.assert_not_awaited()


class TestIdempotency:
    async def test_replayed_key_returns_existing_outcome(self) -> None:
        """Retrying the same key must not apply the change twice."""
        existing = _entry(500)
        wallet_repo = AsyncMock()
        wallet_repo.get = AsyncMock(return_value=_wallet(1500))  # already adjusted
        wallet_repo.add_funds = AsyncMock()
        ledger_repo = AsyncMock()
        ledger_repo.get_entry_by_idempotency = AsyncMock(return_value=existing)
        ledger_repo.post_entry = AsyncMock()
        audit_repo = AsyncMock()

        service = _service(wallet_repo, ledger_repo, audit_repo)
        wallet, entry = await service.adjust_balance(
            admin=_admin(),
            user_id=USER_ID,
            amount=500,
            reason="goodwill credit",
            idempotency_key="adj-key-1",
        )

        assert entry is existing
        assert wallet.balance == 1500  # untouched: replay returns prior state
        wallet_repo.add_funds.assert_not_awaited()
        ledger_repo.post_entry.assert_not_awaited()
        audit_repo.append.assert_not_awaited()


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
