"""Payment reconciliation job (M09-007).

Stuck sessions (PENDING past a grace window) are rechecked safely: each
session is verified against the gateway (``verify_with_amount`` for
ZarinPal), then the result flows through the SAME replay-safe
``PaymentWebhookService.process_callback`` that the webhook uses — so a
recheck can never produce a second deposit:

- SUCCEEDED verify -> ``succeeded`` callback -> credited exactly once
  (deterministic ledger key ``deposit-{gateway}-{external_id}``).
- FAILED verify -> ``failed`` callback -> session marked failed.
- Still-pending verify -> session left untouched for the next run.
- Sessions without an external id (never reached the gateway) are skipped.
- Per-session failures never break the run; one SYSTEM audit event per run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    checked: int = 0
    credited: int = 0
    marked_failed: int = 0
    still_pending: int = 0
    skipped: int = 0
    errors: int = 0

    def render(self) -> str:
        return (
            f"reconciled checked={self.checked} credited={self.credited} "
            f"failed={self.marked_failed} pending={self.still_pending} "
            f"skipped={self.skipped} errors={self.errors}"
        )


@dataclass
class PaymentReconciliationService:
    """Recheck stuck PENDING sessions against the gateway."""

    payments_repo: Any
    webhook_service: Any
    gateway: Any
    audit_repo: Any | None = None
    stale_after: timedelta = field(default_factory=lambda: timedelta(minutes=15))

    async def run(self, *, now: datetime | None = None) -> ReconcileReport:
        from cloud_platform.modules.payments.domain import PaymentSessionStatus

        moment = now or datetime.now(UTC)
        cutoff = moment - self.stale_after
        sessions: list[Any] = await self._stuck_sessions(cutoff)
        checked = credited = marked_failed = still_pending = skipped = errors = 0
        for session in sessions:
            external_id: str | None = session.gateway_payment_id
            session_id: Any = session.id
            if not external_id:
                skipped += 1
                continue
            checked += 1
            try:
                intent: Any = await self.gateway.verify_with_amount(
                    external_id, session.amount_minor
                )
            except Exception:
                logger.warning("reconcile verify failed for %s", session_id, exc_info=True)
                errors += 1
                continue
            try:
                status = getattr(intent, "status", None)
                status_value = getattr(status, "value", status)
                succeeded = PaymentSessionStatus.SUCCEEDED.value
                failed = PaymentSessionStatus.FAILED.value
                if status_value == succeeded or str(status_value) == "succeeded":
                    outcome: Any = await self.webhook_service.process_callback(
                        gateway_key=session.gateway_key,
                        external_id=external_id,
                        status="succeeded",
                    )
                    action = getattr(getattr(outcome, "action", None), "value", "")
                    if action in ("credited", "late_credit"):
                        credited += 1
                    else:
                        still_pending += 1
                elif status_value == failed or str(status_value) == "failed":
                    await self.webhook_service.process_callback(
                        gateway_key=session.gateway_key,
                        external_id=external_id,
                        status="failed",
                    )
                    marked_failed += 1
                else:
                    still_pending += 1
            except Exception:
                logger.warning("reconcile credit failed for %s", session_id, exc_info=True)
                errors += 1
        if self.audit_repo is not None:
            try:
                await self.audit_repo.append(
                    actor_type="system",
                    actor_id=UUID(int=0),
                    action="payments.reconcile",
                    resource_type="payment",
                    resource_id=None,
                    metadata={
                        "checked": checked,
                        "credited": credited,
                        "failed": marked_failed,
                    },
                )
            except Exception:
                logger.warning("reconcile audit append failed", exc_info=True)
        return ReconcileReport(
            checked=checked,
            credited=credited,
            marked_failed=marked_failed,
            still_pending=still_pending,
            skipped=skipped,
            errors=errors,
        )

    async def _stuck_sessions(self, cutoff: datetime) -> list[Any]:
        repo: Any = self.payments_repo
        if hasattr(repo, "list_stuck_pending"):
            result: list[Any] = await repo.list_stuck_pending(cutoff)
            return result
        if hasattr(repo, "list_pending"):
            sessions: list[Any] = await repo.list_pending()
            out: list[Any] = []
            for item in sessions:
                created = getattr(item, "created_at", None)
                if created is None or created <= cutoff:
                    out.append(item)
            return out
        return []
