"""Application service turning gateway webhook callbacks into wallet deposits.

Replay-safety (M09-003) — a duplicated callback can never produce a second
deposit because:

- the ``(gateway_key, gateway_payment_id)`` pair is UNIQUE at the database
  level, so a replay cannot create a second session;
- the deposit ledger entry uses the deterministic key
  ``deposit-{gateway_key}-{external_id}``, so the append-only ledger itself
  deduplicates concurrent or retried credits;
- a session already marked credited short-circuits before any wallet I/O.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from cloud_platform.modules.payments.domain import (
    InvalidPaymentSessionTransition,
    PaymentSession,
    PaymentSessionRepository,
    PaymentSessionStatus,
)
from cloud_platform.modules.wallet.domain import (
    DuplicateIdempotencyError,
    LedgerEntryType,
    LedgerRepository,
    WalletRepository,
)

logger = logging.getLogger(__name__)


class WebhookAction(StrEnum):
    """What the webhook handler did with the callback."""

    CREDITED = "credited"  # first success: session bound + deposit posted
    LATE_CREDIT = "late_credit"  # session was succeeded but the deposit was missing
    DUPLICATE_IGNORED = "duplicate_ignored"  # replayed callback, nothing changed
    FAILED_RECORDED = "failed_recorded"  # gateway reported a failure


@dataclass(frozen=True, slots=True)
class WebhookOutcome:
    action: WebhookAction
    session: PaymentSession


class PaymentWebhookService:
    """Processes signed (signature checked upstream) gateway callbacks."""

    def __init__(
        self,
        payments_repo: PaymentSessionRepository,
        wallet_repo: WalletRepository,
        ledger_repo: LedgerRepository,
    ) -> None:
        self._payments = payments_repo
        self._wallet = wallet_repo
        self._ledger = ledger_repo

    async def process_callback(
        self,
        *,
        gateway_key: str,
        external_id: str,
        status: str,
        user_id: UUID | None = None,
        amount_minor: int | None = None,
        currency: str | None = None,
    ) -> WebhookOutcome:
        """Apply one gateway callback to the payment session and wallet.

        ``user_id``/``amount_minor``/``currency`` are required only when no
        session exists for the external id yet (the callback must then be
        self-describing).
        """
        if not gateway_key or not gateway_key.strip():
            raise ValueError("gateway_key must not be empty")
        if not external_id or not external_id.strip():
            raise ValueError("external payment id must not be empty")
        normalized = status.strip().lower()
        if normalized not in {"succeeded", "failed"}:
            raise ValueError("callback status must be 'succeeded' or 'failed'")

        session = await self._payments.get_by_external_id(gateway_key, external_id)
        if session is None:
            if user_id is None or amount_minor is None or currency is None:
                raise ValueError("unknown payment requires user_id, amount_minor and currency")
            session = PaymentSession(
                user_id=user_id,
                gateway_key=gateway_key,
                amount_minor=amount_minor,
                currency=currency,
                idempotency_key=f"webhook-{gateway_key}-{external_id}",
                gateway_payment_id=external_id,
            )
            session = await self._payments.create(session)
            assert session.id is not None  # persisted sessions carry an id

        if normalized == "failed":
            if session.status is PaymentSessionStatus.PENDING:
                saved = await self._payments.save(
                    session.mark_failed(gateway_payment_id=external_id)
                )
                return WebhookOutcome(WebhookAction.FAILED_RECORDED, saved)
            if session.status is PaymentSessionStatus.FAILED:
                return WebhookOutcome(WebhookAction.DUPLICATE_IGNORED, session)
            raise InvalidPaymentSessionTransition(
                f"payment session {session.id} is {session.status.value}; cannot record failed"
            )

        # --- succeeded ---
        if session.status is PaymentSessionStatus.SUCCEEDED:
            if session.credited_at is not None:
                logger.info(
                    "duplicate success callback for %s/%s; ignoring",
                    gateway_key,
                    external_id,
                )
                return WebhookOutcome(WebhookAction.DUPLICATE_IGNORED, session)
            credited = await self._credit(session)
            return WebhookOutcome(WebhookAction.LATE_CREDIT, credited)

        credited = await self._credit(session.mark_succeeded(gateway_payment_id=external_id))
        return WebhookOutcome(WebhookAction.CREDITED, credited)

    async def _credit(self, session: PaymentSession) -> PaymentSession:
        """Post the deposit exactly once for this session, then mark credited."""
        assert session.id is not None
        assert session.gateway_payment_id is not None
        deposit_key = f"deposit-{session.gateway_key}-{session.gateway_payment_id}"

        existing = await self._ledger.get_entry_by_idempotency(session.id, deposit_key)
        if existing is None:
            await self._wallet.add_funds(session.user_id, session.amount_minor, deposit_key)
            ext_id = session.gateway_payment_id
            try:
                await self._ledger.post_entry(
                    session.id,
                    session.amount_minor,
                    session.currency,
                    LedgerEntryType.DEPOSIT,
                    deposit_key,
                    reference_type="payment",
                    reference_id=ext_id,
                    description=f"gateway deposit {session.gateway_key}/{ext_id}",
                )
            except DuplicateIdempotencyError:
                # A concurrent duplicate callback already posted this deposit;
                # the ledger's unique constraint is the arbiter.
                logger.warning("deposit %s was already posted concurrently", deposit_key)

        credited = session.mark_credited(at=datetime.now(UTC))
        return await self._payments.save(credited)
