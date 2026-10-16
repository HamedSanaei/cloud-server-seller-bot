"""Strict JSON boundary for the explicit funding cutover administration command.

No configuration, credentials, database connection or target discovery occurs
here. The application supplies its configured service. Preview is the default;
applying requires the caller to deliberately pass ``apply=True``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, fields
from datetime import datetime
from typing import Any
from uuid import UUID

from cloud_platform.modules.funding.cutover import (
    CutoverEntry,
    CutoverManifest,
    SqlAlchemyOperatorFundingCutover,
)
from cloud_platform.modules.funding.domain import (
    FundingSource,
    FundingValidationError,
    ProviderCostSnapshot,
)


def manifest_from_dict(payload: Mapping[str, Any]) -> CutoverManifest:
    """Parse only documented fields; never silently ignore stale anchors."""
    if not isinstance(payload, Mapping) or set(payload) != {
        "operation_key",
        "operator_id",
        "effective_at",
        "entries",
    }:
        raise FundingValidationError(
            "manifest requires operation_key/operator_id/effective_at/entries"
        )
    raw_entries = payload["entries"]
    if not isinstance(raw_entries, list) or not 0 < len(raw_entries) <= 20:
        raise FundingValidationError("entries must be an explicit list of 1 to 20 servers")
    entries = []
    allowed = {item.name for item in fields(CutoverEntry)}
    try:
        for raw in raw_entries:
            if not isinstance(raw, Mapping) or set(raw) - allowed:
                raise FundingValidationError("unknown cutover entry fields")
            data = dict(raw)
            for name in ("server_id", "owner_id"):
                data[name] = UUID(data[name])
            for name in (
                "expected_last_accrued_at",
                "expected_paid_until",
                "period_start",
                "period_end",
            ):
                if data.get(name) is not None:
                    data[name] = datetime.fromisoformat(data[name])
            if "expected_source" in data:
                data["expected_source"] = FundingSource(data["expected_source"])
            if data.get("provider_cost_snapshot") is not None:
                data["provider_cost_snapshot"] = ProviderCostSnapshot.from_dict(
                    data["provider_cost_snapshot"]
                )
            if "refund_charge_ids" in data:
                if not isinstance(data["refund_charge_ids"], list):
                    raise FundingValidationError("refund_charge_ids must be a list")
                data["refund_charge_ids"] = tuple(UUID(item) for item in data["refund_charge_ids"])
            entries.append(CutoverEntry(**data))
        return CutoverManifest(
            operation_key=payload["operation_key"],
            operator_id=UUID(payload["operator_id"]),
            effective_at=datetime.fromisoformat(payload["effective_at"]),
            entries=tuple(entries),
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        if isinstance(exc, FundingValidationError):
            raise
        raise FundingValidationError("manifest has missing or invalid typed anchors") from exc


async def run_manifest(
    service: SqlAlchemyOperatorFundingCutover,
    payload: Mapping[str, Any],
    *,
    apply: bool = False,
) -> dict[str, Any]:
    manifest = manifest_from_dict(payload)
    results = await (service.apply(manifest) if apply else service.preview(manifest))
    return {
        "mode": "apply" if apply else "preview",
        "manifest_fingerprint": manifest.fingerprint(),
        "results": [
            {
                key: str(value) if isinstance(value, UUID) else value
                for key, value in asdict(item).items()
            }
            for item in results
        ],
    }
