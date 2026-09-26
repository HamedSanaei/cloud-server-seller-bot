# Leaseweb integration notes (M17-001 / M17-002)

Status: **adapter implemented, live account pending.** This file records
observations from a live Leaseweb account as credentials are provisioned,
mirroring `docs/hetzner/INTEGRATION_NOTES.md` and
`docs/iranian/INTEGRATION_NOTES.md`.

## 1. Auth

`X-LSW-Auth: <API-KEY>` on `https://api.leaseweb.com` (no `Bearer`
prefix). Key issued at `secure.leaseweb.com > Administration > API Keys`.

Observations: _(none yet — verify on first `GET /publicCloud/v1/regions`)_

## 2. Endpoints assumed (confirm against the live API)

| operation | assumed path |
| --- | --- |
| list regions | `GET /publicCloud/v1/regions` |
| list instance types | `GET /publicCloud/v1/instanceTypes?region=` |
| list images | `GET /publicCloud/v1/images?region=` (see live observation below) |
| list instances | `GET /publicCloud/v1/instances?region=&limit=&offset=` |
| get instance | `GET /publicCloud/v1/instances/{id}` |
| launch instance | `POST /publicCloud/v1/instances` (see launch contract below) |
| terminate instance | `DELETE /publicCloud/v1/instances/{id}` |
| start / stop / reboot | `POST /publicCloud/v1/instances/{id}/start\|stop\|reboot` |

Response envelopes are parsed defensively (bare list or
`{regions,instanceTypes,types,images,instances,data,items}` keys;
single-resource reads accept a bare object or `{instance: {...}}`).

Observations (live account, 2026-09-25):

- `GET /publicCloud/v1/regions` returns **ten** regions (`eu-central-1`,
  `eu-west-2`, `eu-west-3`, `ca-central-1`, `ap-northeast-1`,
  `ap-southeast-1`, `ap-southeast-2`, `us-east-1`, `us-west-1`,
  `us-west-2`), and `GET /publicCloud/v1/instanceTypes?region=` accepts all
  ten.
- The **image** catalog is GLOBAL, not region-scoped: every entry states
  `"region": null`, and `?region=` currently accepts only `eu-central-1`.
  Any other sellable region id is rejected with
  `HTTP 400 {"errorCode":"400","errorMessage":"Validation Failed",
  "errorDetails":{"region":["The value \"eu-west-3\" is not valid region.
  Valid regions are: eu-central-1"]}}`.
  `GET /publicCloud/v1/images` **without** the filter answers `200` with the
  same 19 `READY` images, so the CUSTOMER-facing read
  (`list_images`, used by the OS picker and create-time image validation)
  attempts the region-scoped request first and falls back to the global read
  when the request shape is rejected (warning logged).
  The fallback must never be used for ROUTING: it makes every credential look
  image-capable for every region, which moved eu-west-2's owner to the
  priority-first account and broke that region's refresh (the offer identity
  is `(provider, product, location)` and cannot express two owners). The
  catalog sync therefore calls the strict region-scoped probe
  (`probe_region_images`, no fallback); the accepted region id identifies the
  account's own Sales Organization location (north: `eu-central-1`,
  uk: `eu-west-2`).
  An unknown parameter name (`regionId`) is silently ignored and returns the
  unfiltered list, which is why the filter is never trusted as a
  region-scoping guarantee.
- Instance types state **no** `architecture` (50/50 blank in `eu-west-3`)
  while every image states `x86_64`; the storefront therefore only drops an
  image on a positive architecture conflict and never hides the catalog
  because a plan is silent.
- Image payload fields: `id`, `name`, `version`, `family`, `flavour`,
  `architecture`, `storageSize`, `state` (`READY`), `stateReason`, `region`,
  `createdAt`, `updatedAt`. The adapter keeps `id` and the customer label
  apart.
- Instance types carry prices under `prices` with the envelope's
  `_metadata.currency` (JPY/KRW/EUR observed), so hourly cost parsing needs
  the currency's own exponent (`JPY`/`KRW` are zero-decimal).
- `minDiskSize` and `storageTypes` exist on BOTH instance types and images,
  and they are the launch constraints, not display text. Observed
  `eu-central-1` facts: `lsw.m3.large` states `minDiskSize: 5` and
  `storageTypes: ["CENTRAL", "LOCAL"]`; Linux/FreeBSD images state 5 GB, some
  Enterprise Linux images (`ALMALINUX_8/10`, `ROCKY_LINUX_10`) state 10 GB,
  Windows images state 50 GB **and only `["CENTRAL"]`**. `rootDiskSize` must
  therefore satisfy the type AND the image floor (the adapter pins the larger
  of the two), and `rootDiskStorageType` must be accepted by both sides.

