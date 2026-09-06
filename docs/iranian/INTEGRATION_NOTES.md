# ArvanCloud integration notes (M15-002)

Status: **adapter implemented, live account pending.** This file records
observations from a live ArvanCloud account as credentials are provisioned,
mirroring `docs/hetzner/INTEGRATION_NOTES.md`. Every open item from
`PROVIDER_CONTRACT.md` §12 maps to a section below; each stays `TBD` until
observed, and the adapter's behavior is designed to keep working while they
are TBD (normalization passes unknown values through; the throttle is
conservative; prices pass through metadata).

## 1. Status vocabulary (contract §12.1)

`TBD` — capture the exact `status`/`task_state` strings once a real server is
created. The adapter's normalization table (`normalize_provider_status`)
currently maps the expected values:

| raw (expected) | platform state |
| --- | --- |
| `available`, `running`, `started`, `active` | `running` |
| `creating`, `build`, `building`, `pending` | `building` |
| `deleting`, `delete` | `deleting` |
| `stopped`, `off`, `powered-off` | `stopped` |
| `error`, `failed` | `error` |
| anything else | passed through lower-cased |

Observations: _(none yet)_

## 2. Create flow: synchronous or task-based? (§12.2)

`TBD` — the spec's create response is a `ServerDetail` carrying
`task_id`/`task_state`; confirm whether a created server is immediately
addressable or must be polled via
`GET /regions/{r}/servers/inquiry/{task_id}`, and what poll interval the
provider team recommends. The reconciliation layer already treats
"accepted, not finished" as the model; the poll interval feeds
`OperationsSettings` tuning.

Observations: _(none yet)_

## 3. Rate limits (§12.3)

`TBD` — the public spec documents no numeric limits or rate-limit headers.
The adapter ships with a conservative client-side throttle (default
**10 req/s**, `Throttle(max_rps=...)`) and honors `Retry-After` (and
`Retry-When`) on 429 with bounded retries. When a live account reveals
actual limits, record them here and tighten/relax the default accordingly.

Observed headers: _(none yet)_

## 4. Price currency and prepaid packages (§12.4)

`TBD` — provider prices are assumed **IRR** and pass through
`ProviderPlan.metadata` (`price_per_hour` / `price_per_day` /
`price_per_month`). Confirm the currency field from a live account and
record it here; confirm whether `prepaid_package_template` implies a
prepaid billing model that must map to explicit pre-purchase charges (the
platform bills hourly usage segments and must never create negative wallet
races).

Observations: _(none yet)_

## 5. SSH key handling (§12.5)

`TBD` — the create request takes a single `key_name` (SSH key by **name**,
not id). The platform `CreateServerRequest` may carry several ids; the
adapter uses the **first** id as the key name and documents this limitation
here. Confirm precedence of `key_name` vs `ssh_key` fields on a live
account.

Observations: _(none yet)_

## 6. Idempotency header (§12.6)

`TBD` — the public spec documents **no** `Idempotency-Key` header, so
idempotency is enforced platform-side in the adapter: operation-ledger
`operation_key` + get-before-create name correlation + delete-treats-404-as
success. If the provider team confirms an undocumented header, adopt it in
the client but keep the ledger + correlation guard (defense in depth).

Observations: _(none yet)_

## 7. Regions endpoint

`GET /regions` is documented in the contract, but the captured OpenAPI
snapshot (iaas-1.0.json) contains no such path. The adapter tries it first
and falls back to the configured default region (`arvancloud_region`) when
it 404s, so catalog sync works either way. Confirm the real endpoint on a
live account and record the exact path/shape.

Observations: _(none yet)_

## 8. Live smoke checklist (run when credentials exist)

1. `GET /regions` (or the real regions endpoint) — record §7.
2. Create a smallest-flavor server with a deterministic name; record
   status transitions (§1) and create timing (§2).
3. Observe headers on a burst of reads for §3.
4. Read one `Plan`; record currency and prepaid fields (§4).
5. Create an SSH key, reference it by name, create with it (§5).
6. Delete the server; confirm 404-after-delete semantics (§6).
7. Record everything in this file, re-run the suite, flip the contract
   suite to live mode.
