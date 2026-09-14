"""Honesty checks for the Leaseweb VPS coverage matrix.

``docs/leaseweb/VPS_API_COVERAGE.md`` is only useful if it cannot drift away
from the code or from the local Leaseweb documentation. This module enforces
that with three independent comparisons:

1. **Documentation -> inventory**: the paths and operationIds that exist in
   the local OpenAPI capture (``api_docs/leaseweb/...html``) for the modern
   VPS / Ordering-VPS / Account-Orders surface must be EXACTLY the ones the
   machine-readable inventory claims (no invented endpoint, no forgotten
   one).
2. **Inventory -> code**: every listed client class and method exists and is
   callable, and the destructive classification matches the code's
   ``DESTRUCTIVE_OPERATIONS`` plus the billable ``order_vps``.
3. **Inventory -> document**: the matrix mentions every operationId, endpoint
   and test, and its summary numbers equal the inventory counts.

The doc capture is a ReDoc page whose state object uses unquoted JS keys, so
it is checked with targeted patterns (documented paths, ``operationId:"x"``,
``tags:["VPS"]``) rather than by JSON parsing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from cloud_platform.providers.leaseweb.ordering_api import LeaseWebOrderingApi
from cloud_platform.providers.leaseweb.orders_api import LeaseWebAccountOrdersApi
from cloud_platform.providers.leaseweb.vps.client import (
    DESTRUCTIVE_OPERATIONS,
    LeaseWebVpsApi,
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

CLIENTS: dict[str, type[Any]] = {
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


@pytest.fixture(scope="module")
def documentation() -> str:
    """The local Leaseweb documentation capture, read once per module."""
    assert DOCS_CAPTURE.exists(), f"missing documentation capture: {DOCS_CAPTURE}"
    return DOCS_CAPTURE.read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def coverage_doc() -> str:
    return (REPO_ROOT / COVERAGE_DOC).read_text(encoding="utf-8")


def _documented_paths(documentation: str, prefix: str) -> set[str]:
    pattern = re.compile(rf'"({re.escape(prefix)}[^"]*)":\{{')
    return {match.group(1) for match in pattern.finditer(documentation)}


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


def _documented_methods(documentation: str, path: str) -> set[str]:
    """The HTTP methods the documentation defines for one path."""
    body = _path_object(documentation, path)
    return set(re.findall(r"(?:^|[,{ ])(get|post|put|delete|patch):\{", body))


class TestDocumentationAgreement:
    """The inventory covers every documented operation — and invents none."""

    def test_vps_paths_match_the_documentation_exactly(self, documentation: str) -> None:
        documented = _documented_paths(documentation, "/publicCloud/v1/vps")
        claimed = {operation.path for operation in VPS_OPERATIONS}
        assert documented == claimed

    def test_ordering_vps_paths_match_the_documentation_exactly(self, documentation: str) -> None:
        documented = _documented_paths(documentation, "/ordering/v1/products/vps")
        claimed = {operation.path for operation in ORDERING_OPERATIONS}
        assert documented == claimed

    def test_account_order_paths_match_the_documentation_exactly(self, documentation: str) -> None:
        documented = _documented_paths(documentation, "/account/v1/orders")
        claimed = {operation.path for operation in ORDERS_OPERATIONS}
        assert documented == claimed

    def test_documented_operation_count_matches_the_inventory(self, documentation: str) -> None:
        # ``tags:["VPS"]`` appears exactly once per VPS operation in the doc.
        assert documentation.count('tags:["VPS"]') == len(VPS_OPERATIONS) == 38

    def test_every_operation_id_exists_in_the_documentation(self, documentation: str) -> None:
        for operation in ALL_OPERATIONS:
            assert f'operationId:"{operation.operation_id}"' in documentation, operation

    def test_every_method_and_path_pair_is_documented(self, documentation: str) -> None:
        for path in {operation.path for operation in ALL_OPERATIONS}:
            claimed = {
                operation.method.lower() for operation in ALL_OPERATIONS if operation.path == path
            }
            assert _documented_methods(documentation, path) == claimed, path
            body = _path_object(documentation, path)
            for operation in ALL_OPERATIONS:
                if operation.path == path:
                    assert f'operationId:"{operation.operation_id}"' in body, operation


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
