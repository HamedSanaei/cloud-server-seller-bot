"""Recover historical capacity refusals into durable account knowledge.

The first capacity release could only LEARN from a refusal it observed itself,
so an account that had already refused a create *before* the feature existed
was still treated as eligible: the very next customer order was sent into a
Sales Organization that was already at its limit and was refused again.

This service closes that hole without a one-off SQL statement and without ever
calling the provider:

1. a read-only source (:class:`HistoricalCapacityEvidenceSource`) walks failed
   create operations and yields only failures the AUDITED classifier accepts as
   a capacity refusal (``errorCode=PC-2031`` / the documented
   "Customer limit reached" text) — unrelated HTTP 400s are never reinterpreted
   as capacity;
2. each incident is reconciled through
   :meth:`AccountCapacityRepository.record_historical_evidence`, which is
   idempotent per provider operation, mutates no operation row and stores no
   credential material.

Running it twice, or on three processes at once, therefore appends nothing the
second time. It is safe to run at worker startup and from the operator CLI.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from cloud_platform.modules.provider_capacity.domain import (
    DEFAULT_LIMIT_TTL_SECONDS,
    AccountCapacity,
    AccountCapacityRepository,
    HistoricalCapacityEvidence,
    HistoricalCapacityEvidenceSource,
    validate_limit_ttl,
)

logger = logging.getLogger(__name__)

__all__ = ["CapacityBackfillReport", "CapacityReconciliationService"]


@dataclass(frozen=True, slots=True)
class CapacityBackfillReport:
    """What one reconciliation pass found and what it changed."""

    provider_key: str
    evidence_found: int = 0
    applied: tuple[tuple[str, str], ...] = ()
    """``(credential_account_id, state)`` for each NEWLY applied incident."""
    already_recorded: tuple[str, ...] = ()
    """Provider operations already reconciled by an earlier pass."""
    failed: tuple[str, ...] = ()
    errors: tuple[str, ...] = field(default=())

    @property
    def applied_count(self) -> int:
        return len(self.applied)

    def summary(self) -> str:
        return (
            f"provider={self.provider_key} evidence={self.evidence_found} "
            f"applied={self.applied_count} already={len(self.already_recorded)} "
            f"failed={len(self.failed)}"
        )


class CapacityReconciliationService:
    """Applies historical capacity evidence to the durable capacity store."""

    def __init__(
        self,
        *,
        capacity_repo: AccountCapacityRepository,
        evidence_source: HistoricalCapacityEvidenceSource,
        ttl_seconds: int = DEFAULT_LIMIT_TTL_SECONDS,
    ) -> None:
        self._capacity = capacity_repo
        self._evidence = evidence_source
        self._ttl_seconds = validate_limit_ttl(ttl_seconds)

    async def run(
        self, provider_key: str = "leaseweb", *, limit: int = 200
    ) -> CapacityBackfillReport:
        """Reconcile every provable historical refusal, exactly once each."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        try:
            evidence = await self._evidence.failed_capacity_evidence(provider_key, limit=limit)
        except Exception as exc:
            logger.warning(
                "capacity reconciliation could not read historical evidence (%s)",
                type(exc).__name__,
                exc_info=True,
            )
            return CapacityBackfillReport(
                provider_key=provider_key,
                errors=(f"evidence source: {type(exc).__name__}",),
            )
        applied: list[tuple[str, str]] = []
        already: list[str] = []
        failed: list[str] = []
        for item in evidence:
            try:
                record = await self._record(item)
            except Exception as exc:
                logger.warning(
                    "capacity reconciliation failed for operation %s (%s)",
                    item.source_ref,
                    type(exc).__name__,
                    exc_info=True,
                )
                failed.append(item.source_ref)
                continue
            if record is None:
                already.append(item.source_ref)
                continue
            applied.append((item.credential_account_id, record.state.value))
            logger.warning(
                "capacity reconciled from history: provider=%s account=%s cumulative=%d source=%s",
                item.provider_key,
                item.credential_account_id,
                record.observations,
                item.source_ref,
            )
        report = CapacityBackfillReport(
            provider_key=provider_key,
            evidence_found=len(evidence),
            applied=tuple(applied),
            already_recorded=tuple(already),
            failed=tuple(failed),
        )
        logger.info("capacity reconciliation: %s", report.summary())
        return report

    async def _record(self, evidence: HistoricalCapacityEvidence) -> AccountCapacity | None:
        return await self._capacity.record_historical_evidence(
            evidence, ttl_seconds=self._ttl_seconds
        )
