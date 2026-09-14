# Leaseweb modern VPS API — endpoint coverage matrix

Status: **complete** — every modern VPS-related operation in the local
Leaseweb documentation is implemented and covered by a mocked contract test.

| Metric | Value |
| --- | --- |
| Operations discovered in the local docs | **43** |
| Operations implemented | **43** |
| Coverage | **100.0%** |
| Unexplained gaps | **0** |
| Destructive operations (classified) | **13** |

## 1. Source of truth and how this matrix was produced

The authoritative documentation is the local capture at
`api_docs/leaseweb/Leaseweb Developer Portal __ API _ Github _ Terraform.html`.
It is a rendered ReDoc page that embeds the **complete OpenAPI
3.0.3 document** inline in a `const __redoc_state = {...}`
script block. It was extracted (not fetched) into JSON for this audit:

- `openapi: 3.0.3`
- **374 paths**, **553 component schemas**
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

1. the complete modern **VPS** API section (`/publicCloud/v1/vps...`) — 38 operations;
2. the **Ordering** API VPS product operations (`/ordering/v1/products/vps...`) — 3 operations;
3. the **Account Orders** operations required to track a VPS after ordering
   (`/account/v1/orders...`) — 2 operations.

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

The local documentation also contains a separate, legacy 'Virtual Servers' section with /virtualServers/... endpoints. It is a different (older) product family with different models, paths and power semantics; nothing in this platform depends on it, so it is deliberately NOT implemented and NOT mixed into the modern VPS client.

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

## 4. VPS API (`/publicCloud/v1/vps...`) — 38 operations

