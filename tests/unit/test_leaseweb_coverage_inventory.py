"""Honesty checks for the Leaseweb VPS coverage matrix.

``docs/leaseweb/VPS_API_COVERAGE.md`` is only useful if it cannot drift away
from the code or from the Leaseweb documentation. This module enforces that
with three independent comparisons:

1. **Documentation -> inventory**: the paths and operationIds documented for
   the modern VPS / Ordering-VPS / Account-Orders surface must be EXACTLY the
   ones the machine-readable inventory claims (no invented endpoint, no
   forgotten one).
2. **Inventory -> code**: every listed client class and method exists and is
   callable, and the destructive classification matches the code's
   ``DESTRUCTIVE_OPERATIONS`` plus the billable ``order_vps``.
3. **Inventory -> document**: the matrix mentions every operationId, endpoint
   and test, and its summary numbers equal the inventory counts.

The documentation facts come from the local OpenAPI capture
(``api_docs/leaseweb/...html``) when it is present. That capture is a ReDoc
page whose state object uses unquoted JS keys, so it is checked with targeted
patterns (documented paths, ``operationId:"x"``, ``tags:["VPS"]``) rather than
by JSON parsing. The capture is git-ignored, so on a clean clone (CI) the
same facts are read from the committed deterministic snapshot
(``docs/leaseweb/leaseweb_vps_api_snapshot.json``) instead — every assertion
below still runs; only the source of the documentation facts changes. The
snapshot itself is verified byte-identical against the capture by
``python scripts/gen_leaseweb_coverage.py --check`` whenever the capture is
available.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cloud_platform.providers.leaseweb.ordering_api import LeaseWebOrderingApi
from cloud_platform.providers.leaseweb.orders_api import LeaseWebAccountOrdersApi
from cloud_platform.providers.leaseweb.vps.client import (
    DESTRUCTIVE_OPERATIONS,
    LeaseWebVpsApi,
)
from cloud_platform.providers.leaseweb.vps.doc_snapshot import (
    DocOperation,
    build_snapshot,
    extract_legacy_virtual_servers,
    extract_operations,
    load_snapshot,
    snapshot_mismatches,
)
from cloud_platform.providers.leaseweb.vps.inventory import (
    ALL_OPERATIONS,
    COVERAGE_DOC,
    LEGACY_VIRTUAL_SERVERS_NOTE,
    OPERATIONS_BY_ID,
    ORDERING_OPERATIONS,
    ORDERS_OPERATIONS,
    VPS_OPERATIONS,
    coverage_summary,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_CAPTURE = (
    REPO_ROOT
    / "api_docs"
    / "leaseweb"
    / "Leaseweb Developer Portal __ API _ Github _ Terraform.html"
)

CLIENTS = {
    "LeaseWebVpsApi": LeaseWebVpsApi,
    "LeaseWebOrderingApi": LeaseWebOrderingApi,
    "LeaseWebAccountOrdersApi": LeaseWebAccountOrdersApi,
}

#: Client modules that must stay free of the legacy product family.
IMPLEMENTATION_MODULES = (
    "src/cloud_platform/providers/leaseweb/vps/client.py",
    "src/cloud_platform/providers/leaseweb/ordering_api.py",
    "src/cloud_platform/providers/leaseweb/orders_api.py",
    "src/cloud_platform/providers/leaseweb/transport.py",
)


def _inventory_claims() -> tuple[tuple[str, str, str, str], ...]:
    return tuple(
        (operation.operation_id, operation.method.lower(), operation.path, operation.category)
        for operation in ALL_OPERATIONS
    )


@pytest.fixture(scope="module")
def doc_operations() -> list[DocOperation]:
    """Documented operation facts: raw capture when present, snapshot otherwise."""
    if DOCS_CAPTURE.exists():
        documentation = DOCS_CAPTURE.read_text(encoding="utf-8", errors="replace")
        return list(extract_operations(documentation))
    snapshot = load_snapshot(REPO_ROOT)
    return [
        DocOperation(
            operation_id=operation["operationId"],
            method=operation["method"],
            path=operation["path"],
            tag=operation["tag"],
        )
        for operation in snapshot["operations"]
    ]


@pytest.fixture(scope="module")
def legacy_observed() -> list[str]:
    """Legacy Virtual Servers operations visible in the documentation."""
    if DOCS_CAPTURE.exists():
        documentation = DOCS_CAPTURE.read_text(encoding="utf-8", errors="replace")
        return list(extract_legacy_virtual_servers(documentation))
    return list(load_snapshot(REPO_ROOT)["legacy_virtual_servers_observed"])


@pytest.fixture(scope="module")
def coverage_doc() -> str:
    return (REPO_ROOT / COVERAGE_DOC).read_text(encoding="utf-8")


class TestDocumentationAgreement:
    """The inventory covers every documented operation — and invents none."""

    def test_vps_paths_match_the_documentation_exactly(
        self, doc_operations: list[DocOperation]
    ) -> None:
        documented = {operation.path for operation in doc_operations if operation.tag == "VPS"}
        claimed = {operation.path for operation in VPS_OPERATIONS}
        assert documented == claimed

    def test_ordering_vps_paths_match_the_documentation_exactly(
        self, doc_operations: list[DocOperation]
    ) -> None:
        documented = {operation.path for operation in doc_operations if operation.tag == "Ordering"}
        claimed = {operation.path for operation in ORDERING_OPERATIONS}
        assert documented == claimed

    def test_account_order_paths_match_the_documentation_exactly(
        self, doc_operations: list[DocOperation]
    ) -> None:
        documented = {operation.path for operation in doc_operations if operation.tag == "Orders"}
        claimed = {operation.path for operation in ORDERS_OPERATIONS}
        assert documented == claimed

    def test_documented_operation_count_matches_the_inventory(
        self, doc_operations: list[DocOperation]
    ) -> None:
        assert (
            sum(1 for operation in doc_operations if operation.tag == "VPS")
            == len(VPS_OPERATIONS)
            == 38
        )

    def test_every_operation_id_exists_in_the_documentation(
        self, doc_operations: list[DocOperation]
    ) -> None:
        documented_ids = {operation.operation_id for operation in doc_operations}
        for operation in ALL_OPERATIONS:
            assert operation.operation_id in documented_ids, operation

    def test_every_method_and_path_pair_is_documented(
        self, doc_operations: list[DocOperation]
    ) -> None:
        by_path: dict[str, dict[str, set[str]]] = {}
        for operation in doc_operations:
            by_path.setdefault(operation.path, {}).setdefault(operation.method, set()).add(
                operation.operation_id
            )
        for path in {operation.path for operation in ALL_OPERATIONS}:
            claimed = {
                operation.method.lower() for operation in ALL_OPERATIONS if operation.path == path
            }
            assert set(by_path[path]) == claimed, path
            documented_ids = {
                operation_id for ids in by_path[path].values() for operation_id in ids
            }
            for operation in ALL_OPERATIONS:
                if operation.path == path:
                    assert operation.operation_id in documented_ids, operation

    def test_inventory_matches_the_snapshot_exactly(self) -> None:
        snapshot = load_snapshot(REPO_ROOT)
        assert snapshot_mismatches(snapshot, _inventory_claims()) == []

    def test_snapshot_counts_are_exact(self) -> None:
        counts = load_snapshot(REPO_ROOT)["_provenance"]["counts"]
        assert counts == {"VPS": 38, "Ordering": 3, "Orders": 2, "total": 43}

    def test_committed_snapshot_matches_the_capture_when_present(self) -> None:
        if not DOCS_CAPTURE.exists():
            pytest.skip("raw documentation capture is absent (clean clone)")
        documentation = DOCS_CAPTURE.read_text(encoding="utf-8", errors="replace")
        assert build_snapshot(documentation) == load_snapshot(REPO_ROOT)

    def test_legacy_family_is_observed_but_not_claimed(
        self, legacy_observed: list[str], doc_operations: list[DocOperation]
    ) -> None:
        # The legacy family exists in the documentation (exclusion is deliberate).
        assert len(legacy_observed) > 0
        # ... and nothing in the inventory belongs to it.
        assert all(not operation.path.startswith("/virtualServers") for operation in ALL_OPERATIONS)
        assert all("virtualServers" not in operation.path for operation in doc_operations)


class TestInventoryIntegrity:
    """The inventory is machine-readable and internally consistent."""

    def test_counts_and_categories(self) -> None:
        assert len(VPS_OPERATIONS) == 38
        assert len(ORDERING_OPERATIONS) == 3
        assert len(ORDERS_OPERATIONS) == 2
        assert len(ALL_OPERATIONS) == 43
        assert len(OPERATIONS_BY_ID) == 43
        assert all(operation.status == "implemented" for operation in ALL_OPERATIONS)

    def test_summary_matches_the_rows(self) -> None:
        assert coverage_summary() == {
            "VPS": 38,
            "Ordering": 3,
            "Orders": 2,
            "total": 43,
            "destructive": 13,
        }

    def test_every_listed_client_method_exists_and_is_callable(self) -> None:
        for operation in ALL_OPERATIONS:
            client = CLIENTS[operation.client]
            method = getattr(client, operation.client_method, None)
            assert method is not None, operation
            assert callable(method), operation

    def test_destructive_classification_matches_the_code(self) -> None:
        claimed = {operation.client_method for operation in ALL_OPERATIONS if operation.destructive}
        # The billable purchase is destructive too, but it lives on the
        # ordering client, so it is not in the VPS client's set.
        assert claimed == DESTRUCTIVE_OPERATIONS | {"order_vps"}

    def test_no_legacy_virtual_servers_operation_is_claimed(self) -> None:
        assert all(not operation.path.startswith("/virtualServers") for operation in ALL_OPERATIONS)
        for relative in IMPLEMENTATION_MODULES:
            source = (REPO_ROOT / relative).read_text(encoding="utf-8")
            # The comment/docstring in the VPS client only *mentions* the
            # legacy family to explain the exclusion; no endpoint literal may
            # exist (a literal would be a quoted "/virtualServers...").
            assert '"/virtualServers' not in source, relative

    def test_every_referenced_test_exists(self) -> None:
        for operation in ALL_OPERATIONS:
            relative, _, test_id = operation.test.partition("::")
            path = REPO_ROOT / relative
            assert path.exists(), f"{operation}: {relative} does not exist"
            source = path.read_text(encoding="utf-8")
            function, _, parameter = test_id.partition("[")
            assert f"def {function}(" in source, f"{operation}: {operation.test}"
            if parameter:
                # A parametrized case id must be a real case of that test.
                assert f'"{parameter.rstrip("]")}"' in source, f"{operation}: {operation.test}"


class TestCoverageDocument:
    """The human-readable matrix cannot drift from the inventory."""

    def test_every_operation_is_listed(self, coverage_doc: str) -> None:
        for operation in ALL_OPERATIONS:
            assert f"`{operation.operation_id}`" in coverage_doc, operation
            assert operation.path in coverage_doc, operation
            assert f"`{operation.client}.{operation.client_method}`" in coverage_doc, operation

    def test_every_referenced_test_is_listed(self, coverage_doc: str) -> None:
        for operation in ALL_OPERATIONS:
            assert operation.test in coverage_doc, operation

    def test_summary_numbers_match_the_inventory(self, coverage_doc: str) -> None:
        summary = coverage_summary()
        assert f"**{summary['total']}**" in coverage_doc
        assert f"**{summary['destructive']}**" in coverage_doc
        assert "100.0%" in coverage_doc

    def test_legacy_distinction_is_documented(self, coverage_doc: str) -> None:
        assert "Virtual Servers" in coverage_doc
        assert "/virtualServers" in coverage_doc
        assert LEGACY_VIRTUAL_SERVERS_NOTE in coverage_doc