### Launch contract (`POST /publicCloud/v1/instances`)

Official schema (`publicCloud/components/schemas/launchInstanceOpts.yaml`,
Leaseweb `api-definitions`):

- REQUIRED: `region`, `imageId`, `contractType`, `rootDiskSize`,
  `rootDiskStorageType`, `type`.
- OPTIONAL: `reference`, `contractTerm`, `billingFrequency`, `sshKey`,
  `userData`, `marketAppId`.
- `rootDiskSize`: integer, **minimum 5** (Linux/FreeBSD) / **50** (Windows),
  **maximum 1000**.
- `rootDiskStorageType`: enum `LOCAL` | `CENTRAL`.
- There is **no `labels`** field on this endpoint.

Incident 2026-09-25 (production): the adapter sent `labels` and omitted both
root-disk fields, so Leaseweb answered
`400 {errorCode: "400", errorMessage: "Validation Failed"}` and the instance
was never created (`provider_server_id` stayed NULL, operation failed at
attempt 1). Two follow-on contracts now hold:

1. The launch body is exactly the documented schema — `build_create_body`
   validates the pinned `(size, storage type)` pair and can never emit an
   undocumented field.
2. The launch root disk is pinned INTO the accepted hourly contract: the
   fingerprint (version 2) carries `root_disk_size_gb` /
   `root_disk_storage_type`, derived at checkout from live provider facts
   (type minimum + image minimum + both storage-type sets). The create worker
   re-verifies those exact values against live facts and fails closed instead
   of guessing. Version-1 (pre-root-disk) contracts are still readable but
   cannot be re-created, so they fail with an actionable reason and the
   customer starts a new order — never a silent re-POST.
3. Exactly-once is unchanged and does NOT depend on provider labels: durable
   operation ledger (`server-create:<server id>`), deterministic `reference`
   (`srv-<server id>`), and a read-only `find_by_reference` correlation before
   any create/recovery.

A `400` now also preserves `errorDetails` in the safe one-line summary
(field + reason, redacted and bounded), because `errorCode`/`errorMessage`
alone are not actionable.

### Account capacity (`PC-2031`, "Customer limit reached")

Incident 2026-09-25 (production): a later create was accepted by the schema and
definitively refused with `400 {errorCode: "PC-2031", errorMessage: "Customer
limit reached"}` on the `sales-org-north` credential. That is a fact about the
ACCOUNT, not about the offer — the same plan/image/region may be perfectly
sellable through another credential.

- **No quota endpoint exists.** The official Public Cloud API schema has no
  quota/limit path (checked against `leaseweb/api-definitions`
  `publicCloud/paths`), so capacity cannot be read: it can only be LEARNED from
  a definitive refusal and remembered.
- `PC-2031` is classified as `LeasewebCapacityError`
  (`LeasewebValidationError` **and** the provider-neutral
  `ProviderCapacityError`): permanent for that POST (never auto-retried),
  still carrying error code + sanitized message + correlation id for admins.
- The refusal is persisted per `(provider_key, credential_account_id)` in
  `provider_account_capacity`, written as an atomic `INSERT ... ON CONFLICT DO
  UPDATE` so concurrent observations of one refusal end as ONE row with N
  observations. It gates NEW orders only: management/reconciliation of
  existing resources always resolves the account pinned on the resource.
- **Never a cross-account replay.** The accepted contract is pinned to one
  credential; swapping credentials after confirmation would break both the
  immutable fingerprint and exactly-once. A capacity signal therefore acts
  BEFORE checkout (catalog publication + a pre-checkout gate) so a *new*
  checkout can legitimately be pinned to a healthy account, and arrives with
  the offer's own fingerprint.
- Publication is provable or absent: an account is only handed a pair it
  PROVED read-only (region list + type list + image read). An inconclusive read
  (timeout/5xx/throttle) is never a verdict — it keeps the current owner and
  retires nothing; only a definitive "this credential cannot serve the pair"
  routes the pair away, and a limited account keeps its row unpublished instead
  of moving the offer.
