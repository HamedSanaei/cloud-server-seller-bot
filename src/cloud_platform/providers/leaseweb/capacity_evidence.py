"""Recover historical Leaseweb account-capacity refusals (read-only).

Before the capacity subsystem existed, a refusal was recorded only as the
failure text of the provider operation that hit it::

    errorCode=PC-2031; Customer limit reached; correlationId=07376219-...; HTTP 400

Nothing remembered it per ACCOUNT, so after the capacity release the Sales
Organization was considered eligible again and the next customer order was sent
into an account that was already at its limit.

This module turns those historical failures into
:class:`HistoricalCapacityEvidence` records. It is DELIBERATELY narrow:

* it only reads ``operations`` joined to the hourly ``servers`` row that pins
  the credential account — it never writes an operation, a server or an offer;
* it makes NO provider call and stores NO credential material;
* the DECISION is the audited provider classifier
  (:func:`cloud_platform.providers.leaseweb.errors.is_capacity_exhausted`), so
  only ``PC-2031`` / the documented "Customer limit reached" text qualifies.
  A generic HTTP 400 ("Validation Failed", an image or region rejection) is
  never reinterpreted as a capacity refusal;
* the provider operation key is carried as the idempotency anchor, so a second
  reconciliation appends nothing.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import Operation as _OperationModel
from cloud_platform.db.base import Server as _ServerModel
from cloud_platform.modules.operations.domain import OperationStatus, OperationType
from cloud_platform.modules.provider_capacity.domain import HistoricalCapacityEvidence
from cloud_platform.providers.leaseweb.accounts import PROVIDER_KEY
from cloud_platform.providers.leaseweb.errors import is_capacity_exhausted

logger = logging.getLogger(__name__)

__all__ = ["SqlAlchemyHistoricalCapacityEvidenceSource"]

#: The hourly Cloud create intent: server_create on a cloud_server resource.
_OPERATION_TYPE = OperationType.SERVER_CREATE.value
_RESOURCE_TYPE = "cloud_server"
_STATUS_FAILED = OperationStatus.FAILED.value

#: Cheap SQL pre-filter; the audited classifier below is the actual decision.
#: A broad pre-filter can only over-read (which the classifier then rejects),
#: never under-read a genuine refusal.
_PREFILTER = ("%PC-2031%", "%limit reached%")

_CODE_RE = re.compile(r"errorCode=([A-Za-z0-9_.\-]+)")
_CORRELATION_RE = re.compile(r"correlationId=([^\s;]+)")


def parse_capacity_evidence(
    error_text: str | None,
    *,
    provider_key: str,
    credential_account_id: str,
    source_ref: str,
    observed_at: datetime | None = None,
) -> HistoricalCapacityEvidence | None:
    """One operation failure -> capacity evidence, or None when unproven.

    ``is_capacity_exhausted`` is the single authority: an unrelated HTTP 400
    (validation, image, region) returns None even though its text mentions no
    capacity condition at all.
    """
    text = str(error_text or "").strip()
    if not text:
        return None
    code_match = _CODE_RE.search(text)
    error_code = code_match.group(1) if code_match is not None else None
    if not is_capacity_exhausted(error_code, text):
        return None
    correlation_match = _CORRELATION_RE.search(text)
    return HistoricalCapacityEvidence(
        provider_key=provider_key,
        credential_account_id=credential_account_id,
        source_ref=source_ref,
        error_code=error_code,
        correlation_id=correlation_match.group(1) if correlation_match is not None else None,
        observed_at=observed_at,
    )


class SqlAlchemyHistoricalCapacityEvidenceSource:
    """Reads provable refusals out of the platform's own operation history."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def failed_capacity_evidence(
        self, provider_key: str, *, limit: int = 200
    ) -> tuple[HistoricalCapacityEvidence, ...]:
        """Failed hourly creates whose stored error proves a capacity refusal."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        if provider_key != PROVIDER_KEY:
            return ()
        operation = _OperationModel
        server = _ServerModel
        statement = (
            select(
                operation.operation_key,
                operation.error,
                operation.updated_at,
                operation.created_at,
                server.credential_account_id,
            )
            .join(server, server.id == operation.resource_id)
            .where(
                operation.provider_key == provider_key,
                operation.operation_type == _OPERATION_TYPE,
                operation.resource_type == _RESOURCE_TYPE,
                operation.status == _STATUS_FAILED,
                operation.error.isnot(None),
                server.credential_account_id.isnot(None),
                or_(*(operation.error.like(pattern) for pattern in _PREFILTER)),
            )
            .order_by(operation.created_at.desc(), operation.operation_key)
            .limit(limit)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
        evidence: list[HistoricalCapacityEvidence] = []
        for operation_key, error, updated_at, created_at, credential_account_id in rows:
            parsed = parse_capacity_evidence(
                error,
                provider_key=provider_key,
                credential_account_id=str(credential_account_id),
                source_ref=str(operation_key),
                observed_at=_naive_utc(updated_at or created_at),
            )
            if parsed is not None:
                evidence.append(parsed)
        if evidence:
            logger.warning(
                "leaseweb capacity evidence found in history: %d failed create(s) across %d "
                "account(s)",
                len(evidence),
                len({item.credential_account_id for item in evidence}),
            )
        return tuple(evidence)


def _naive_utc(value: Any) -> datetime | None:
    """The DB stores naive UTC; make the UTC boundary explicit for callers."""
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
