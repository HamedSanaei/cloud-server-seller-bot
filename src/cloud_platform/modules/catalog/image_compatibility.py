"""Provider-neutral image compatibility rules for cloud offers.

Provider adapters preserve different documented payload shapes, but checkout,
catalog synchronization, and reconciliation must agree on whether one image
is usable for one pinned plan/location/account.  This module owns that
normalization so an index/display label is never treated as an identity.
"""

from __future__ import annotations

from typing import Any

_PLAN_KEYS = ("plan_ids", "instance_type_ids", "type_ids", "instance_types", "plans")
_LOCATION_KEYS = ("locations", "location_ids", "regions", "region_ids")
_ACCOUNT_KEYS = ("account_ids", "accounts", "credential_account_ids", "account_id")


def _metadata(image: Any) -> dict[str, Any] | None:
    raw = getattr(image, "metadata", {}) or {}
    if not isinstance(raw, dict):
        return None
    values: dict[str, Any] = dict(raw)
    for envelope_key in ("restrictions", "metadata"):
        nested = values.get(envelope_key)
        if nested is not None:
            if not isinstance(nested, dict):
                return None
            values.update(nested)
    return values


def _restriction_values(values: dict[str, Any], keys: tuple[str, ...]) -> set[str] | None:
    """Return None only when no restriction key is present.

    An explicitly present empty list/dict is an empty restriction set and
    therefore incompatible; silently treating it as unrestricted widens the
    set of images and can provision a different billable identity.
    """
    present = False
    result: set[str] = set()
    for key in keys:
        if key not in values:
            continue
        present = True
        raw = values[key]
        if isinstance(raw, dict):
            raw_values: list[Any] = list(raw.values())
        elif isinstance(raw, (list, tuple, set, frozenset)):
            raw_values = list(raw)
        elif raw is None:
            raw_values = []
        else:
            raw_values = [raw]
        result.update(str(item).strip() for item in raw_values if str(item).strip())
    return result if present else None


def image_restriction_values(image: Any, keys: tuple[str, ...]) -> set[str] | None:
    """Normalize a documented image restriction shape for one component."""
    values = _metadata(image)
    return None if values is None else _restriction_values(values, keys)


def image_compatible(
    image: Any,
    *,
    plan_id: str | None = None,
    architecture: str | None = None,
    location_id: str | None = None,
    account_id: str | None = None,
) -> bool:
    """Return whether a provider image can be used for pinned cloud facts.

    Missing restriction metadata means unrestricted.  Present restriction
    metadata, including an empty list, is fail-closed.  The function never
    resolves a positional index or a display label.
    """
    values = _metadata(image)
    if values is None:
        return False
    expected_architecture = str(architecture or "").strip().lower()
    if expected_architecture:
        image_architecture = (
            str(getattr(image, "architecture", "") or values.get("architecture", ""))
            .strip()
            .lower()
        )
        if image_architecture != expected_architecture:
            return False
    for value, keys in (
        (plan_id, _PLAN_KEYS),
        (location_id, _LOCATION_KEYS),
        (account_id, _ACCOUNT_KEYS),
    ):
        if value is None:
            continue
        restrictions = _restriction_values(values, keys)
        if restrictions is not None and str(value).strip() not in restrictions:
            return False
    return True


def image_architecture_conflict(image: Any, architecture: str | None) -> bool:
    """Whether an image contradicts a pinned plan architecture.

    Only a POSITIVE mismatch counts: when either side states nothing (many
    providers list instance architectures but not image ones), the image is
    kept instead of silently disappearing, and the create-time gate stays the
    authority for the remaining unknown.
    """
    expected = str(architecture or "").strip().lower()
    if not expected:
        return False
    values = _metadata(image) or {}
    declared = str(getattr(image, "architecture", "") or values.get("architecture", ""))
    declared = declared.strip().lower()
    if not declared:
        return False
    return declared != expected


__all__ = [
    "image_architecture_conflict",
    "image_compatible",
    "image_restriction_values",
]
