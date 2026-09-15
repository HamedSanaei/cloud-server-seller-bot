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
from typing import Any
from uuid import UUID

from cloud_platform.modules.businesslog.domain import BusinessEventSink, emit_safe
from cloud_platform.modules.businesslog.events import (
    recharge_failed_event,
    recharge_succeeded_event,
)
from cloud_platform.modules.payments.domain import (
    InvalidPaymentSessionTransition,
    PaymentSession,
    PaymentSessionRepository,
    PaymentSessionStatus,
    session_credit_amount,
    session_credit_currency,
)
from cloud_platform.modules.wallet.domain import (
    LedgerRepository,
    WalletRepository,
)
from cloud_platform.providers.errors import ProviderError

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
        event_sink: BusinessEventSink | None = None,
        user_repo: object | None = None,
    ) -> None:
        self._payments = payments_repo
        self._wallet = wallet_repo
        self._ledger = ledger_repo
        self._events = event_sink
        self._users = user_repo

    async def _load_user(self, user_id: UUID) -> object | None:
        """Best-effort user lookup for the operator-channel payload."""
        if self._users is None:
            return None
        try:
            found: object = await self._users.get(user_id)  # type: ignore[attr-defined]
        except Exception:
            logger.warning("user lookup for recharge log failed", exc_info=True)
            return None
        return found

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
                # Operator channel: the gateway reported a failure. Logged
                # only for the FIRST failure transition (a replayed callback
                # hits DUPLICATE_IGNORED below and logs nothing new).
                await emit_safe(
                    self._events,
                    recharge_failed_event(
                        user=await self._load_user(session.user_id),
                        payment_session_id=session.id or external_id,
                        amount_minor=session_credit_amount(session),
                        currency=session_credit_currency(session),
                        gateway=gateway_key,
                        state=PaymentSessionStatus.FAILED.value,
                    ),
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
        """Post the deposit exactly once for this session, then mark credited.

        The wallet credit and its ledger entry are applied ATOMICALLY by
        :meth:`SqlAlchemyWalletRepository.credit_deposit` under a row-locked
        wallet: concurrent duplicates of this callback can never both
        increment the balance, and the append-only ledger keeps exactly one
        entry per deterministic deposit key.

        The credited amount is the FROZEN wallet credit side, never a fresh
        FX conversion: the settlement side was already verified EXACTLY
        against the gateway before this point.
        """
        assert session.id is not None
        assert session.gateway_payment_id is not None
        deposit_key = f"deposit-{session.gateway_key}-{session.gateway_payment_id}"

        wallet, _applied = await self._wallet.credit_deposit(
            session.user_id,
            session_credit_amount(session),
            deposit_key,
            reference=f"{session.gateway_key}/{session.gateway_payment_id}",
        )
        credited = await self._payments.save(session.mark_credited(at=datetime.now(UTC)))
        # Operator channel: emitted only AFTER the wallet was actually
        # credited (the session is persisted as credited above).
        balance_after = getattr(wallet, "balance", None)
        await emit_safe(
            self._events,
            recharge_succeeded_event(
                user=await self._load_user(session.user_id),
                payment_session_id=session.id,
                amount_minor=session_credit_amount(session),
                currency=session_credit_currency(session),
                gateway=session.gateway_key,
                gateway_reference=session.gateway_payment_id,
                balance_after_minor=(balance_after if isinstance(balance_after, int) else None),
            ),
        )
        return credited


class TetraminatorCallbackAction(StrEnum):
    """What the Tetraminator callback handler did (all HTTP 200-safe)."""

    CREDITED = "credited"  # inquiry verified paid + exact amount; deposit posted
    ALREADY_PROCESSED = "already_processed"  # replay or terminal session; no effect
    STILL_PENDING = "still_pending"  # not paid yet (or inquiry transient); retry later
    FAILED_RECORDED = "failed_recorded"  # verified mismatch; session failed, operator alerted


@dataclass(frozen=True, slots=True)
class TetraminatorCallbackOutcome:
    action: TetraminatorCallbackAction
    session: PaymentSession | None


class TetraminatorCallbackService:
    """Verifies unsigned Tetraminator GET callbacks before any wallet effect.

    Tetraminator documents NO webhook signature, so the callback itself is
    UNTRUSTED: it only identifies a local session. Every credit decision
    comes from a server-side ``inquiry`` compared EXACTLY against the stored
    session (gateway key, pinned pay_id, exact Toman amount). The actual
    deposit reuses :class:`PaymentWebhookService`, so replay/concurrent
    callbacks share its idempotency guarantees.
    """

    def __init__(
        self,
        payments_repo: PaymentSessionRepository,
        webhook_service: PaymentWebhookService,
        gateway: Any,
    ) -> None:
        self._payments = payments_repo
        self._webhook = webhook_service
        self._gateway = gateway

    async def process_callback(self, *, ref: str) -> TetraminatorCallbackOutcome:
        """Handle one ``GET /webhooks/payments/tetraminator?ref=...`` call."""
        try:
            session_id = UUID(str(ref or "").strip())
        except (ValueError, AttributeError, TypeError):
            raise ValueError("callback reference must be a session UUID") from None
        session = await self._payments.get(session_id)
        if session is None or session.gateway_key != "tetraminator":
            return TetraminatorCallbackOutcome(TetraminatorCallbackAction.ALREADY_PROCESSED, None)
        if session.status is not PaymentSessionStatus.PENDING:
            # Terminal sessions (SUCCEEDED/FAILED) replay through the same
            # idempotent webhook path: zero new wallet effect, guaranteed.
            if session.status is PaymentSessionStatus.SUCCEEDED:
                await self._webhook.process_callback(
                    gateway_key=session.gateway_key,
                    external_id=session.gateway_payment_id or "",
                    status="succeeded",
                )
            return TetraminatorCallbackOutcome(
                TetraminatorCallbackAction.ALREADY_PROCESSED, session
            )
        if not session.gateway_payment_id:
            # Intent persisted but the invoice call never completed: nothing
            # to inquire about yet; the next refresh or retry will bind it.
            return TetraminatorCallbackOutcome(TetraminatorCallbackAction.STILL_PENDING, session)
        try:
            intent = await self._gateway.verify_payment(session.gateway_payment_id)
        except ProviderError as exc:
            # Transport/auth/rate-limit failures keep the session pending;
            # reconciliation retries. The error is already redacted upstream.
            logger.warning("tetraminator inquiry transient for %s: %s", session.id, exc)
            return TetraminatorCallbackOutcome(TetraminatorCallbackAction.STILL_PENDING, session)
        intent_status = getattr(getattr(intent, "status", None), "value", None) or getattr(
            intent, "status", None
        )
        if intent_status != PaymentSessionStatus.SUCCEEDED.value:
            return TetraminatorCallbackOutcome(TetraminatorCallbackAction.STILL_PENDING, session)
        intent_pay_id = str(getattr(intent, "gateway_payment_id", "") or "")
        intent_amount = getattr(intent, "amount_minor", None)
        intent_currency = str(getattr(intent, "currency", "") or "")
        if (
            intent_pay_id != session.gateway_payment_id
            or not isinstance(intent_amount, int)
            or isinstance(intent_amount, bool)
            or intent_amount != session.amount_minor
            or intent_currency != session.currency
        ):
            # Paid — but NOT for this session. Never credit, never adjust:
            # record an attention-worthy failure for operator review.
            failed = await self._webhook.process_callback(
                gateway_key=session.gateway_key,
                external_id=session.gateway_payment_id,
                status="failed",
            )
            logger.warning(
                "tetraminator inquiry mismatch for %s: provider pay_id/amount "
                "does not match the stored session",
                session.id,
            )
            return TetraminatorCallbackOutcome(
                TetraminatorCallbackAction.FAILED_RECORDED, failed.session
            )
        outcome = await self._webhook.process_callback(
            gateway_key=session.gateway_key,
            external_id=session.gateway_payment_id,
            status="succeeded",
        )
        return TetraminatorCallbackOutcome(TetraminatorCallbackAction.CREDITED, outcome.session)
