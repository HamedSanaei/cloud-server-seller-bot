"""Deterministic Leaseweb documentation snapshot (CI source of truth).

The full Leaseweb documentation capture
(``api_docs/leaseweb/Leaseweb Developer Portal __ API _ Github _ Terraform.html``)
is a ~12 MB rendered ReDoc page and stays git-ignored. CI runners never have
it, so the honesty tests cannot read it there.

This module bridges that gap without weakening the verification:

* :func:`extract_operations` parses the exact ``(path, method, operationId,
  tag)`` facts out of a raw capture, scoped per documented path object (the
  same scoping the honesty tests use).
* :func:`build_snapshot` freezes those facts into a small deterministic JSON
  document committed at ``docs/leaseweb/leaseweb_vps_api_snapshot.json``.
* :func:`verify_inventory_against_snapshot` compares the machine-readable
  inventory (``vps/inventory.py``) against those frozen facts.

Nothing here is invented: the snapshot is regenerated from the capture with
``python scripts/gen_leaseweb_coverage.py --write-snapshot`` and verified with
``--check``. When the capture is available, ``--check`` fails unless the
committed snapshot regenerates byte-identically; when it is not (clean CI
clones), the inventory is verified against the committed snapshot instead.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

__all__ = [
    "CAPTURE_FILENAME",
    "DOCUMENTED_FAMILIES",
    "SNAPSHOT_DOC",
    "DocOperation",
    "build_snapshot",
    "extract_legacy_virtual_servers",
    "extract_operations",
    "load_snapshot",
    "snapshot_mismatches",
]

#: Raw documentation capture (local-only, git-ignored).
CAPTURE_FILENAME = "Leaseweb Developer Portal __ API _ Github _ Terraform.html"

#: Tracked deterministic snapshot (the CI source of truth).
SNAPSHOT_DOC = "docs/leaseweb/leaseweb_vps_api_snapshot.json"

#: Documentation families this integration covers (path prefixes).
DOCUMENTED_FAMILIES: tuple[str, ...] = (
    "/publicCloud/v1/vps",
    "/ordering/v1/products/vps",
    "/account/v1/orders",
)

_METHOD_RX = re.compile(r"(?:^|[,{ ])(get|post|put|delete|patch):\{")
_OPERATION_RX = re.compile(r'operationId:"([^"]+)"')
_TAG_RX = re.compile(r'tags:\["([^"\]]+)"\]')
_LEGACY_MENU_RX = re.compile(
    r"tag/Virtual-Servers/operation/(get|post|put|delete|patch)/([A-Za-z0-9_{}/.\-]+)"
)


@dataclass(frozen=True, slots=True)
class DocOperation:
    """One documented operation fact: identity, HTTP method, path and tag."""

    operation_id: str
    method: str  # lowercase, exactly as documented
    path: str  # exactly as documented
    tag: str


def _path_object(documentation: str, path: str) -> str:
    """The balanced-brace object literal of one documented path.

    The capture is a JS object (unquoted keys), so the substring is found by
    matching braces while skipping over string literals.
    """
    marker = f'"{path}":{{'
    start = documentation.index(marker)
    index = start + len(marker) - 1  # positioned on the opening brace
    depth = 0
    while index < len(documentation):
        char = documentation[index]
        if char == '"':
            end = index + 1
            while documentation[end] != '"' or documentation[end - 1] == "\\":
                end += 1
            index = end
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                break
        index += 1
    return documentation[start : index + 1]


def _documented_paths(documentation: str, prefix: str) -> set[str]:
    pattern = re.compile(rf'"({re.escape(prefix)}[^"]*)":\{{')
    return {match.group(1) for match in pattern.finditer(documentation)}


def extract_operations(documentation: str) -> tuple[DocOperation, ...]:
    """Extract every documented operation of the covered families.

    Each ``(path, method, operationId, tag)`` fact is scoped to its own path
    object and method block, so unrelated product families documented in the
    same page can never leak into the result.
    """
    operations: set[DocOperation] = set()
    for prefix in DOCUMENTED_FAMILIES:
        for path in sorted(_documented_paths(documentation, prefix)):
            body = _path_object(documentation, path)
            marks = list(_METHOD_RX.finditer(body))
            for pos, mark in enumerate(marks):
                end = marks[pos + 1].start() if pos + 1 < len(marks) else len(body)
                chunk = body[mark.start() : end]
                operation = _OPERATION_RX.search(chunk)
                tag = _TAG_RX.search(chunk)
                if operation is None or tag is None:  # pragma: no cover
                    raise ValueError(f"undocumented method block: {path} {mark.group(1)}")
                operations.add(
                    DocOperation(
                        operation_id=operation.group(1),
                        method=mark.group(1),
                        path=path,
                        tag=tag.group(1),
                    )
                )
    return tuple(sorted(operations, key=lambda op: (op.path, op.method, op.operation_id)))


def extract_legacy_virtual_servers(documentation: str) -> tuple[str, ...]:
    """The legacy Virtual Servers operations visible in the documentation.

    Recorded as exclusion evidence: the family exists in the docs and is
    deliberately NOT implemented (separate older product family).
    """
    return tuple(
        sorted({f"{method} {rest}" for method, rest in _LEGACY_MENU_RX.findall(documentation)})
    )


class SnapshotOperation(TypedDict):
    """One frozen documentation fact in the snapshot file."""

    operationId: str
    method: str
    path: str
    tag: str


class SnapshotDocument(TypedDict):
    """Shape of the committed snapshot JSON document."""

    _provenance: dict[str, Any]
    operations: list[SnapshotOperation]
    legacy_virtual_servers_observed: list[str]
    legacy_note: str


def build_snapshot(documentation: str) -> SnapshotDocument:
    """Build the deterministic snapshot document from a raw capture."""
    operations = extract_operations(documentation)
    counts = {"VPS": 0, "Ordering": 0, "Orders": 0}
    for operation in operations:
        if operation.tag in counts:
            counts[operation.tag] += 1
    total = len(operations)
    return {
        "_provenance": {
            "generated_by": "python scripts/gen_leaseweb_coverage.py --write-snapshot",
            "source_capture": f"api_docs/leaseweb/{CAPTURE_FILENAME}",
            "source_note": (
                "Local-only raw ReDoc capture (~12 MB, git-ignored). "
                "This small snapshot is the tracked, deterministic CI source "
                "of truth derived from it — operationId, HTTP method, path "
                "and tag per operation, nothing invented."
            ),
            "openapi": "3.0.3",
            "counts": {**counts, "total": total},
        },
        "operations": [
            {
                "operationId": operation.operation_id,
                "method": operation.method,
                "path": operation.path,
                "tag": operation.tag,
            }
            for operation in operations
        ],
        "legacy_virtual_servers_observed": list(extract_legacy_virtual_servers(documentation)),
        "legacy_note": (
            "The legacy Virtual Servers family is present in the documentation "
            "and deliberately NOT implemented here: a separate, older product "
            "family with different models, paths and power semantics."
        ),
    }


def snapshot_path(repo_root: Path) -> Path:
    """Path of the committed snapshot file."""
    return repo_root / SNAPSHOT_DOC


def load_snapshot(repo_root: Path) -> SnapshotDocument:
    """Read and parse the committed snapshot file."""
    raw: SnapshotDocument = json.loads(snapshot_path(repo_root).read_text(encoding="utf-8"))
    return raw


def snapshot_mismatches(
    snapshot: SnapshotDocument,
    claimed: tuple[tuple[str, str, str, str], ...],
) -> list[str]:
    """Compare inventory claims against snapshot facts.

    ``claimed`` holds ``(operation_id, method, path, category)`` rows from the
    inventory. Returns human-readable mismatches (empty when exact): invented
    operations, forgotten operations, and method/path/tag drift.
    """
    documented = {operation["operationId"]: operation for operation in snapshot["operations"]}
    errors: list[str] = []
    for operation_id, method, path, category in claimed:
        actual = documented.get(operation_id)
        if actual is None:
            errors.append(f"invented or stale operation: {method.upper()} {path} ({operation_id})")
            continue
        if (actual["method"], actual["path"], actual["tag"]) != (method, path, category):
            errors.append(
                f"drifted operation {operation_id}: snapshot has "
                f"{actual['method'].upper()} {actual['path']} [{actual['tag']}], "
                f"inventory claims {method.upper()} {path} [{category}]"
            )
    for operation_id in sorted(set(documented) - {row[0] for row in claimed}):
        errors.append(f"documented operation missing from inventory: {operation_id}")
    return errors