| Category | operationId | Method | Endpoint | Implemented client method | Request model | Response model | Test | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| VPS | `start` | `POST` | `/publicCloud/v1/vps/{vpsId}/start` | `LeaseWebVpsApi.start_vps` | - | AcceptedVpsAction | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[start_vps]` | implemented | 202 no body (documented); precondition: VPS must be stopped |
| VPS | `stop` | `POST` | `/publicCloud/v1/vps/{vpsId}/stop` | `LeaseWebVpsApi.stop_vps` | - | AcceptedVpsAction | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[stop_vps]` | implemented | 202 no body (documented); precondition: VPS must be running |
| VPS | `reboot` | `POST` | `/publicCloud/v1/vps/{vpsId}/reboot` | `LeaseWebVpsApi.reboot_vps` | - | AcceptedVpsAction | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[reboot_vps]` | implemented | 202 no body (documented); precondition: VPS must be running |
| VPS | `getConsoleAccess1` | `GET` | `/publicCloud/v1/vps/{vpsId}/console` | `LeaseWebVpsApi.get_console_access` | - | ConsoleAccess | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[get_console_access]` | implemented | temporary access URL: SecretStr, never logged |
| VPS | `getCredentialList1` | `GET` | `/publicCloud/v1/vps/{vpsId}/credentials` | `LeaseWebVpsApi.list_credentials` | - | list[CredentialSummary] | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[list_credentials]` | implemented | documented without query parameters |
| VPS | `storeCredential1` | `POST` | `/publicCloud/v1/vps/{vpsId}/credentials` | `LeaseWebVpsApi.store_credential` | StoreCredentialRequest | StoredCredential | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[store_credential]` | implemented | password is SecretStr in request and response |
| VPS | `deleteCredentials1` | `DELETE` | `/publicCloud/v1/vps/{vpsId}/credentials` | `LeaseWebVpsApi.delete_credentials` | - | None (204) | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[delete_credentials]` | implemented |  |
| VPS | `getCredentialListByType1` | `GET` | `/publicCloud/v1/vps/{vpsId}/credentials/{type}` | `LeaseWebVpsApi.list_credentials_by_type` | - | list[CredentialSummary] | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[list_credentials_by_type]` | implemented | type enum: OPERATING_SYSTEM, CONTROL_PANEL |
| VPS | `getCredential1` | `GET` | `/publicCloud/v1/vps/{vpsId}/credentials/{type}/{username}` | `LeaseWebVpsApi.get_credential` | - | CredentialDetail | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[get_credential]` | implemented | password returned as SecretStr |
| VPS | `updateCredential1` | `PUT` | `/publicCloud/v1/vps/{vpsId}/credentials/{type}/{username}` | `LeaseWebVpsApi.update_credential` | UpdateCredentialRequest | StoredCredential | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[update_credential]` | implemented |  |
| VPS | `deleteCredential1` | `DELETE` | `/publicCloud/v1/vps/{vpsId}/credentials/{type}/{username}` | `LeaseWebVpsApi.delete_credential` | - | None (204) | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[delete_credential]` | implemented |  |
| VPS | `resetPassword1` | `POST` | `/publicCloud/v1/vps/{vpsId}/resetPassword` | `LeaseWebVpsApi.reset_password` | - | AcceptedVpsAction | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[reset_password]` | implemented | 202 no body (documented); read the new value via credentials endpoints |
| VPS | `getIsoList1` | `GET` | `/publicCloud/v1/vps/isos` | `LeaseWebVpsApi.list_isos` | - | LeasewebPage[IsoRecord] | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[list_isos]` | implemented | account-wide catalogue, not vps-scoped (documented path) |
| VPS | `attachIso1` | `POST` | `/publicCloud/v1/vps/{vpsId}/attachIso` | `LeaseWebVpsApi.attach_iso` | AttachIsoRequest | AcceptedVpsAction | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[attach_iso]` | implemented | 202 no body; precondition: no ISO attached |
| VPS | `detachIso1` | `POST` | `/publicCloud/v1/vps/{vpsId}/detachIso` | `LeaseWebVpsApi.detach_iso` | - | AcceptedVpsAction | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[detach_iso]` | implemented | 202 no body; no request body documented |
| VPS | `getReinstallImageList1` | `GET` | `/publicCloud/v1/vps/{vpsId}/reinstall/images` | `LeaseWebVpsApi.list_reinstall_images` | - | LeasewebPage[ReinstallImage] | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[list_reinstall_images]` | implemented | query: limit, offset, standard |
| VPS | `reinstall` | `PUT` | `/publicCloud/v1/vps/{vpsId}/reinstall` | `LeaseWebVpsApi.reinstall` | ReinstallRequest | AcceptedVpsAction | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[reinstall]` | implemented | DESTRUCTIVE: recreates the VPS; cannot run while snapshots exist |
| VPS | `getIPList1` | `GET` | `/publicCloud/v1/vps/{vpsId}/ips` | `LeaseWebVpsApi.list_ips` | - | LeasewebPage[VpsIpDetails] | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[list_ips]` | implemented | query: version (4|6), nullRouted, ips ('|' separated) |
| VPS | `getIP` | `GET` | `/publicCloud/v1/vps/{vpsId}/ips/{ip}` | `LeaseWebVpsApi.get_ip` | - | VpsIpDetails | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[get_ip]` | implemented | ip path parameter: format ip |
| VPS | `updateIP1` | `PUT` | `/publicCloud/v1/vps/{vpsId}/ips/{ip}` | `LeaseWebVpsApi.update_ip` | UpdateIpRequest | VpsIpDetails | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[update_ip]` | implemented |  |
| VPS | `nullRouteIP1` | `POST` | `/publicCloud/v1/vps/{vpsId}/ips/{ip}/null` | `LeaseWebVpsApi.null_route_ip` | NullRouteIpRequest | VpsIpDetails | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[null_route_ip]` | implemented | IPv4 only (documented); body optional; automatedUnnulingAt is hours |
| VPS | `removeIPNullRoute1` | `POST` | `/publicCloud/v1/vps/{vpsId}/ips/{ip}/unnull` | `LeaseWebVpsApi.remove_ip_null_route` | - | VpsIpDetails | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[remove_ip_null_route]` | implemented |  |
| VPS | `getDataTrafficMetrics` | `GET` | `/publicCloud/v1/vps/{vpsId}/metrics/datatraffic` | `LeaseWebVpsApi.get_data_traffic_metrics` | - | DataTrafficMetrics | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[get_data_traffic_metrics]` | implemented | query: from, to, granularity (5m|10m|30m|60m|DAY), aggregation (SUM); values are bytes as integers; metadata preserved verbatim |
| VPS | `getSnapshotList1` | `GET` | `/publicCloud/v1/vps/{vpsId}/snapshots` | `LeaseWebVpsApi.list_snapshots` | - | LeasewebPage[Snapshot] | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[list_snapshots]` | implemented |  |
| VPS | `createSnapshot1` | `POST` | `/publicCloud/v1/vps/{vpsId}/snapshots` | `LeaseWebVpsApi.create_snapshot` | CreateSnapshotRequest | AcceptedVpsAction | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[create_snapshot]` | implemented | 202 no body; one snapshot per VPS (documented) |
| VPS | `getSnapshot1` | `GET` | `/publicCloud/v1/vps/{vpsId}/snapshots/{snapshotId}` | `LeaseWebVpsApi.get_snapshot` | - | Snapshot | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[get_snapshot]` | implemented | snapshotId path parameter: format uuid |
| VPS | `restoreSnapshot1` | `PUT` | `/publicCloud/v1/vps/{vpsId}/snapshots/{snapshotId}` | `LeaseWebVpsApi.restore_snapshot` | - | AcceptedVpsAction | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[restore_snapshot]` | implemented | 202 no body (documented) |
| VPS | `deleteSnapshot1` | `DELETE` | `/publicCloud/v1/vps/{vpsId}/snapshots/{snapshotId}` | `LeaseWebVpsApi.delete_snapshot` | - | AcceptedVpsAction | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[delete_snapshot]` | implemented | 202 no body (documented, NOT 204) |
| VPS | `getVpsList` | `GET` | `/publicCloud/v1/vps/` | `LeaseWebVpsApi.list_vps` | - | LeasewebPage[VpsSummary] | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[list_vps]` | implemented | query: limit, offset, id, reference, ip, state, pack, region |
| VPS | `getVps` | `GET` | `/publicCloud/v1/vps/{vpsId}` | `LeaseWebVpsApi.get_vps` | - | VpsDetail | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[get_vps]` | implemented | no 404 in the documented response list (401/403/500/503 only) |
| VPS | `updateVps` | `PUT` | `/publicCloud/v1/vps/{vpsId}` | `LeaseWebVpsApi.update_vps` | UpdateVpsRequest | VpsDetail | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[update_vps]` | implemented | documented body: reference |
| VPS | `getNotificationSettingList1` | `GET` | `/publicCloud/v1/vps/{vpsId}/notificationSettings/dataTraffic` | `LeaseWebVpsApi.list_data_traffic_notification_settings` | - | LeasewebPage[NotificationSetting] | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[list_data_traffic_notification_settings]` | implemented |  |
| VPS | `getNotificationSetting1` | `GET` | `/publicCloud/v1/vps/{vpsId}/notificationSettings/dataTraffic/{notificationSettingId}` | `LeaseWebVpsApi.get_data_traffic_notification_setting` | - | NotificationSetting | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[get_data_traffic_notification_setting]` | implemented |  |
| VPS | `createNotificationSetting1` | `POST` | `/publicCloud/v1/vps/{vpsId}/notificationSettings/dataTraffic/{notificationSettingId}` | `LeaseWebVpsApi.create_data_traffic_notification_setting` | CreateNotificationSettingRequest | NotificationSetting | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[create_data_traffic_notification_setting]` | implemented | documented 201; CLIENT-SUPPLIED id in the PATH (unusual shape kept verbatim) |
| VPS | `updateNotificationSetting1` | `PUT` | `/publicCloud/v1/vps/{vpsId}/notificationSettings/dataTraffic/{notificationSettingId}` | `LeaseWebVpsApi.update_data_traffic_notification_setting` | UpdateNotificationSettingRequest | NotificationSetting | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[update_data_traffic_notification_setting]` | implemented |  |
| VPS | `deleteNotificationSetting1` | `DELETE` | `/publicCloud/v1/vps/{vpsId}/notificationSettings/dataTraffic/{notificationSettingId}` | `LeaseWebVpsApi.delete_data_traffic_notification_setting` | - | None (204) | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[delete_data_traffic_notification_setting]` | implemented |  |
| VPS | `getVpsMonitoringStatus` | `GET` | `/publicCloud/v1/vps/{vpsId}/monitoring/status` | `LeaseWebVpsApi.get_monitoring_status` | - | MonitoringStatusResult | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[get_monitoring_status]` | implemented |  |
| VPS | `enableVpsMonitoring` | `POST` | `/publicCloud/v1/vps/{vpsId}/monitoring/enable` | `LeaseWebVpsApi.enable_monitoring` | - | None (204) | `tests/unit/test_leaseweb_vps_api.py::test_documented_operation_contract[enable_monitoring]` | implemented | 204 no content (documented) |

## 5. Ordering API (VPS products) — 3 operations

| Category | operationId | Method | Endpoint | Implemented client method | Request model | Response model | Test | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Ordering | `getVpsList1` | `GET` | `/ordering/v1/products/vps` | `LeaseWebOrderingApi.list_products` | - | LeasewebPage[VpsProductListItem] | `tests/unit/test_leaseweb_ordering_api.py::test_list_products_sends_documented_query` | implemented | query: location, limit, offset |
| Ordering | `getVps1` | `GET` | `/ordering/v1/products/vps/{vpsId}` | `LeaseWebOrderingApi.get_product` | - | VpsProductDetail | `tests/unit/test_leaseweb_ordering_api.py::test_get_product_sends_every_documented_parameter` | implemented | query (all documented): location (required), diskUpgrade, operatingSystem, controlPanel, contractTerm, billingCycle, serviceLevelAgreement |
| Ordering | `orderVps` | `POST` | `/ordering/v1/products/vps/{vpsId}/order` | `LeaseWebOrderingApi.order_vps` | OrderVpsRequest | VpsOrderResult | `tests/unit/test_leaseweb_ordering_api.py::test_order_posts_documented_body_and_returns_order_id` | implemented | BILLABLE: creates a real contract; 201 {orderId}; never retried; ambiguous outcome raises LeasewebAmbiguousMutationError; must flow through checkout -> hold -> operation ledger -> worker |

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

## 6. Account Orders API — 2 operations

| Category | operationId | Method | Endpoint | Implemented client method | Request model | Response model | Test | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Orders | `getOrders` | `GET` | `/account/v1/orders` | `LeaseWebAccountOrdersApi.list_orders` | - | LeasewebPage[AccountOrder] | `tests/unit/test_leaseweb_orders_api.py::test_list_orders_paginates` | implemented | read-only; limit/offset |
| Orders | `getOrder` | `GET` | `/account/v1/orders/{Id}` | `LeaseWebAccountOrdersApi.get_order` | - | AccountOrder | `tests/unit/test_leaseweb_orders_api.py::test_get_order_exposes_equipment_id` | implemented | read-only; reconciliation MUST use this (GET) path only; equipmentId is the only identity that may attach a delivered VPS |

### Provisioning correlation rule

The ordering response gives an **order id**, never a VPS id. The ONLY
provider identity that may automatically attach a delivered VPS to a local
order is the service's documented `equipmentId`, confirmed by a successful
`GET /publicCloud/v1/vps/{equipmentId}`.

Plan/price/datacenter/start-time similarity is NEVER proof of ownership (two
customers can order the same plan at the same price in the same location) and
can never produce a `provider_server_id`. Order-based reconciliation uses
this READ-ONLY client exclusively, so a reconciliation loop cannot create an
order.

## 7. Destructive operations and reseller safety

The low-level client exposes 13 destructive/billable
operations so the platform *can* perform them under control. Exposing a
provider capability is NOT the same as exposing it to a customer:

| Classification | Operations |
| --- | --- |
| Destructive (ownership + authorization + explicit confirmation + idempotency) | `store_credential`, `delete_credentials`, `update_credential`, `delete_credential`, `reset_password`, `attach_iso`, `detach_iso`, `reinstall`, `null_route_ip`, `restore_snapshot`, `delete_snapshot`, `delete_data_traffic_notification_setting`, `order_vps` |

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
| Console URL (`getConsoleAccess1`) | `ConsoleAccess.url` is a `SecretStr`; `repr()` shows `**********`; exposed only via an explicit `reveal()` |
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

```toml
[providers.leaseweb]
enabled = true
api_key = "CHANGE_ME"
base_url = "https://api.leaseweb.com"
timeout_seconds = 30
locations = ["AMS-01", "FRA-01"]
os_allowlist = []
order_os_only_free = true
contract_term = "1_MONTH"
billing_cycle = "1_MONTH"
```

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
   verbatim.** `POST /publicCloud/v1/vps/{vpsId}/notificationSettings/dataTraffic/{notificationSettingId}`
   takes the CLIENT-SUPPLIED id in the path and returns `201`. The path
   parameter is documented as `format: uuid`, so it is validated as a UUID.
4. **`getCredentialList1` / `getCredentialListByType1` document no query
   parameters** even though the response carries `_metadata`. No undocumented
   `limit`/`offset` are sent.
5. **`getIsoList1` is account-scoped**, not VPS-scoped:
   `/publicCloud/v1/vps/isos` (no `{vpsId}`), exactly as documented.
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
   shows `operatingSystem: [{name, selected, price, currency}]`). The DTOs
   follow the schema; a `{"options": [...]}`-wrapped envelope is accepted
   defensively too, because an older adapter in this repository observed it —
   no field is invented either way.
10. **The Orders API exposes only the product FAMILY** (`VIRTUAL_SERVER`), not
    the ordering product id, location, OS or any client reference. This is why
    ambiguous results escalate to a human instead of being auto-matched.
11. **`orderVps` returns `{orderId}` as an integer.** The DTO keeps it as an
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
