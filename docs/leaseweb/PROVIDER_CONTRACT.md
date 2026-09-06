# LeaseWeb Cloud provider contract (sellable EU provider #2)

Status: **implemented** — `LeaseWebProvider` in
`src/cloud_platform/providers/leaseweb/` maps this API onto the
provider-neutral `CloudProvider` port in `src/cloud_platform/providers/base.py`.

## 1. Why LeaseWeb

Hetzner is provider #1. LeaseWeb is provider #2 because it sells the same
primitives this platform already owns (cloud instances, regions, images,
power lifecycle) behind a plain REST API with per-key auth — no SDK, no
provider types leaking into the domain.

## 2. Transport and authentication

- Base URL (production): `https://api.leaseweb.com/cloud/v2` (overridable via
  `LEASEWEB_API_BASE_URL`, same pattern as `HETZNER_API_BASE_URL`).
- Auth: `X-LSW-Auth: <API-KEY>` header — **plain key, no `Bearer` prefix**.
- The key lives in encrypted secret storage / `CredentialHolder` (M10-008),
  never in logs, metric labels or audit payloads (only `key_hint`).

## 3. Capability mapping

Advertised phase-1 set (`LEASEWEB_CAPABILITIES`):

| Platform `Capability` | LeaseWeb endpoint(s) | Notes |
| --- | --- | --- |
| `COMPUTE` | `GET/POST /instances`, `GET/DELETE /instances/{id}` | Create body: `instanceTypeId`, `imageId`, `region`, `hostname`, HOURLY contract. |
| `POWER` | `POST /instances/{id}/powerOn`, `.../powerOff`, `.../reboot` | |
| `REBUILD` | `POST /instances/{id}/rebuild` | Via instance rebuild action. |
| `SNAPSHOT` | snapshot actions | |
| `FIREWALL` | firewall rules | |
| `NETWORK` | private networks | |
| `FLOATING_IP` / `PRIMARY_IP` | IP assignment | |
| `RDNS` | reverse DNS | |
| `VOLUME` | block storage | |
| `BACKUP` | — | **not advertised** in phase 1 (provider-side flag only). |
| `RESCUE` | — | **not advertised** in phase 1 (no stable public endpoint). |

## 4. Catalog / pricing

- `GET /regions` → `ProviderLocation` (`id`, `countryCode` → `country_code`).
- `GET /instanceTypes` → `ProviderPlan` + per-region `PlanPricing`.
- `GET /images` → `ProviderImage` (only `system`/`os`/`distribution` types).
- Prices (`pricePerHour`/`pricePerMonth`, `currency`) are ingested as
  `Decimal` major units via `PricingIngestionService` — never float, never
  hard-coded. One catalog row per (provider, plan, location).

## 5. Idempotency and reconciliation

- The spec documents no `Idempotency-Key` header, so create is guarded by a
  get-before-create on the deterministic server name (same pattern as
  ArvanCloud); delete treats 404 as success.
- Status vocabulary is normalized (`RUNNING` → `running`, `SHUTOFF` →
  `stopped`, ...) for the reconciliation layer.
- Ambiguous power re-sends go through `probe_power_effect` (read-only).

## 6. Error mapping

| HTTP | Platform error |
| --- | --- |
| 401/403 | `ProviderAuthError` |
| 404 | `ProviderNotFound` (delete/get → success/`None`) |
| 409/423 or "already" in message | `ProviderConflict` |
| 429 | `ProviderRateLimited` (honors `Retry-After`, bounded retries) |
| 5xx / network | `ProviderUnavailable` |

## 7. Selling checklist (operator)

1. Create a LeaseWeb Cloud API key in the provider panel.
2. Set `LEASEWEB_API_KEY` (and optionally `LEASEWEB_API_BASE_URL`) in `.env`.
3. `uv run alembic upgrade head`
4. `uv run python scripts/sync_catalog.py --provider leaseweb`
5. Publish a price book version covering `leaseweb/*` (margin rules).
6. Enable the offers you want to sell (catalog visibility service).
7. Order from Telegram (`/menu`) or REST v1 — allocation picks
   hetzner/leaseweb by capability + region policy.
