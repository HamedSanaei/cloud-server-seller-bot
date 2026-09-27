"""Privileged, atomic IRT wallet credit from the Telegram admin console."""

from __future__ import annotations

from dataclasses import dataclass

from cloud_platform.modules.users.domain import PermissionDeniedError, UserNotFound
from cloud_platform.modules.users.repository import SqlAlchemyUserRepository
from cloud_platform.modules.wallet.domain import LedgerEntryType
from cloud_platform.modules.wallet.repository import SqlAlchemyWalletRepository

SUPERADMIN_TELEGRAM_ID = 85758085


@dataclass(frozen=True, slots=True)
class AdminWalletCreditResult:
    target_telegram_id: int
    amount_toman: int
    balance_toman: int
    applied: bool


class AdminWalletService:
    """Credit an existing IRT wallet, with authorization inside the service."""

    def __init__(
        self,
        user_repo: SqlAlchemyUserRepository,
        wallet_repo: SqlAlchemyWalletRepository,
    ) -> None:
        self._users = user_repo
        self._wallets = wallet_repo

    async def credit_admin_toman(
        self,
        actor_telegram_id: int,
        target_telegram_id: int,
        amount_toman: int,
        idempotency_key: str,
    ) -> AdminWalletCreditResult:
        if type(actor_telegram_id) is not int or actor_telegram_id != SUPERADMIN_TELEGRAM_ID:
            raise PermissionDeniedError("only the Telegram superadmin may credit wallets")
        if type(target_telegram_id) is not int or target_telegram_id <= 0:
            raise ValueError("target_telegram_id must be a positive integer")
        if type(amount_toman) is not int or amount_toman <= 0:
            raise ValueError("amount_toman must be a positive integer")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key must be a nonempty string")
        if idempotency_key != idempotency_key.strip():
            raise ValueError("idempotency_key must not have surrounding whitespace")

        user = await self._users.get_by_telegram_user_id(target_telegram_id)
        if user is None or user.id is None:
            raise UserNotFound(f"Telegram user {target_telegram_id} not found")

        # adjust() locks the wallet and commits balance + immutable ledger in
        # ONE transaction; a duplicate key with different facts fails closed.
        # Checking currency there (under the same lock) prevents a concurrent
        # currency change between user lookup and the credit.
        wallet, applied = await self._wallets.adjust(
            user.id,
            amount_toman,
            idempotency_key,
            entry_type=LedgerEntryType.ADJUSTMENT,
            reference_type="admin_telegram_credit",
            description=(
                f"Telegram admin {actor_telegram_id} credited Telegram user {target_telegram_id}"
            ),
            expected_currency="IRT",
        )
        return AdminWalletCreditResult(
            target_telegram_id=target_telegram_id,
            amount_toman=amount_toman,
            balance_toman=wallet.balance,
            applied=applied,
        )
