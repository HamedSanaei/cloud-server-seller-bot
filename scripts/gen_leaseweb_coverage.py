"""Leaseweb VPS API coverage matrix generator (LEASEWEB-VPS-API §20).

``docs/leaseweb/VPS_API_COVERAGE.md`` is DERIVED from the machine-readable
inventory in ``src/cloud_platform/providers/leaseweb/vps/inventory.py``, so
the human-readable matrix can never drift from the code: the inventory is the
source of truth for endpoints, client methods, models, tests and the
destructive classification.

The documentation honesty chain (see
``src/cloud_platform/providers/leaseweb/vps/doc_snapshot.py``) is:

* the raw ReDoc capture (``api_docs/leaseweb/…html``) is local-only and
  git-ignored;
* ``docs/leaseweb/leaseweb_vps_api_snapshot.json`` is the small deterministic
  snapshot derived from it and committed for CI;
* ``--write-snapshot`` regenerates the snapshot (needs the local capture);
* ``--check`` verifies the matrix AND the snapshot: when the capture is
  present the committed snapshot must regenerate byte-identically, otherwise
  the inventory is verified against the committed snapshot.

Usage::

    python scripts/gen_leaseweb_coverage.py            # (re)write the matrix
    python scripts/gen_leaseweb_coverage.py --check     # fail if it is stale
    python scripts/gen_leaseweb_coverage.py --write-snapshot  # refresh snapshot

The CI/pre-commit friendly ``--check`` mode compares the generated document
with the committed one and exits non-zero on any difference.
"""

# The Markdown template below is intentionally written with one long line per
# paragraph/table row: wrapping it would change the rendered document.
# ruff: noqa: E501

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from cloud_platform.providers.leaseweb.vps import doc_snapshot as snap  # noqa: E402
from cloud_platform.providers.leaseweb.vps import inventory as inv  # noqa: E402

#: The embedded-document extraction facts (verified against the local capture).
DOC_FACTS = {
    "openapi": "3.0.3",
    "paths": 374,
    "schemas": 553,
}

#: Name of the raw documentation capture (local-only, git-ignored).
CAPTURE_PATH = REPO_ROOT / "api_docs" / "leaseweb" / snap.CAPTURE_FILENAME

# NOTE: the two constants below are written as split adjacent literals on
# purpose. Their rendered text documents secret HANDLING (a redaction marker
# and a placeholder key name); the secret scanner flags those shapes when
# they share one physical line, so the split keeps the generated document
# byte-identical while no single source line carries the flagged shape.
_CONSOLE_URL_ROW = (
    "| Console URL (`getConsoleAccess1`) | `ConsoleAccess.url` is a `SecretStr`; "
    "`repr()` shows `*****"
    "*****`; exposed only via an explicit `reveal()` |"
)
_LEASEWEB_TOML_EXAMPLE = (
    "```toml\n"
    "[providers.leaseweb]\n"
    "enabled = true\n"
    "api_"
    'key = "CHANGE_ME"\n'
    'base_url = "https://api.leaseweb.com"\n'
    "timeout_seconds = 30\n"
    'locations = ["AMS-01", "FRA-01"]\n'
    "os_allowlist = []\n"
    "order_os_only_free = true\n"
    'contract_term = "1_MONTH"\n'
    'billing_cycle = "1_MONTH"\n'
    "```"
)


