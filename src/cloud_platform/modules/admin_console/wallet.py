"""Owner-bound Toman adjustments with frozen FX and immutable ledger receipts."""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID

from cloud_platform.core.session_store import BotSessionStore
from cloud_platform.modules.fx.domain import ConversionSnapshot, FxPurpose, FxUnavailableError
from cloud_platform.modules.fx.service import FxResolver
from cloud_platform.modules.users.domain import PermissionDeniedError, Role, User, UserRepository
from cloud_platform.modules.users.superadmins import require_superadmin, validate_telegram_user_ids
from cloud_platform.modules.wallet.domain import LedgerEntry, Wallet, WalletRepository
from cloud_platform.modules.wallet.service import WalletAdminService


@dataclass(frozen=True, slots=True)
class AdminWalletConfirmation:
    nonce: str
    telegram_user_id: int
    amount_toman: int
    amount_minor: int
    currency: str
    reason: str
    snapshot: ConversionSnapshot


class AdminWalletAdjustmentService:
    """Accept signed whole Toman; never resolve FX again when applying."""

    def __init__(
        self,
        users: UserRepository,
        wallets: WalletRepository,
        wallet_admin: WalletAdminService,
        store: BotSessionStore,
        *,
        fx: FxResolver | None = None,
        configured_ids: Iterable[int] = (),
        ttl_seconds: int = 900,
    ) -> None:
        self._users = users
        self._wallets = wallets
        self._admin = wallet_admin
        self._store = store
        self._fx = fx
        self._ids = validate_telegram_user_ids(tuple(configured_ids))
        self._ttl = ttl_seconds

    async def _authorize(self, actor: User) -> User:
        admin = require_superadmin(actor, self._ids)
        if admin.id is None:
            raise PermissionDeniedError("persisted administrator required")
        fresh = require_superadmin(await self._users.get(admin.id), self._ids)
        if fresh.id != admin.id or fresh.telegram_user_id != admin.telegram_user_id:
            raise PermissionDeniedError("administrator identity changed")
        return fresh

    async def target(self, actor: User, telegram_user_id: int) -> tuple[User, Wallet]:
        admin = await self._authorize(actor)
        target_id = validate_telegram_user_ids((telegram_user_id,))[0]
        target = await self._users.get_by_telegram_user_id(target_id)
        if target is None or target.id is None or target.role != Role.USER or target.id == admin.id:
            raise ValueError("customer target required")
        wallet = await self._wallets.get(target.id)
        if wallet is None or wallet.id is None:
            raise ValueError("target wallet is unavailable")
        return target, wallet

    async def clear(self, actor: User) -> None:
        admin = await self._authorize(actor)
        active = await self._store.get("admin-adjust-active", str(admin.id))
        if active is not None:
            await self._store.delete("admin-adjust-confirm", f"{admin.id}:{active['nonce']}")
        await self._store.delete("admin-adjust-active", str(admin.id))

    async def prepare(
        self, actor: User, telegram_user_id: int, amount_toman: int, reason: str
    ) -> AdminWalletConfirmation:
        admin = await self._authorize(actor)
        target, wallet = await self.target(admin, telegram_user_id)
        if (
            type(amount_toman) is not int
            or not -(2**63 - 1) <= amount_toman <= 2**63 - 1
            or amount_toman == 0
        ):
            raise ValueError("a non-zero signed whole Toman amount is required")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
            raise ValueError("a reason of 1 to 500 characters is required")
        # Credit rounding never grants more than the authorized Toman value;
        # debit rounding never collects less than the authorized charge.
        purpose = FxPurpose.LIQUIDATION if amount_toman > 0 else FxPurpose.CHARGE
        if wallet.currency == "IRT":
            now = datetime.now(UTC)
            snapshot = ConversionSnapshot(
                abs(amount_toman),
                "IRT",
                abs(amount_toman),
                "IRT",
                Decimal(1),
                purpose,
                "identity",
                "IRT->IRT",
                now,
                expires_at=now + timedelta(seconds=self._ttl),
            )
        else:
            if self._fx is None:
                raise FxUnavailableError("FX is required for this wallet currency")
            snapshot = await self._fx.snapshot(abs(amount_toman), "IRT", wallet.currency, purpose)
        if snapshot.stale or snapshot.target_amount_minor <= 0:
            raise ValueError("a fresh non-zero conversion is required")
        await self.clear(admin)
        nonce = secrets.token_urlsafe(6)
        amount = snapshot.target_amount_minor * (1 if amount_toman > 0 else -1)
        record = {
            "target_id": str(target.id),
            "telegram_user_id": telegram_user_id,
            "amount_toman": amount_toman,
            "amount_minor": amount,
            "reason": reason.strip(),
            "snapshot": snapshot.to_dict(),
        }
        await self._store.put(
            "admin-adjust-confirm", f"{admin.id}:{nonce}", record, ttl_seconds=self._ttl
        )
        await self._store.put(
            "admin-adjust-active", str(admin.id), {"nonce": nonce}, ttl_seconds=self._ttl
        )
        return AdminWalletConfirmation(
            nonce, telegram_user_id, amount_toman, amount, wallet.currency, reason.strip(), snapshot
        )

    async def apply(self, actor: User, nonce: str) -> tuple[Wallet, LedgerEntry]:
        admin = await self._authorize(actor)
        active = await self._store.get("admin-adjust-active", str(admin.id))
        if active is None or active.get("nonce") != nonce:
            raise ValueError("adjustment confirmation expired")
        record = await self._store.get("admin-adjust-confirm", f"{admin.id}:{nonce}")
        if record is None:
            raise ValueError("adjustment confirmation expired")
        target, wallet = await self.target(admin, record["telegram_user_id"])
        snapshot = ConversionSnapshot.from_dict(record["snapshot"])
        amount_toman = record["amount_toman"]
        amount = record["amount_minor"]
        if (
            str(target.id) != record["target_id"]
            or wallet.currency != snapshot.target_currency
            or type(amount_toman) is not int
            or amount_toman == 0
            or snapshot.source_currency != "IRT"
            or snapshot.source_amount_minor != abs(amount_toman)
            or type(amount) is not int
            or abs(amount) != snapshot.target_amount_minor
            or (amount > 0) != (amount_toman > 0)
            or snapshot.stale
            or snapshot.purpose != (FxPurpose.LIQUIDATION if amount > 0 else FxPurpose.CHARGE)
        ):
            raise ValueError("adjustment facts changed")
        # Both operator reason and the exact frozen conversion survive in the
        # append-only ledger description and the transaction's audit reason.
        reason = record["reason"] + "\nFX: " + json.dumps(snapshot.to_dict(), sort_keys=True)
        return await self._admin.adjust_balance(
            admin=admin,
            user_id=cast(UUID, target.id),
            amount=amount,
            reason=reason,
            idempotency_key=f"admin-adjust:{admin.id}:{nonce}",
            expected_currency=snapshot.target_currency,
        )
