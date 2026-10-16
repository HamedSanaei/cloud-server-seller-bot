"""Explicit zero-value cutover and cancellation receipt; never a bulk money rewrite."""

from __future__ import annotations

import json
from hashlib import sha256
from uuid import UUID

from sqlalchemy import or_, select, text

from cloud_platform.db import base as db
from cloud_platform.modules.users.superadmin_repository import authorize_superadmin_transaction
from cloud_platform.modules.wallet.domain import WalletCurrencyMigrationError


def invoice_facts(invoice) -> dict:
    """Retain the exact original financial identity, not just its current status."""
    names = (
        "gateway_key",
        "gateway_payment_id",
        "provider_payment_id",
        "amount_minor",
        "currency",
        "credit_amount_minor",
        "credit_currency",
        "idempotency_key",
        "fx_source",
        "fx_rate",
        "fx_path",
        "fx_observed_at",
        "fx_proxy",
        "fx_proxy_asset",
    )
    # Datetimes/Decimal are serialized as exact text, never float.
    return json.loads(json.dumps({name: getattr(invoice, name) for name in names}, default=str))


def invoice_fingerprint(invoice) -> str:
    return sha256(json.dumps(invoice_facts(invoice), sort_keys=True).encode()).hexdigest()


class SqlAlchemyEmptyTomanCutover:
    """Operator-only atomic command, absent from public API and bot onboarding.

    A zero wallet with no monetary history needs no FX rate. A pending invoice
    may be cancelled only by this explicit command with its reviewed fingerprint.
    FAILED is the existing terminal non-crediting state; the immutable audit
    distinguishes an owner's cancellation from a provider-reported failure.
    """

    def __init__(self, session_factory, *, configured_ids):
        self._sessions, self._ids = session_factory, tuple(configured_ids)

    async def apply(
        self,
        *,
        user_id: UUID,
        wallet_id: UUID,
        invoice_id: UUID,
        actor_id: UUID,
        expected_invoice_fingerprint: str,
        reason: str,
        operation_key: str,
    ) -> bool:
        if (
            not all(isinstance(value, UUID) for value in (user_id, wallet_id, invoice_id, actor_id))
            or not isinstance(reason, str)
            or not 1 <= len(reason.strip()) <= 500
            or not isinstance(operation_key, str)
            or not 1 <= len(operation_key) <= 128
            or len(expected_invoice_fingerprint) != 64
        ):
            raise WalletCurrencyMigrationError("explicit reviewed cutover facts are required")
        facts = {
            "operation_key": operation_key,
            "user_id": str(user_id),
            "wallet_id": str(wallet_id),
            "invoice_id": str(invoice_id),
            "invoice_fingerprint": expected_invoice_fingerprint,
            "source_currency": "USD",
            "target_currency": "IRT",
            "balance_minor": 0,
        }
        async with self._sessions() as session, session.begin():
            await authorize_superadmin_transaction(session, actor_id, self._ids)
            key = int.from_bytes(sha256(operation_key.encode()).digest()[:8], "big", signed=True)
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
            wallet = (
                await session.execute(
                    select(db.Wallet).where(db.Wallet.id == wallet_id).with_for_update()
                )
            ).scalar_one_or_none()
            # NOWAIT avoids a wallet/payment lock-order deadlock with any
            # in-flight settlement. Contention fails closed and rolls back.
            invoice = (
                await session.execute(
                    select(db.PaymentSession)
                    .where(db.PaymentSession.id == invoice_id)
                    .with_for_update(nowait=True)
                )
            ).scalar_one_or_none()
            if (
                wallet is None
                or invoice is None
                or wallet.user_id != user_id
                or invoice.user_id != user_id
                or invoice_fingerprint(invoice) != expected_invoice_fingerprint
            ):
                raise WalletCurrencyMigrationError("wallet/invoice ownership or facts changed")
            prior = (
                await session.execute(
                    select(db.AuditEvent).where(
                        db.AuditEvent.action == "wallet.empty_toman_cutover",
                        db.AuditEvent.event_metadata["operation_key"].astext == operation_key,
                    )
                )
            ).scalar_one_or_none()
            if prior is not None:
                if (
                    prior.actor_id != actor_id
                    or prior.reason != reason.strip()
                    or prior.event_metadata
                    != {**facts, "cancelled_invoice": invoice_facts(invoice)}
                    or wallet.currency != "IRT"
                    or invoice.status != "failed"
                    or invoice.credited_at is not None
                ):
                    raise WalletCurrencyMigrationError("cutover replay facts changed")
                return False
            if (
                wallet.currency != "USD"
                or wallet.balance != 0
                or invoice.status != "pending"
                or invoice.credited_at is not None
                or (invoice.credit_currency or invoice.currency) != "USD"
            ):
                raise WalletCurrencyMigrationError(
                    "only a zero USD wallet and unpaid pending invoice qualify"
                )
            guards = (
                select(db.LedgerEntry.id).where(db.LedgerEntry.wallet_id == wallet_id),
                select(db.Hold.id).where(db.Hold.wallet_id == wallet_id),
                select(db.Server.id).where(
                    db.Server.user_id == user_id,
                    db.Server.funding_source == "customer_wallet",
                    db.Server.state != "deleted",
                ),
                select(db.ProviderOrder.id)
                .join(db.Server, db.Server.id == db.ProviderOrder.server_id)
                .where(
                    db.Server.user_id == user_id,
                    db.ProviderOrder.funding_source == "customer_wallet",
                    db.ProviderOrder.status.not_in(("failed", "cancelled")),
                ),
                select(db.PaymentSession.id).where(
                    db.PaymentSession.user_id == user_id,
                    db.PaymentSession.id != invoice_id,
                    or_(
                        db.PaymentSession.status.in_(("pending", "manual_review")),
                        (db.PaymentSession.status == "succeeded")
                        & db.PaymentSession.credited_at.is_(None),
                    ),
                ),
            )
            for guard in guards:
                if (await session.execute(guard.limit(1))).first() is not None:
                    raise WalletCurrencyMigrationError(
                        "monetary history or another obligation blocks empty cutover"
                    )
            invoice.status = "failed"
            wallet.currency = "IRT"
            # No balance change, fake deposit, FX quote, or historical rewrite.
            session.add(
                db.AuditEvent(
                    actor_type="admin",
                    actor_id=actor_id,
                    action="wallet.empty_toman_cutover",
                    resource_type="wallet",
                    resource_id=str(wallet_id),
                    reason=reason.strip(),
                    event_metadata={**facts, "cancelled_invoice": invoice_facts(invoice)},
                )
            )
            await session.flush()
            return True