- **Evidence state machine, never "time passed".** An account is always in
  exactly one of three states:
  - `limit_reached` — a definitive refusal inside its cooling window
    (the Leaseweb provider setting `cloud_account_limit_ttl_seconds`, default
    3600s, minimum 60);
  - `unknown_after_limit` — that window elapsed with NO proof that provider
    capacity came back. Time passing is not evidence (Leaseweb does not free a
    Sales Organization's limit because an hour went by), so the account stays
    out of NEW-order publication. Reads settle the stored row
    (`limit_reached` -> `unknown_after_limit`), which is why no caller can ever
    observe "the TTL expired, therefore eligible";
  - `healthy` — no refusal on record, or one of exactly two positive proof
    paths: an operator clear (`leaseweb cloud accounts clear --account <id>`,
    run after provider instances were actually removed) or a future read-only
    quota API whose semantics PROVE new-instance capacity. `list_regions` /
    `list_instanceTypes` / `list_images` / `list_instances` are not proofs
    (they prove authentication and catalog access only), and no billable create
    is ever issued as a capacity probe.
- **History is reconciled, exactly once.** A refusal that predates the capacity
  feature survives only as a failed provider operation's text
  (`operations.error`). `leaseweb cloud accounts reconcile [--dry-run]` — and
  the worker's startup pass — turn those into the same durable knowledge
  through the audited classifier (`is_capacity_exhausted`): an unrelated 400
  (image, region, validation) is never reinterpreted as capacity, no operation
  or server row is mutated, no provider call is made, and the provider
  operation key is the idempotency anchor (one evidence row per operation,
  ever). The 2026-09-25 incident's own operation
  (`server-create:5e4bf88c-...`) is recovered through exactly this path.
- **Publication reacts immediately.** The moment a refusal is durable, the
  hourly service refreshes NEW-order publication for that account: pairs
  another enabled account PROVES read-only are re-pinned to it through the same
  observation write the periodic sync uses, and the remaining pairs are
  unpublished (fail closed). The 15-minute catalog cycle stays the backstop;
  the offer row, its pricing provenance and any existing server keep their
  pinned account, and the refused POST is never re-sent.
- **One image-read semantics.** `list_images` (the customer OS screen) may fall
  back to the provider's GLOBAL image catalog when the region FILTER is
  rejected, but the capability question — doctor, catalog sync, checkout
  revalidation — is answered by one implementation
  (`region_images_verdict`): a rejected region filter is `unserved` (the
  global list is display-only), an empty region-scoped read is `empty`, and
  only a region-scoped read that lists usable images is `proven`. Routing and a
  billable create fail closed on anything but `proven`.
- `leaseweb cloud accounts doctor` prints the safe view (account state,
  capacity state and why, proven regions, visible instances, last
  code/correlation id — never key material).

## 3. Status vocabulary

`RUNNING` is confirmed by the documented launch response. The adapter maps
`running/started/active/available -> running`,
`creating/build/building/pending/provisioning/starting/rebooting -> building`,
`stopping/stopped/off/powered-off -> stopped`,
`terminating/deleting -> deleting`, `terminated/deleted -> deleted`,
`error/failed -> error`; anything else passes through lower-cased so the
reconciler contains instead of misclassifying.

Observations: _(none yet — capture exact `state` strings on first create)_

## 4. Prices

Leaseweb bills in **EUR**. The mapper reads `pricePerHour`/`pricePerMonth`
when the API exposes them. When it does not, the syncer accepts an explicit
operator price map (`LEASEWEB_PRICES_JSON`, e.g.
`{"lsw.m3.large": {"hourly": "0.12", "monthly": "72.00"}}`); rows ingested
that way are tagged `price-source:operator-price-list` in the plan
description so the margin report can tell them apart. There are no
hard-coded provider prices anywhere.

Observations: `prices.hourly` is the authoritative hourly rate and the
envelope's `_metadata.currency` is the only currency source (observed EUR for
the EU regions). No per-item currency exists in the payload.

## 5. Idempotency

The public API documents no `Idempotency-Key` header, so idempotency is
enforced platform-side: operation-ledger `operation_key` +
get-before-create name correlation + delete-treats-404-as success
(same pattern as ArvanCloud M15-002).

Observations: _(none yet)_

## 6. Capabilities

Phase 1 advertises **COMPUTE + POWER** only (the sellable minimum). Enable
REBUILD/SNAPSHOT/NETWORK only after the live endpoint shapes are confirmed
here.

## 7. Live smoke checklist (run when credentials exist)

1. `GET /publicCloud/v1/regions` — record §1/§2.
2. Launch a smallest type with a deterministic `reference`; record state
   transitions (§3) and launch timing.
3. Observe headers on a burst of reads (rate limits).
4. Read one instance type; record price fields (§4).
5. Terminate; confirm 404-after-delete semantics (§5).
6. Record everything here, re-run the suite, flip the contract suite to
   live mode.
