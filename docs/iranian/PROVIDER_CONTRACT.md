# Iranian provider selection and API contract (M15-001)

Status: **selected** — ArvanCloud IaaS is the first Iranian provider. This
document is the durable contract the M15-002 adapter must satisfy. Verified
against ArvanCloud's official API documentation and the published OpenAPI
snapshot [iaas-1.0.json](./api/iaas-1.0.json)
(source: `https://www.arvancloud.ir/api-docs/iaas-1.0.json`, captured
2026-08-24).

## 1. Selection

| Candidate | Why / why not |
| --- | --- |
| **ArvanCloud (selected)** | Large domestic provider; public REST IaaS API with a downloadable OpenAPI spec; Machine-User API keys; in-Iran regions (`ir-thr-*`); plan prices exposed per hour/day/month; async task tracking endpoint. Docs: [API Usage](https://docs.arvancloud.ir/en/developer-tools/api/api-usage), [IaaS OpenAPI](https://www.arvancloud.ir/api/iaas/1.0). |
| Liara | Solid PaaS with OpenAPI docs, but instance model is PaaS-flavored (PVM/containers) and less aligned with the raw IaaS operations this platform owns end-to-end. Kept as fallback. |
| IDCloud (Unico) | Cloud products under the Unico platform; API surface documented via DevCenter but less complete public OpenAPI for IaaS at capture time. Revisit if ArvanCloud integration hits a wall. |

Decision: build the adapter for **ArvanCloud IaaS 1.0**. Everything below is
provider-neutral on the domain side; the adapter maps this API onto the
`CloudProvider` port in `src/cloud_platform/providers/base.py`.

## 2. Transport and authentication

- Base URL (production): `https://napi.arvancloud.ir/ecc/v1` (overridable for
  staging/tests via settings, same pattern as `hetzner_api_base_url`).
- Auth: `Authorization: <MU-KEY>` header — **plain key, no `Bearer` prefix** —
  plus `Accept: application/json`, `Content-Type: application/json` for bodies.
  Keys are Machine User keys created in the provider panel.
- The key is stored in encrypted secret storage (never `.env` in production,
  never logged, never in metric labels, redacted in any error text — the same
  redaction discipline as the Hetzner token).
- All endpoints are region-scoped under `/regions/{region}/...`; a small set of
  global endpoints exists (e.g. `GET /regions`, `GET /dedicated-servers/...`).

## 3. Capability mapping

Provider capabilities are advertised via the `capabilities` frozenset. Phase-1
advertised set for ArvanCloud:

| Platform `Capability` | ArvanCloud endpoint(s) (per region) | Notes |
| --- | --- | --- |
| `COMPUTE` | `GET/POST /regions/{r}/servers`, `GET/DELETE /regions/{r}/servers/{id}` | Core lifecycle. |
| `POWER` | `POST /regions/{r}/servers/{id}/power-on`, `.../power-off`, `.../reboot` | `hard-reboot` exists; use `reboot` for the platform `POWER` capability. |
| `REBUILD` | `POST /regions/{r}/servers/{id}/rebuild` | Via `RebuildImageRequest`. |
| `RESCUE` | `POST .../rescue`, `.../unrescue` | `RescueServerRequest`. |
| `SNAPSHOT` | `POST .../snapshot`, `GET /regions/{r}/snapshots`, `.../snapshots/{id}/revert` | Snapshot->image and snapshot->volume endpoints also exist. |
| `FIREWALL` | `GET/POST /regions/{r}/securities`, `.../securities/security-rules/{id}` | Security groups; server attach/detach via `add-security-group`/`remove-security-group`. |
| `NETWORK` | `GET/POST /regions/{r}/networks`, `/subnets`, `/ports` + `attach-vpc` | Private VPC; phase-1 may advertise but expose read-only until a product decision. |
| `FLOATING_IP` | `GET/POST /regions/{r}/float-ips`, `.../float-ips/{id}/attach`, `.../detach` | |
| `PRIMARY_IP` | `POST /regions/{r}/servers/{id}/add-public-ip` | `AddServerPublicIPRequest`. |
| `RDNS` | `POST /regions/{r}/ptr/`, `DELETE /regions/{r}/ptr/{ip}` | `CreatePTRRequest`. |
| `BACKUP` | — | Scheduled backups are a provider-side flag on create (`enable_backup`); no independent backup-management API in the IaaS spec → **not advertised** in phase 1. |
| `VOLUME` | `Volume`, `VolumeOptions` definitions exist (create/attach/detach) | Spec references present; **not advertised in phase 1** until verified against a live account. |

Not implemented in phase 1 regardless: `DedicatedServers`, `Databases`,
`TrafficPackages`, `ServerGroups`, `IPAdvertising` (out of platform scope).

## 4. Key request/response shapes (from the OpenAPI snapshot)

- **Create** (`POST /regions/{r}/servers`, body `CreateServerRequest`):
  `name`, `flavor_id` (plan), `image_id`, `key_name`/`ssh_key` (SSH key by
  **name**, not by id), `init_script` (cloud-init user data), `enable_ipv4`,
  `enable_ipv6`, `vpcId`/`vpcSubnetId`, `security_groups`, `disk_type`,
  `disk_size`, `create_type`, `enable_backup`. Mapping from our
  `CreateServerRequest`: `plan_id -> flavor_id`, `image_id -> image_id`,
  `user_data -> init_script`, `ssh_key_ids -> key_name` (single key — the
  adapter must pick/require one and document the limitation).
- **Plan** (`GET /regions/{r}/sizes`, `Plan`): `id`, `name`, `cpu_count`,
  `memory`/`memory_in_bytes`, `disk`/`disk_in_bytes`, `generation`,
  `bandwidth_in_bytes`, `port_speed`, `pps`, GPU fields, and
  **`price_per_hour` / `price_per_day` / `price_per_month`** (see §7).
- **Region** (`GET /regions`, `Region`): `code`, `city`, `city_code`,
  `country`, `dc`, `beta`, `create` (whether creation is allowed in the
  region). Map to `ProviderLocation` (`code -> id`, `country ->
  country_code`, `city -> city`).
- **Images** (`GET /regions/{r}/images`, `ImageList`): provider images plus
  marketplace images; map `os_family` from the image name/metadata.
- **Quota** (`GET /regions/{r}/quota`, `Quota`/`QuotaValue`): per-region
  quota reporting — available for future quota pre-checks; the platform keeps
  its own `QuotaPolicy` (M10-003) as the source of truth for user-facing
  limits.
- **Server detail** (`GET /regions/{r}/servers/{id}`, `ServerDetail`): status,
  IPs, sizes, images, volumes; status strings normalized by the adapter into
  the platform state machine via `normalize_provider_status`-style mapping
  (exact provider status vocabulary must be captured from a live account
  before phase 1 freeze — record the observed values in
  `docs/iranian/INTEGRATION_NOTES.md` when credentials exist).

## 5. Async operations and reconciliation

Mutations return task references; completion is tracked via
`GET /regions/{r}/servers/inquiry/{task_id}` (200 → `ServerDetail`,
404 → task/region not found, 500 → internal error). This is the same
action-model as Hetzner:

- The adapter treats the HTTP 202/200 response as "accepted", never as
  "finished".
- The operation ledger (M07-002) stores the platform `operation_key` and the
  provider task id in `provider_response`; the provisioning worker and the
  reconcile job poll `inquiry`/server detail until the terminal state matches.
- A timeout or error after a mutation is **ambiguous until reconciled** (same
  rule as `docs/hetzner/INTEGRATION_NOTES.md`).
- Delete reconciliation treats 404 as success (resource already gone).

## 6. Idempotency (platform-side)

The IaaS 1.0 spec documents **no `Idempotency-Key` request header**.
Invariant ("external mutations must be idempotent and reconciled") is
therefore enforced **platform-side** in the adapter:

1. Every mutation goes through the operation ledger with the platform
   `operation_key` (doubles as the provider idempotency key concept).
2. Before a create, the adapter lists servers and matches on the
   platform correlation label (same approach as Hetzner's
   `PLATFORM_SERVER_ID_LABEL`) to detect a prior attempt that already
   materialized.
3. Power/delete operations are re-sent only when the recorded operation is
   still PENDING/IN_FLIGHT; COMPLETED operations are never re-sent.
4. `delete_server` reconciliation: 404 ⇒ success.

If the provider later documents an idempotency header, the adapter may adopt
it, but the ledger+label check remains.

## 7. Pricing model

- The `Plan` schema exposes `price_per_hour`, `price_per_day`,
  `price_per_month` (numeric, provider-published). **The adapter must pass
  them through into `ProviderPlan.metadata`** (`price_per_hour` etc.) — never
  hard-code them in the codebase.
- The catalog sync job (M04-006) owns refreshing these into the catalog;
  platform **billing** prices come from the explicit price book (margin
  policy), not from provider prices. Provider prices are cost inputs for the
  margin report (M06-008), never user-facing.
- Currency: provider prices are Iranian Rial (IRR) unless the spec says
  otherwise — capture the currency field/assumption from a live account.
  The wallet already handles ISO-4217 integer minor units (IRR = 2 decimals).
  No float anywhere in money math.
- Billing granularity: confirm hourly vs. prepaid-package billing
  (`prepaidPackageTemplateID` / `prepaid_package_template` appear in `Plan`
  and create request) — the platform bills hourly with the usage-segment
  model (M06-004); prepaid packages, if used, must map to explicit
  pre-purchase charges, never to negative wallet races.

## 8. Error model

Documented error body: `ErrorResponse { "message": string, "errors":
[[string]] }`; success acknowledgements: `MessageResponse { "message":
string }`. Documented status codes include 200, 404, 500, and 401 with
`{"message": "Unauthenticated."}`.

Adapter mapping (must be exact; metrics outcome labels depend on it):

| Provider signal | Platform exception | Metric outcome |
| --- | --- | --- |
| 401 (e.g. `Unauthenticated.`), 403 | `ProviderAuthError` | `auth_error` |
| 404 (server/task/region not found) | `ProviderNotFound` | `not_found` |
| 409 or "already exists / already in use" messages | `ProviderConflict` | `conflict` |
| 429 (if issued; none documented at capture) | `ProviderRateLimited(reset_at_unix=...)` from `Retry-After` when present | `rate_limited` |
| 5xx, connection reset, TLS failures, DNS | `ProviderUnavailable` | `unavailable` |
| anything else (JSON decode, unexpected shape) | `ProviderError` | `provider_error` |

Rules: parse `message` defensively (absent/empty is allowed); never echo raw
error bodies into user-facing Telegram text; error bodies never contain
secrets but must still pass through the shared redaction pass.

## 9. Rate limiting

No numeric limits or rate-limit headers are documented in the captured spec
or docs (gap — confirm with the provider team when credentials are
provisioned). Until then:

- The adapter applies a **conservative client-side throttle** (configurable
  max requests/second per account; default 10, same mechanism as the Hetzner
  client retry/throttle stack) and honors `Retry-After` on 429.
- Captures any observed limit/remaining/reset headers into provider-call
  metrics (M11-001 already records `cloud_platform_provider_calls_total` /
  duration per provider+operation+outcome).
- Catalog sync and reconcile jobs must stay well under the throttle.

## 10. Security requirements (non-negotiable)

- MU key: encrypted secret storage; env only for local dev; redacted in logs,
  errors, traces, and metric labels (provider label = `arvancloud`, never the
  key).
- `init_script`/cloud-init payloads are customer secrets — never logged.
- Destructive operations (`delete_server`, `terminate`) require the
  application-layer authorization chain (saga + explicit authorization +
  idempotency key, M07-007/008) exactly like Hetzner.
- No endpoint may be called with a production key from tests; tests use a
  fake HTTP layer (contract tests against a live low-risk account come with
  the M15-002 acceptance).

## 11. Adapter delivery checklist (for M15-002)

- [ ] `src/cloud_platform/providers/arvancloud/` package: `client.py`
      (transport + auth + error mapping + throttle), `adapter.py`
      (`CloudProvider` implementation, `key="arvancloud"`, capabilities from
      §3), label handling mirroring `PLATFORM_SERVER_ID_LABEL`.
- [ ] Settings: `arvancloud_api_key` (encrypted storage),
      `arvancloud_api_base_url` default `https://napi.arvancloud.ir/ecc/v1`.
- [ ] Registration in the provider registry (M03-006 pattern).
- [ ] Status vocabulary capture + normalization table (live account).
- [ ] Tests: fake-HTTP unit tests for every port method, error-mapping table
      tests, idempotency/reconciliation tests, zero secret leakage in test
      snapshots.
- [ ] `docs/iranian/INTEGRATION_NOTES.md` for live-account observations
      (mirroring `docs/hetzner/INTEGRATION_NOTES.md`).

## 12. Open items to verify with live credentials

1. Exact `ServerDetail` status strings (state normalization table).
2. Whether create is synchronous or task-based in practice, and the
   `inquiry` poll interval guidance.
3. Numeric rate limits / rate-limit headers.
4. Price currency field confirmation (IRR) and prepaid-package semantics.
5. SSH key handling details (key_name vs ssh_key precedence).
6. Whether a documented idempotency header exists outside the public spec.
