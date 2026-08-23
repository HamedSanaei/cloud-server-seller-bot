"""Application services for privileged wallet operations.

Authorization is enforced here in the application layer (not only at the
UI/bot layer): every adjustment requires the ``wallet:adjust`` permission,
a non-empty human-readable reason, and leaves an append-only audit record
linking the action to its actor. All audit writes go through the
:class:`~cloud_platform.modules.audit.service.AuditTrail` facade, which
structurally rejects admin mutations without a reason.
"""

from __future__ import annotations

import logging
from uuid import UUID

from cloud_platform.modules.audit.domain import ActorType, AuditRepository
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.users.domain import Permission, PermissionChecker, User
from cloud_platform.modules.wallet.domain import (
    Hold,
    LedgerEntry,
    LedgerEntryType,
    LedgerRepository,
    Wallet,
    WalletRepository,
)
from cloud_platform.modules.wallet.repository import HoldService

logger = logging.getLogger(__name__)


class WalletAdminService:
    """Administrative balance adjustments with a mandatory audit trail.

    Every adjustment is:
    - authorized in the application layer (``Permission.WALLET_ADJUST``),
    - recorded in the append-only ledger under an idempotency key,
    - audited with the acting admin's identity and a non-empty reason.
    """

    def __init__(
        self,
        wallet_repo: WalletRepository,
        ledger_repo: LedgerRepository,
        audit_repo: AuditRepository,
    ) -> None:
        self._wallet_repo = wallet_repo
        self._ledger_repo = ledger_repo
        self._audit = AuditTrail(audit_repo)

    async def adjust_balance(
        self,
        *,
        admin: User,
        user_id: UUID,
        amount: int,
        reason: str,
        idempotency_key: str,
    ) -> tuple[Wallet, LedgerEntry]:
        """Credit (positive) or debit (negative) a user's wallet.

        Returns the updated wallet and the posted ledger entry. Retrying
        with the same ``idempotency_key`` replays the original outcome
        without applying the balance change twice.

        Raises:
            PermissionDeniedError: If the actor lacks wallet:adjust.
            ValueError: On empty reason, zero amount, or missing wallet.
            InsufficientBalanceError: On debits beyond the balance.
        """
        checker = PermissionChecker(admin)
        checker.require(Permission.WALLET_ADJUST)

        if not reason or not reason.strip():
            raise ValueError("adjustment reason must not be empty")
        if amount == 0:
            raise ValueError("adjustment amount must not be zero")

        wallet = await self._wallet_repo.get(user_id)
        if wallet is None:
            raise ValueError(f"no wallet for user {user_id}")
        assert wallet.id is not None  # persisted wallets carry an id
        wallet_id: UUID = wallet.id

        # Idempotent replay: the same key returns the original outcome.
        existing = await self._ledger_repo.get_entry_by_idempotency(wallet_id, idempotency_key)
        if existing is not None:
            logger.info(
                "adjustment %s already applied for wallet %s; replaying",
                idempotency_key,
                wallet_id,
            )
            return wallet, existing

        if amount > 0:
            updated = await self._wallet_repo.add_funds(user_id, amount, idempotency_key)
        else:
            updated = await self._wallet_repo.debit(user_id, -amount, idempotency_key)

        entry = await self._ledger_repo.post_entry(
            wallet_id,
            abs(amount),
            updated.currency,
            LedgerEntryType.ADJUSTMENT,
            idempotency_key,
            reference_type="admin_adjustment",
            reference_id=str(admin.id) if admin.id is not None else "",
            description=reason,
        )

        await self._audit.record_mutation(
            actor_type=ActorType.ADMIN,
            actor_id=admin.id,
            action="wallet.adjust",
            resource_type="wallet",
            resource_id=str(wallet_id),
            reason=reason,
            metadata={"amount": str(amount), "currency": updated.currency},
        )
        return updated, entry


class HoldAdminService:
    """Administrative forced hold releases with mandatory audit linkage.

    Releasing a hold returns reserved funds to the available balance — a
    financial mutation — so it requires ``Permission.WALLET_ADJUST``, a
    non-empty reason, and produces an audit event linking actor and hold.
    """

    def __init__(self, hold_service: HoldService, audit_repo: AuditRepository) -> None:
        self._hold_service = hold_service
        self._audit = AuditTrail(audit_repo)

    async def force_release(
        self,
        *,
        admin: User,
        wallet_id: UUID,
        hold_id: UUID,
        reason: str,
        idempotency_key: str,
    ) -> Hold:
        """Release a hold on behalf of an administrator.

        Raises:
            PermissionDeniedError: If the actor lacks wallet:adjust.
            ValueError: On empty reason.
            HoldNotFoundError / HoldStateConflictError: From the underlying
                release state machine.
        """
        checker = PermissionChecker(admin)
        checker.require(Permission.WALLET_ADJUST)

        if not reason or not reason.strip():
            raise ValueError("force-release reason must not be empty")

        released = await self._hold_service.release_hold(wallet_id, hold_id, idempotency_key)
        assert released.id is not None  # persisted holds carry an id

        await self._audit.record_mutation(
            actor_type=ActorType.ADMIN,
            actor_id=admin.id,
            action="wallet.force_release",
            resource_type="wallet",
            resource_id=str(wallet_id),
            reason=reason,
            metadata={
                "hold_id": str(released.id),
                "amount": str(released.amount),
                "currency": released.currency,
            },
        )
        return released
