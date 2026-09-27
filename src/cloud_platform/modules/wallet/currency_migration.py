"""Explicit operator-only USD -> IRT wallet conversion.

Run with gateways, provisioning, billing and hold creation quiesced; obtain a
fresh plan, inspect its full FX snapshot and converted amount, then explicitly
apply *that same plan*. The repository is the sole transaction boundary and
must recheck eligibility under its wallet row lock. Never automate this from
startup or a payment callback.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from cloud_platform.modules.fx.domain import ConversionSnapshot, FxPurpose
from cloud_platform.modules.wallet.domain import (
    Wallet,
    WalletCurrencyMigrationError,
    WalletCurrencyMigrationPlan,
    WalletRepository,
)


class MigrationFxResolver(Protocol):
    async def snapshot(
        self, amount_minor: int, source_currency: str, target_currency: str, purpose: FxPurpose
    ) -> ConversionSnapshot: ...


class WalletCurrencyMigrationService:
    """Prepare a reviewable FX snapshot; commit only on explicit operator call."""

    def __init__(self, wallets: WalletRepository, fx: MigrationFxResolver) -> None:
        self._wallets = wallets
        self._fx = fx

    async def plan(
        self,
        *,
        user_id: UUID,
        operator_id: UUID,
        migration_id: UUID,
        reason: str,
    ) -> WalletCurrencyMigrationPlan:
        """Preview a tentative liquidation at today's live USD/USDT sell quote.

        A quote can expire or wallet state can change before application; the
        repository checks both again in the same transaction as the mutation.
        The migration_id must be retained for precise operator replay.
        """
        wallet = await self._wallets.get(user_id)
        if (
            wallet is None
            or wallet.id is None
            or wallet.user_id != user_id
            or wallet.currency != "USD"
        ):
            raise WalletCurrencyMigrationError("migration requires an existing USD wallet")
        if wallet.balance < 0:
            raise WalletCurrencyMigrationError("cannot migrate a negative USD balance")
        snapshot = await self._fx.snapshot(wallet.balance, "USD", "IRT", FxPurpose.LIQUIDATION)
        if not isinstance(snapshot, ConversionSnapshot):
            raise WalletCurrencyMigrationError("FX resolver did not return an audited snapshot")
        now = datetime.now(UTC)
        if snapshot.observed_at > now or snapshot.expires_at is None or snapshot.expires_at <= now:
            raise WalletCurrencyMigrationError("migration requires an unexpired liquidation quote")
        return WalletCurrencyMigrationPlan(
            id=migration_id,
            user_id=user_id,
            wallet_id=wallet.id,
            source_balance_minor=wallet.balance,
            snapshot=snapshot,
            operator_id=operator_id,
            reason=reason,
        )

    async def apply(self, plan: WalletCurrencyMigrationPlan) -> tuple[Wallet, bool]:
        """Commit the reviewed plan or replay the *exact* previous migration.

        The durable repository owns all row locks, pending-obligation guards,
        FX expiry checks, ledger writes and wallet transition atomically. A
        consumed migration ID with different facts must raise, not reconvert.
        """
        if not isinstance(plan, WalletCurrencyMigrationPlan):
            raise WalletCurrencyMigrationError("apply requires the reviewed migration plan")
        return await self._wallets.apply_currency_migration(plan)
