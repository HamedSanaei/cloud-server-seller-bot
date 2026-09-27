"""AtlasPay reconciliation: trust only provider inquiry, never Telegram input.

This is polled by the worker; no public callback or webhook is needed. The
existing deposit service locks wallet balance and appends one deterministic
ledger entry for every verified order, including concurrent/replayed polls.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from cloud_platform.modules.audit.domain import ActorType, AuditEvent
from cloud_platform.modules.payments.reconcile import ReconcileReport, _reconcile_audit_event
from cloud_platform.providers.base import PaymentStatus

logger = logging.getLogger(__name__)


async def reconcile_atlaspay_pending(
    *,
    payments_repo: Any,
    webhook_service: Any,
    gateway: Any,
    stale_after: timedelta = timedelta(seconds=30),
    now: datetime | None = None,
    limit: int = 50,
    audit_repo: Any | None = None,
) -> ReconcileReport:
    """Poll older pending AtlasPay orders, validating every pinned identity."""
    moment = now or datetime.now(UTC)
    try:
        sessions = await payments_repo.list_pending_before("atlaspay", moment - stale_after, limit)
    except Exception:
        logger.warning("atlaspay reconciliation scan failed", exc_info=True)
        return ReconcileReport(errors=1)
    checked = credited = failed = pending = skipped = errors = 0
    for session in sessions:
        order_id = session.gateway_payment_id
        if not order_id:
            skipped += 1
            continue
        checked += 1
        try:
            intent = await gateway.verify_payment(order_id)
        except Exception:
            logger.warning("atlaspay inquiry failed for session %s", session.id)
            errors += 1
            continue
        try:
            metadata = intent.metadata
            identity_matches = (
                intent.gateway_payment_id == order_id
                and metadata.get("merchant_order_ref") == session.idempotency_key
                and metadata.get("tracking_code") == session.tracking_code
                and isinstance(intent.amount_minor, int)
                and not isinstance(intent.amount_minor, bool)
                and intent.amount_minor == session.amount_minor
                and intent.currency == session.currency == "IRT"
                and session.effective_credit_currency == "IRT"
                and session.effective_credit_amount > 0
                and session.effective_credit_amount <= session.amount_minor
            )
            if not identity_matches:
                logger.warning(
                    "atlaspay inquiry identity or amount mismatch for session %s", session.id
                )
                errors += 1
                continue
            if intent.status is PaymentStatus.SUCCEEDED:
                outcome = await webhook_service.process_callback(
                    gateway_key="atlaspay", external_id=order_id, status="succeeded"
                )
                if outcome.action.value in ("credited", "late_credit"):
                    credited += 1
                else:
                    pending += 1
            elif (
                metadata.get("provider_status") in ("confirmed", "settled")
                and metadata.get("manual_delivery") == "true"
            ):
                await payments_repo.save(session.mark_manual_review())
                logger.warning("atlaspay order needs manual review for session %s", session.id)
                if audit_repo is not None:
                    try:
                        await audit_repo.append(
                            AuditEvent(
                                actor_type=ActorType.SYSTEM,
                                actor_id=UUID(int=0),
                                action="payments.atlaspay.manual_review",
                                resource_type="payment",
                                resource_id=str(session.id),
                                reason="Provider confirmed underpayment; operator review required",
                            )
                        )
                    except Exception:
                        logger.warning(
                            "atlaspay manual review audit failed for session %s", session.id
                        )
                pending += 1
            elif intent.status is PaymentStatus.FAILED:
                await webhook_service.process_callback(
                    gateway_key="atlaspay", external_id=order_id, status="failed"
                )
                failed += 1
            else:
                pending += 1
        except Exception:
            logger.warning("atlaspay reconciliation processing failed for session %s", session.id)
            errors += 1
    if audit_repo is not None:
        try:
            await audit_repo.append(
                _reconcile_audit_event(checked=checked, credited=credited, marked_failed=failed)
            )
        except Exception:
            logger.warning("atlaspay reconciliation audit failed", exc_info=True)
    return ReconcileReport(
        checked=checked,
        credited=credited,
        marked_failed=failed,
        still_pending=pending,
        skipped=skipped,
        errors=errors,
    )