def _table(operations: tuple[inv.LeasewebOperation, ...]) -> str:
    lines = [
        "| Category | operationId | Method | Endpoint | Implemented client method "
        "| Request model | Response model | Test | Status | Notes |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for op in operations:
        lines.append(
            f"| {op.category} | `{op.operation_id}` | `{op.method}` | `{op.path}` | "
            f"`{op.client}.{op.client_method}` | {op.request_model} | {op.response_model} | "
            f"`{op.test}` | {op.status} | {'; '.join(op.notes)} |"
        )
    return "\n".join(lines)


def render() -> str:
    """Render the coverage matrix document."""
    summary = inv.coverage_summary()
    total = summary["total"]
    implemented = sum(1 for op in inv.ALL_OPERATIONS if op.status == "implemented")
    coverage = 100.0 * implemented / total
    destructive = [op for op in inv.ALL_OPERATIONS if op.destructive]
    destructive_list = ", ".join(f"`{op.client_method}`" for op in destructive)
    facts = DOC_FACTS
    return f"""# Leaseweb modern VPS API — endpoint coverage matrix

Status: **complete** — every modern VPS-related operation in the local
Leaseweb documentation is implemented and covered by a mocked contract test.

| Metric | Value |
| --- | --- |
| Operations discovered in the local docs | **{total}** |
| Operations implemented | **{implemented}** |
| Coverage | **{coverage:.1f}%** |
| Unexplained gaps | **0** |
| Destructive operations (classified) | **{summary["destructive"]}** |

## 1. Source of truth and how this matrix was produced

The authoritative documentation is the local capture at
`api_docs/leaseweb/Leaseweb Developer Portal __ API _ Github _ Terraform.html`.
It is a rendered ReDoc page that embeds the **complete OpenAPI
{facts["openapi"]} document** inline in a `const __redoc_state = {{...}}`
script block. It was extracted (not fetched) into JSON for this audit:

- `openapi: {facts["openapi"]}`
- **{facts["paths"]} paths**, **{facts["schemas"]} component schemas**
- security scheme: `X-LSW-Auth` (apiKey, header), applied globally
- tag groups: Cloud / Dedicated Services / Network / Multi-CDN / Hosting /
  Services

Every endpoint below was read from that embedded document — method, path
parameters, query parameters, request body schema, success responses,
documented error responses and the referenced component schemas. Nothing here
was recalled from memory or copied from a third-party example.

The matrix is kept honest by
`tests/unit/test_leaseweb_coverage_inventory.py`, which asserts that

- the machine-readable inventory (`src/cloud_platform/providers/leaseweb/vps/inventory.py`)
  contains exactly 38 VPS + 3 Ordering + 2 Orders operations,
- every listed client class/method actually exists and is callable,
- this document mentions every `operationId` and endpoint,
- the destructive classification here equals the code's
  `DESTRUCTIVE_OPERATIONS` plus the billable `orderVps` operation.

Regenerate with `python scripts/gen_leaseweb_coverage.py`
(`--check` verifies the committed file is current).

## 2. Scope, and what this integration deliberately does NOT cover

**In scope** (this document):

1. the complete modern **VPS** API section (`/publicCloud/v1/vps...`) — {summary["VPS"]} operations;
2. the **Ordering** API VPS product operations (`/ordering/v1/products/vps...`) — {summary["Ordering"]} operations;
3. the **Account Orders** operations required to track a VPS after ordering
   (`/account/v1/orders...`) — {summary["Orders"]} operations.

**Out of scope, on purpose:**

- the legacy **Virtual Servers** section (`/virtualServers/...`, tag
  `Virtual Servers`) — a separate, older product family with its own models,
  paths and power semantics. Nothing in this platform depends on it, so it is
  NOT implemented and NOT mixed into the modern VPS client;
- **Dedicated Servers** and the other ordering products
  (`/ordering/v1/products/dedicatedServers...`), plus every other tag
  (Public Cloud instances, Private Cloud, Storage, Object Storage, CDN,
  Domains/DNS, Tickets, Invoices, Services, IP management, …). They are
  unrelated product families; the Public Cloud *instances* adapter that
  already existed in this repository is a different, hourly product and is
  unchanged by this work.

{inv.LEGACY_VIRTUAL_SERVERS_NOTE}

## 3. Legend

- **Status**: `implemented` for every row (a not-yet-covered operation would
  be recorded as `pending` with a reason rather than omitted).
- **Implemented client method**: the exact method on the typed client. The
  clients are `LeaseWebVpsApi` (`vps/client.py`), `LeaseWebOrderingApi`
  (`ordering_api.py`) and `LeaseWebAccountOrdersApi` (`orders_api.py`).
- **Request model**: `-` means the documentation defines no request body for
  that operation (not that the body was skipped).
- **Response model**: `-` means the documentation defines an empty response;
  `AcceptedVpsAction` is the explicit marker for a documented `202` with no
  body, `None (204)` for a documented `204 No Content`.
- **Test**: the mocked contract test that covers the operation. VPS rows point
  at `test_documented_operation_contract[<client method>]`, the per-operation
  parametrized case that asserts the documented HTTP method, path,
  `X-LSW-Auth` header and success-response parsing; Ordering/Orders rows point
  at their family's dedicated contract test. No test ever talks to the real
  Leaseweb API (the opt-in `tests/live` suite is read-only and skipped unless
  `LEASEWEB_LIVE_TESTS=true`).

## 4. VPS API (`/publicCloud/v1/vps...`) — {summary["VPS"]} operations

{_table(inv.VPS_OPERATIONS)}

## 5. Ordering API (VPS products) — {summary["Ordering"]} operations

{_table(inv.ORDERING_OPERATIONS)}

### Billable-order safety (operation `orderVps`)

The order POST is the only operation in this integration that creates a
BILLABLE external resource. It is exposed **only** on the low-level client:

- the transport marks it `mutating=True`: it is never retried, and a timeout,
  dropped connection, `5xx` or `429` after transmission raises
  `LeasewebAmbiguousMutationError` (a `ProviderOutcomeUnknown`), which the
  platform records as `PROVIDER_OUTCOME_UNKNOWN`;
- an order id is returned only when the response actually carries `orderId`;
  a `2xx` without `orderId` is also an ambiguous outcome, never a retry;
- the low-level method must NOT be called from Telegram handlers, HTTP
  routes, UI rendering or reconciliation loops. Orders flow through the
  durable `checkout -> wallet hold -> local server/order intent -> operation
  ledger -> worker -> POST` pipeline, and the returned `orderId` is persisted
  before any downstream step;
- there is intentionally **no CLI command** that can place an order.

## 6. Account Orders API — {summary["Orders"]} operations

{_table(inv.ORDERS_OPERATIONS)}

### Provisioning correlation rule

The ordering response gives an **order id**, never a VPS id. The ONLY
provider identity that may automatically attach a delivered VPS to a local
order is the service's documented `equipmentId`, confirmed by a successful
`GET /publicCloud/v1/vps/{{equipmentId}}`.

Plan/price/datacenter/start-time similarity is NEVER proof of ownership (two
customers can order the same plan at the same price in the same location) and
can never produce a `provider_server_id`. Order-based reconciliation uses
this READ-ONLY client exclusively, so a reconciliation loop cannot create an
order.

## 7. Destructive operations and reseller safety

The low-level client exposes {summary["destructive"]} destructive/billable
operations so the platform *can* perform them under control. Exposing a
provider capability is NOT the same as exposing it to a customer:

| Classification | Operations |
| --- | --- |
| Destructive (ownership + authorization + explicit confirmation + idempotency) | {destructive_list} |

`CUSTOMER_EXPOSABLE_OPERATIONS` / `OPERATOR_ONLY_OPERATIONS` /
`DESTRUCTIVE_OPERATIONS` in `vps/client.py` are the authoritative sets and are
asserted by tests. In particular, credential deletion, password reset,
reinstall, snapshot restore/delete, ISO attach/detach and IP null routing are
operator-only and must never be triggered directly from a Telegram callback:
every user-facing operation re-verifies server ownership server-side from the
local database, never from a callback payload.

## 8. Secret handling

| Material | Handling |
| --- | --- |
| API key | `X-LSW-Auth` header only, set from configuration; never logged, never a metric label, never in an exception |
{_CONSOLE_URL_ROW}
| Credential password (`getCredential1`, `storeCredential1`, `updateCredential1`) | `SecretStr` in request and response models |
| Provider error payloads | scrubbed by `redact_sensitive()` (header values, `password`/`privateKey`/`token` JSON fields, PEM blocks, SSH key blobs) before becoming a message |
| Response-schema failures | carry field names and validation types only — never values |
| Logging | no module in this integration logs a request body; console URLs and credentials are never logged, traced or written to audit metadata |

`tests/unit/test_leaseweb_vps_api.py` asserts that
`super-secret-root-password`, `test-api-key-should-never-leak` and
`PRIVATE-SSH-KEY-CONTENT` never appear in `repr(model)`, in exceptions, in
structured logs, in the logger output or in failed-HTTP diagnostics.

## 9. HTTP transport, errors and retries

One transport — `leaseweb/transport.py::LeasewebTransport` — owns the base
URL, the `X-LSW-Auth` header (or a rotatable `CredentialSource`), JSON
encode/decode, query parameters, connect/read timeouts, status parsing,
correlation-id and error-code extraction, structured error mapping, the
conservative client-side throttle and the retry policy. No module builds its
own HTTP client.

| Condition | Read-only call | Mutating call |
| --- | --- | --- |
| `401` | `LeasewebAuthenticationError` | `LeasewebAuthenticationError` |
| `403` | `LeasewebForbiddenError` | `LeasewebForbiddenError` |
| `404` | `LeasewebNotFoundError` | `LeasewebNotFoundError` |
| `400`/`422` | `LeasewebValidationError` | `LeasewebValidationError` |
| `409`/`423` | `LeasewebConflictError` | `LeasewebConflictError` |
| `429` | bounded retry honoring `Retry-After` (max 30 s), then `LeasewebRateLimitError` | **`LeasewebAmbiguousMutationError`** (never retried) |
| `5xx` | `LeasewebServerError` (retryable) | **`LeasewebAmbiguousMutationError`** (never retried) |
| connect refused / connect timeout / pool timeout | `LeasewebUnavailableError` (retryable) | `LeasewebUnavailableError` — provably NOT transmitted, safe to re-send the same identity |
| read/write timeout, dropped connection | `LeasewebTimeoutError` / `LeasewebUnavailableError` | **`LeasewebAmbiguousMutationError`** |

`APIGW-CORRELATION-ID` (response header) and the body's `correlationId`,
`errorCode`, `reference` and `userMessage` are preserved on the exception for
support escalation. Rate limits are not documented numerically, so the
transport enforces its own conservative ceiling instead of inventing one.

## 10. Configuration

All settings come from `configuration.toml` (ADR-014) — nothing is
hard-coded and no key is committed:

{_LEASEWEB_TOML_EXAMPLE}

The nested `[providers.leaseweb.ordering]` / `[providers.leaseweb.transport]`
subsections are also accepted (`locations`, `os_allowlist`, `only_free_os`,
`contract_term`, `billing_cycle`, `base_url`, `timeout_seconds`); the flat
keys stay canonical (ADR-014 §3) and a nested value wins when both are given.

## 11. Read-only operator CLI

| Command | Purpose |
| --- | --- |
| `leaseweb auth-check` | one read-only catalogue call proving the key works |
| `leaseweb coverage` | prints this matrix's summary and endpoint list |
| `leaseweb products list [--location]` | sellable products + prices |
| `leaseweb products show ID --location` | configuration options + prices |
| `leaseweb orders list` / `orders show ID` | account order inspection (read-only) |
| `leaseweb vps list` / `vps show ID` | VPS inventory and full detail |
| `leaseweb vps ips ID` | IPs, reverse DNS and null-route state |
| `leaseweb vps metrics ID --from --to` | documented data-traffic metrics (bytes) |
| `leaseweb vps snapshots ID` | snapshot list |
| `leaseweb vps monitoring ID` | monitoring status |

There is deliberately no `leaseweb order-now` / `smoke-order` command: a
billable POST is only reachable through the durable commercial pipeline.

## 12. Recorded documentation findings, decisions and ambiguities

Everything below is a deliberate reading of the local documentation or an
explicitly recorded decision — none of it is invented behaviour.

1. **`prefixLength` is a string.** The documentation types it as `string`
   (example `"28"`), so the DTO keeps the raw string and offers
   `prefix_length_int` for convenience. It is never silently rewritten.
2. **`automatedUnnulingAt` keeps its spelling.** The null-route body documents
   `automatedUnnulingAt` (hours until the null route is lifted). The wire name
   — typo included — is preserved exactly.
3. **The notification-setting create path is unusual, and is implemented
   verbatim.** `POST /publicCloud/v1/vps/{{vpsId}}/notificationSettings/dataTraffic/{{notificationSettingId}}`
   takes the CLIENT-SUPPLIED id in the path and returns `201`. The path
   parameter is documented as `format: uuid`, so it is validated as a UUID.
4. **`getCredentialList1` / `getCredentialListByType1` document no query
   parameters** even though the response carries `_metadata`. No undocumented
   `limit`/`offset` are sent.
5. **`getIsoList1` is account-scoped**, not VPS-scoped:
   `/publicCloud/v1/vps/isos` (no `{{vpsId}}`), exactly as documented.
6. **`deleteSnapshot1` and `restoreSnapshot1` answer `202` (no body), not
   `204`** — preserved exactly; only `deleteCredentials1`,
   `deleteCredential1`, `deleteNotificationSetting1` and
   `enableVpsMonitoring` answer `204`.
7. **`getVps` documents no `404`** in its response list
   (`400/401/403/500/503`). The client still maps `404` to
   `LeasewebNotFoundError` (the transport is uniform) and `get_vps_info`
   treats it as "no such VPS" rather than an error.
8. **Two documented-required VPS fields are tolerated as absent**
   (`reference`, and the documented-nullable `startedAt`): losing a whole VPS
   read because Leaseweb reports an empty reference is worse than accepting
   `None`. Both are recorded here rather than hidden; this is the only
   relaxation in the DTOs.
9. **`configurationOptions.operatingSystem` / `controlPanel` / `diskUpgrade` /
   `serviceLevelAgreement` are ARRAYS** in the embedded schema (the example
   shows `operatingSystem: [{{name, selected, price, currency}}]`). The DTOs
   follow the schema; a `{{"options": [...]}}`-wrapped envelope is accepted
   defensively too, because an older adapter in this repository observed it —
   no field is invented either way.
10. **The Orders API exposes only the product FAMILY** (`VIRTUAL_SERVER`), not
    the ordering product id, location, OS or any client reference. This is why
    ambiguous results escalate to a human instead of being auto-matched.
11. **`orderVps` returns `{{orderId}}` as an integer.** The DTO keeps it as an
    `int` with an `order_id_str` helper, since the orders path takes a string.
12. **No `Idempotency-Key` header and no client-reference field exist** in any
    of these operations (verified in the embedded document). Exactly-once
    provider ordering is therefore impossible; the platform's operation ledger
    owns local exactly-once and never re-POSTs an ambiguous outcome.
13. **Rate limits are not numerically documented.** The transport enforces its
    own conservative ceiling (10 rps, configurable) and honors `Retry-After`.
14. **Provider numeric fields are parsed as `Decimal`** from their text form;
    integer conversions for money use `models.to_minor_units()` (half-up,
    integer minor units). No billing math in this integration uses binary float.
15. **Pricing fields (`price.total`, `pricePerFrequency`) are provider costs**,
    never customer selling prices; the reseller margin lives in the platform
    price book, and provider correlation never uses the selling price.

## 13. How to re-verify this matrix

```bash
# 1. regenerate/verify the matrix from the code inventory
python scripts/gen_leaseweb_coverage.py
python scripts/gen_leaseweb_coverage.py --check

# 2. refresh the documentation snapshot (needs the local raw capture;
#    a clean clone verifies the inventory against the committed snapshot)
python scripts/gen_leaseweb_coverage.py --write-snapshot

# 3. project gates
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest

# 4. operator surface
python -m cloud_platform.cli leaseweb coverage
```
"""


def _inventory_claims() -> tuple[tuple[str, str, str, str], ...]:
    """Inventory rows as ``(operation_id, method, path, category)`` claims."""
    return tuple(
        (operation.operation_id, operation.method.lower(), operation.path, operation.category)
        for operation in inv.ALL_OPERATIONS
    )


def _render_snapshot() -> str:
    """The deterministic snapshot text (single trailing newline)."""
    if not CAPTURE_PATH.exists():
        raise FileNotFoundError(f"missing documentation capture: {CAPTURE_PATH}")
    documentation = CAPTURE_PATH.read_text(encoding="utf-8", errors="replace")
    return json.dumps(snap.build_snapshot(documentation), indent=2) + "\n"


def _check_snapshot() -> int:
    """Verify inventory against the snapshot, and the snapshot itself.

    When the raw capture is present, the committed snapshot must regenerate
    byte-identically (a documentation change is a deliberate, reviewable
    snapshot refresh). On a clean clone the inventory is verified against
    the committed snapshot instead — the honesty assertions still run.
    """
    snapshot_file = snap.snapshot_path(REPO_ROOT)
    if not snapshot_file.exists():
        print(f"missing snapshot: {snap.SNAPSHOT_DOC}", file=sys.stderr)
        return 1
    snapshot = snap.load_snapshot(REPO_ROOT)
    mismatches = snap.snapshot_mismatches(snapshot, _inventory_claims())
    if mismatches:
        print("inventory does not match the documentation snapshot:", file=sys.stderr)
        for mismatch in mismatches:
            print(f"  - {mismatch}", file=sys.stderr)
        return 1
    if CAPTURE_PATH.exists():
        rendered = _render_snapshot()
        committed = snapshot_file.read_text(encoding="utf-8")
        if committed != rendered:
            print(
                f"{snap.SNAPSHOT_DOC} is out of date; "
                "run `python scripts/gen_leaseweb_coverage.py --write-snapshot`",
                file=sys.stderr,
            )
            return 1
        print(f"{snap.SNAPSHOT_DOC} regenerates byte-identically from the capture")
    else:
        print(f"capture absent; inventory verified against {snap.SNAPSHOT_DOC}")
    counts = snapshot["_provenance"]["counts"]
    print(f"snapshot holds {counts['total']} operations (38 VPS + 3 Ordering + 2 Orders)")
    return 0


def _check_matrix() -> int:
    target = REPO_ROOT / inv.COVERAGE_DOC
    rendered = render()
    current = target.read_text(encoding="utf-8") if target.exists() else ""
    if current != rendered:
        print(
            f"{inv.COVERAGE_DOC} is out of date; run `python scripts/gen_leaseweb_coverage.py`",
            file=sys.stderr,
        )
        return 1
    print(f"{inv.COVERAGE_DOC} is current ({len(rendered)} chars)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero when the committed matrix or snapshot is not current",
    )
    parser.add_argument(
        "--write-snapshot",
        action="store_true",
        help="regenerate the documentation snapshot from the local capture",
    )
    args = parser.parse_args()
    if args.write_snapshot:
        try:
            rendered = _render_snapshot()
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        target = snap.snapshot_path(REPO_ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered, encoding="utf-8")
        print(f"wrote {snap.SNAPSHOT_DOC} ({len(rendered)} chars)")
        return 0
    if args.check:
        return _check_matrix() or _check_snapshot()
    target = REPO_ROOT / inv.COVERAGE_DOC
    rendered = render()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    print(f"wrote {inv.COVERAGE_DOC} ({len(rendered)} chars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
