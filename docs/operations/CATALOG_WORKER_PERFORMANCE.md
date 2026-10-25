# Catalog worker performance: local investigation and rollback

## 2026-10-25: Ordering/Public Cloud namespace follow-up

This follow-up is a **source-only candidate, not an authorized deployment**.
Read-only inspection at 02:14:34 UTC found the running image at `b699335`,
including merged PR #6, rather than the older `93f1819` reported initially.
No provider request, production write, restart, cache invalidation or statistics
reset was performed during this investigation.

### Confirmed causes and boundaries

- `cloud_sync.sync_all()` legitimately persists lower-case Public Cloud regions
  into the shared provider-location display table. Ordering
  `sync_products()` read every Leaseweb location and prior route;
  `merge_candidates()` uppercased them without checking their namespace.
  `_discover_account()` then probed and persisted those identifiers for each
  credential. Production contained 35 display rows: 15 datacenters, 10 Cloud
  regions and 10 upper-case copies. This fed 25 candidates to each of two
  accounts, including 20 invalid cross-family probes per cold discovery round.
- `extract_location_codes()` could also extract `WEST-3` from `EU-WEST-3`.
  Candidate merging, provider hints and direct Ordering probes now require
  whole datacenter identifiers. This is an open-ended adapter grammar, **not
  membership in `KNOWN_VPS_DATACENTERS`**: new halls such as `PAR-99` remain
  discoverable. Cloud discovery and its lower-case region IDs are unchanged.
- Historical display rows and routes remain stored. They no longer authorize
  Ordering discovery. Existing offer billing models already distinguish
  monthly from hourly reconciliation, so no schema migration is needed.
- Leaseweb's own inconsistency remains: the location-specific LIST includes
  `VPS02_1` at `FRA-10`, while DETAIL returns 400 “not available”; the same
  occurs for `LON-11` under its UK sales organization. Omitting contract/billing
  parameters did not help in the owner-confirmed tests. LIST still controls
  membership; failed optional enrichment neither invents a price nor removes
  a valid LIST observation. Checkout/fulfillment checks are unchanged.
- PR #6 already supplied discovery-only Ordering-400 caching. This follow-up
  preserves exact allowlisted error subtype, status, Retry-After and minimal
  region-filter proof through cache replay. All request filter parameters
  participate in hashed identity. Old entries without sufficient evidence
  are reprobed once. No provider message/body or credential is cached.
- A generic mention of a region is not proof that a filter is unsupported.
  Only structured `400/errorDetails.region` or the narrowly recognized
  provider message qualifies. Cached failures remain typed failures in
  reports; only duplicate replay logs become DEBUG. Fresh/reprobe failures
  keep their normal severity. A global image list still does not prove
  region/account fulfillment capability.

### Production baseline, not post-fix measurements

The catalog interval was 900 seconds, timeout 600 seconds, with observation
batching and negative caching enabled (3,600-second negative TTL).

| Observed worker metric | Value |
| --- | --- |
| Completed catalog jobs / summed wall time | 2 / 208.39 s |
| Inclusive catalog-job process CPU | 41.27 s |
| Cgroup CPU since container start | 179.49 s over 983.80 s (~18.2% of one CPU average, not peak) |
| Catalog cursor executions / commits | 624 / 292 |
| Provider-sync cursor executions / commits / read cleanup rollbacks | 586 / 285 / 178 |
| Changed / unchanged provider observations | 0 / 536 |
| Economic price changes / evidence refreshes | 0 / 2 |
| Discovery-cache hits / misses | 49 / 92 |
| Ordering LIST 400 / DETAIL 400 | 22 / 24 |
| Cloud image 400 / image 200 | 0 / 10 |
| Worker cgroup throttled periods / periods | 117 / 4,682 |
| Worker cgroup throttled time / CPU quota | 4.80 s / 1 CPU |

The counters reset with the worker. The owner's earlier 1,008 DETAIL-400,
924 LIST-400 and 72 image-400 observations are a different measurement window,
not a directly comparable after/before pair. Stage CPU is inclusive and can
overlap capacity recovery; it is not exclusive attribution.

`pg_stat_statements` had been reset at 01:36:29 UTC. Its cumulative window
includes deployment/readiness and other jobs, not only scheduled catalog work.
Normalized projections below are abbreviated; query IDs identify exact rows.

| Query ID | Normalized SQL shape | Calls | Total execution ms | Mean ms |
| --- | --- | --- | --- | --- |
| `-1335413168768839628` | `SELECT <offer columns> FROM sellable_offers ORDER BY provider_key, location_id, product_id` | 18 | 3121.24 | 173.402 |
| `-1953548502095612218` | `SELECT <offer columns> FROM sellable_offers WHERE (provider_key,billing_model,product_id,location_id) IN (...)` | 10 | 362.72 | 36.272 |
| `5037813231549613731` | `SELECT <offer columns> FROM sellable_offers WHERE id=$1::UUID` | 854 | 219.27 | 0.257 |
| `1194180340443044238` | `UPDATE sellable_offers SET pricing_metadata=$1::JSONB, updated_at=CURRENT_TIMESTAMP WHERE id=$2::UUID` | 729 | 49.89 | 0.068 |
| `-7064786512269629625` | `SELECT <location columns> FROM provider_locations WHERE provider_id=$1::UUID AND location_id=$2::VARCHAR` | 203 | 5.90 | 0.029 |

The full ordered offer read maps to `list_all()`, used both by monthly discovery
and periodic capacity counting. Monthly discovery now uses
`available_refs(provider, billing_model=monthly)`, selecting only account,
product and location. Capacity/CLI counts use `published_candidates(hourly)`
and retain the exact existing currency/provenance gate, across all providers.
Do not replace that gate with an unaudited SQL COUNT.

Metadata previously incurred a session/provider lookup/location lookup/commit
per account and candidate, plus a second pass over resolved halls.
`SqlAlchemyLocationRepository.upsert_batch()` now resolves providers once,
locks identities in order and writes only actual display-metadata changes.
Ordering persists the union once. Offer/route batches, pricing audit and
necessary freshness timestamps remain; no billing cron was changed.

### Isolated smoke and regression evidence

Five alternating repetitions against disposable local PostgreSQL 18, using the
exact deployed repository and the 25/15 candidate sets captured above. This is
the **warm metadata component**, not an end-to-end catalog/CPU benchmark.
Historical rows were verified retained.

| Metadata component metric | Deployed implementation | Candidate |
| --- | --- | --- |
| Cursor executions | 112 | 2 |
| Commits | 56 | 0 |
| Read-session cleanup rollbacks | 0 | 1 |
| Median wall time | 165.42 ms | 3.35 ms |
| Median process CPU | 93.75 ms | Below Windows clock resolution |

The candidate CPU samples rounded to zero; this does not mean zero CPU usage.
The one rollback closes a read-only transaction, not a failed catalog write.

HTTP estimate with stable account scope and the existing one-hour TTL:
contamination contributes 20 cold LIST attempts per TTL window today and zero
after separation. Without PR #6's cache it would contribute up to 80 attempts
over four 15-minute runs. The 24 optional-detail failures remain provider-side
and are bounded, not “fixed”; successful catalog reads remain fresh. Real
rollout measurements must account for one-time cache-key/legacy-entry misses.

Targeted validation covers namespace separation and new datacenters, legacy
rows/routes, both sales organizations, optional DETAIL 400, 403, transient and
ambiguous failures, negative expiry/invalidation, pricing evidence, unchanged
metadata timestamps, manual price/disable state, and byte-for-byte retained
order/server snapshots across repeated real-PostgreSQL syncs.
Raw local benchmark files remain ignored under `.ci-artifacts/`.

**No claim of solved Production CPU:** after an explicitly approved deployment,
compare at least a cold round and a full warm TTL window on the exact image:
endpoint/status deltas, cache outcomes, catalog wall/inclusive CPU, cgroup
throttling deltas, SQL calls/time, transactions, changed/unchanged observations
and unchanged customer/financial contracts. Do not clear Production caches or
reset PostgreSQL statistics merely to make the comparison easier.

## Historical f609 investigation

The remaining sections describe the earlier baseline and its original
measurements; they are not current Production facts.

## Scope and evidence boundaries

Baseline: `f609f617e4223c85fec2073371c9dbfaf637a3ff`, branch `staging` in
`cloud-server-seller-bot`. At investigation time it was 13 commits ahead of
GitHub `main` (`32dfb56d9d04adec48a068066b35612ccc7bc01c`). The old local
checkout and the unrelated `HamedSanaei/cloud` website were not used or changed.

The owner-authorized read-only production inspection confirmed running images
at this baseline. A bounded 90-minute log sample contained 248 validation and
19 forbidden matches, **not a count of HTTP requests**. A host sample had 75%
CPU steal; infrastructure contention remains independent of application work.
Exit 137 with `OOMKilled=false` does not establish OOM. The reported 191.22%
worker / 58.82% PostgreSQL peak has not been reproduced or attributed
quantitatively to individual jobs. No production sync or load experiment ran.

Production PostgreSQL statistics were cumulative since an unknown reset:
`sellable_offers` had 250,554 updates / 5,475,917 index scans and
`provider_routes` 124,568 updates / 252,524 sequential scans. These are context,
not per-sync measurements or proof that every rollback is a business failure.
`pg_stat_statements` was absent; it was not installed, enabled or reset.

## Execution trace and proven amplification

1. Cron/startup/manual coordinator entry:
   `worker/settings.py:1060` (`catalog_auto_sync`) and `:1079`
   (`run_catalog_auto_sync_once`); schedules are built in that file. The
   coordinator acquires the session-bound PostgreSQL advisory lock at
   `modules/offers/auto_sync.py:261`, implemented in
   `modules/catalog/repository.py:350`. This still excludes overlapping catalog
   runs. Unit scheduling tests and real concurrent coordinator tests cover it.
2. Monthly source `providers/leaseweb/auto_sync.py:33` delegates to
   `ordering_sync.py:459`, then `:1044` account discovery and `:1158` optional
   product detail. Candidate regions combine seeds, persisted locations,
   prior route evidence and discovered products. The old failure memo lived
   only in that discovery, so new adapters/jobs retried identical failures.
3. Hourly source `providers/leaseweb/cloud_auto_sync.py:30` delegates to
   `cloud_sync.py:376` (`sync_all`): account regions, per-region types and images,
   current owner/capacity evidence, provider observation persistence, then
   availability reconciliation. Capacity inventory recovery is also a separate
   periodic job (`worker/settings.py:134`); it is not catalog publication.
4. The original `upsert_from_provider` opened a session, selected/locked,
   committed and refreshed each product. Its compatible single-row entrypoint
   remains at `modules/offers/repository.py:410`; the new bounded writer starts
   at `:459`. The baseline observation benchmark proves 2,000 cursor calls
   even when 1,000 observations are unchanged, and 3,000 on cold insertion.
5. Coordinator pricing and publication previously re-read every verified
   product individually. They now use grouped scoped reads, short pricing
   transactions and a **fresh** read after pricing, preserving publication CAS.
   See `modules/offers/auto_sync.py:545`, `:878`, `:933` and
   `modules/offers/repository.py:1017`.
6. Routing previously used one read/transaction per observation.
   `modules/provider_routes/repository.py:158` now batches 100 evidence rows
   with the existing unique key. Failed probes retain last-known products;
   older observations cannot overwrite newer evidence. A real PostgreSQL test
   observes one cursor call/commit for 100 routes. Location seeds are now read
   once per monthly run, not once per credential account.

The above mechanisms are demonstrated locally. Their share of the historical
production CPU peak remains unproven until an approved rollout is measured.

## Cache, retries and safety policy

`providers/leaseweb/discovery_cache.py` is opt-in through a ContextVar scope
around catalog discovery and read-only capacity recovery. Checkout and customer
operations do not inherit this scope. Mutations never use the cache or new
retry behavior. Normal mutation unknown-outcome/idempotency handling is intact.

- Redis namespace: `catalog:leaseweb:negative:v1:`; maximum 4,096 admitted
  entries, atomic Lua admission and expiry index. No global Redis flush.
- Key identity hashes account, credential fingerprint, API base/family,
  endpoint, region and relevant filter/pagination parameters. Product details
  include their product path. Redis values contain safe classification and
  scope only, not raw provider responses or credentials.
- Definitive authentication, forbidden, recognized region-filter rejection
  and optional VPS-detail 404 failures are cached for one hour by default.
  Arbitrary validation errors are **not** interpreted as invalid regions.
- A cache hit raises a typed failure. Authentication/permission failures
  remain visible in health/sync outcomes. Failure never means empty inventory,
  free capacity, permission to sell or proof of an eligible account.
- Credential rotation changes key identity immediately. Account/region epochs
  fence in-flight results during targeted invalidation. Successful reads clear
  the previous failure. TTL and explicit invalidation bound recovery delay.
- Read-only discovery transport owns a single maximum-two-retry budget for
  timeout, 5xx and 429; bounded jitter and Retry-After are applied. Long delays
  become shared cooldowns rather than sleeping or retrying early. Retry-After
  is bounded at 24 hours; transient cooldown starts near 30 seconds and grows
  to about 300 seconds before an explicit longer Retry-After takes precedence.
- Redis errors/corrupt records fall back to genuine provider reads, never to
  invented success. A five-second local backend circuit breaker avoids a
  connection timeout on every subsequent product after a backend failure.
- Discovery is limited to two slots per worker process, one per account,
  shared by policy instances. The catalog advisory lock handles catalog
  replicas. There is **no distributed singleflight across inventory workers**:
  simultaneous first misses in different processes can duplicate a read.

Observation and pricing batches have at most 100 rows, sorted locks, full
rollback before single-row recovery, explicit persistence failures and no
pricing/retirement from incomplete persistence. Existing account adoption,
operator disable, manual selling prices, billing-model/exact-rate guards and
FX provenance remain. Batch pricing additionally compares owner and prior
pricing evidence. FX evidence refreshes are not dropped just because the
economic price is unchanged; metrics count these separately. Accepted order
snapshots, wallets, ledger entries and service contracts are never rewritten.

## Measured benchmarks

Five repetitions per arm/scenario, same synthetic 1,000-product dataset and
settings, alternating execution order. PostgreSQL 18 ran locally in WSL using
random scratch schemas with explicit loopback opt-in. No production database,
provider API or billable operation was used. Raw samples are ignored artifacts
under `.ci-artifacts/`; benchmark scripts are source-controlled candidates.

### Observation persistence

`scripts/benchmark_catalog_sync.py`, archived baseline repository versus batch
repository. Wall values are median [min, max], seconds. SQL means driver
**cursor executions**, not TCP packets or individual server executions inside
`executemany`; commits/rollbacks are counted separately. The SQL labels in
metrics have the same documented cursor-execution meaning.

| Scenario | SQL before → after | Wall before | Wall after | Worker CPU median before → after | PG backend CPU median before → after |
| --- | --- | --- | --- | --- | --- |
| Cold | 3000 → 20 | 5.518 [5.164, 5.596] | 0.955 [0.905, 1.015] | 3.266 → 0.422 | 0.600 → 0.040 |
| Warm unchanged | 2000 → 10 | 4.610 [4.244, 4.961] | 0.141 [0.106, 0.157] | 2.766 → 0.109 | 0.520 → 0.010 |
| Cost/rate change | 3000 → 30 | 5.744 [5.547, 6.909] | 0.681 [0.646, 0.709] | 3.609 → 0.188 | 0.670 → 0.040 |
| One rejected observation | 2999 → 329 | 5.716 [5.279, 7.475] | 1.206 [1.182, 1.420] | 3.688 → 0.516 | 0.670 → 0.110 |

SQL cursor calls fell 89.0–99.5%, exceeding the 70% criterion on this workload.
Physical network round-trip counts were not instrumented; do not equate a
cursor call or executemany batch with a measured TCP round trip. Necessary
server-side row updates still occur for genuine cost/evidence changes.

Worker CPU min–max before/after: cold 3.125–3.531 / 0.391–0.469;
warm 2.266–3.031 / 0.094–0.141; changed 3.297–4.266 / 0.141–0.266;
partial failure 3.266–4.453 / 0.469–0.719 seconds. PostgreSQL backend CPU
min–max: cold 0.57–0.63 / 0.03–0.04; warm 0.49–0.56 / 0.01–0.02;
changed 0.63–0.88 / 0.03–0.04; partial 0.59–0.96 / 0.10–0.13.
PG values measure the dedicated one-slot backend through `/proc`, not the
whole cluster; resolution is 0.01 seconds. Windows process CPU is quantized.

All successful cases use 1,000 → 10 commits and 1,000 → 0 rollbacks.
Partial failure uses 999 → 108 commits and 1,000 → 101 rollbacks, with exactly
one failed observation in both arms and the same retained catalog facts.
Read-session cleanup accounts for baseline rollbacks, not 1,000 failed sales.

### Coordinator, pricing and publication

`scripts/benchmark_catalog_pipeline.py` loads both repository and coordinator
from the baseline archive. Both arms acquire the real PostgreSQL advisory lock,
persist observations, calculate real audited USD prices and execute publication
CAS against PostgreSQL. Provider discovery and the sync-status repository are
stubbed; region/location/provider-network work is not included in these timings.
Warm/change/failure runs start from the same seeded, valid published catalog.

Five repetitions per scenario. SQL is cursor calls; seconds are median [range].
Economic/spec facts, native currency/exact rates, owner, flags and actual
provenance-gated sellability are compared for all 1,000 offers every repetition.
IDs and evidence timestamps necessarily differ across isolated runs and are not
treated as economic differences. **All 20 arm-pair comparisons passed.**

| Scenario | SQL before → after | Wall before | Wall after | Worker CPU median before → after |
| --- | --- | --- | --- | --- |
| Cold | 11004 → 3056 | 19.738 [19.156, 20.863] | 6.809 [6.579, 6.972] | 11.984 → 3.844 |
| Warm unchanged | 7004 → 46 | 14.017 [13.430, 14.096] | 0.909 [0.877, 1.024] | 8.547 → 0.656 |
| Cost/rate change | 8004 → 66 | 14.724 [13.781, 16.211] | 1.018 [0.935, 1.142] | 8.813 → 0.734 |
| One rejected observation | 3001 → 331 | 5.416 [5.210, 6.254] | 0.889 [0.801, 0.971] | 3.141 → 0.516 |

Cursor-call reductions: 72.2%, 99.3%, 99.2%, 89.0%, respectively. CPU min–max
before/after: cold 11.344–13.219 / 3.813–4.125; warm 8.078–8.781 /
0.641–0.734; changed 8.281–10.219 / 0.578–0.859; partial failure
2.953–3.766 / 0.453–0.578 seconds. Process CPU here is isolated to one benchmark
job, not a claim about CPU ownership during concurrent production jobs.

Commit counts: cold 3000 → 1020; warm/changed 2000 → 20; partial 999 → 108.
Rollback counts: cold 5003 → 1005; warm/changed 4003 → 5; partial 1001 → 102.
Pricing evidence is still refreshed in the warm case. Cold publication retains
its per-row CAS, explaining most remaining cursor calls. Exact TCP round trips
and whole-cluster CPU were not measured by this pipeline benchmark.

### HTTP discovery

`scripts/benchmark_catalog_discovery.py`: archived transport, MockTransport
with the same synthetic 2 ms response latency, fake credentials and
fakeredis 2.40.0 Lua. Four logical runs spaced 900 seconds apart within TTL,
three distinct definitive failures and one changing successful inventory.

Cold: 4 → 4 HTTP attempts. Warm four-run workload: 16 → 7 attempts;
all nine repeated failing attempts removed, all four successful inventory
reads retained. Exception-type/output parity passed in every repetition.
Wall median 0.265 → 0.141 seconds; ranges 0.249–0.311 / 0.108–0.170.
Worker CPU medians were 0.0625 → 0.046875 seconds with overlapping ranges
0.03125–0.09375 / 0.03125–0.125 and coarse clock resolution: **no robust
HTTP-workload CPU reduction is claimed**. This is emulated HTTP/cache behavior, not Leaseweb RTT or a
test against the deployed Redis server.

## Operation, observability and rollback

New canonical settings:

```toml
[storefront.catalog_sync]
batch_enabled = true
probe_cache_enabled = true
probe_cache_ttl_seconds = 3600

[worker]
metrics_enabled = true
metrics_port = 9101
```

Worker metrics use the worker's registry and private container-network port
9101, **no published host port**. API `/metrics` is not the worker registry.
The exporter is closed on worker shutdown. Scraping still needs operator
configuration after approved deployment; no production monitoring was changed.

Metrics include actual Leaseweb HTTP attempts (including retries/raw credential
checks), attempt latency, cache hit/miss/backend error, discovery slots, SQL
cursor executions/transaction ends, changed/no-op rows, economic reprices,
evidence refreshes, phase/job wall time and inclusive process CPU. Nested phase
SQL counts roll up to `stage="catalog_sync"`; do not sum the parent with its
children. Process CPU during overlapping async jobs is not exclusive job CPU;
use active-job/slot gauges to interpret overlap. SQL text/parameters, account
IDs and credentials are not metric labels.

Generic `max_jobs=20`, job timeout 120 seconds and existing role-specific
workers were not changed. Catalog timeout defaults to 600 seconds, clamped to
its cadence; capacity inventory normally runs every three minutes. Startup
and cadence can coincide with billing/provisioning work. Local discovery caps
and lock exclusion were chosen first. Further worker sharding or changing
global concurrency requires post-rollout resource evidence, not guesswork.

Future **separately approved** rollback:

1. Set `batch_enabled=false`, `probe_cache_enabled=false` to restore the
   compatible per-row persistence/pricing path and bypass negative caching.
2. If necessary, invalidate only the affected scope using
   `python -m cloud_platform.cli catalog probe-cache invalidate --account <id>`
   with optional `--region <exact-region>`; no public API or Redis FLUSH.
3. Disable worker metrics if its internal port conflicts. Never publish it on
   the public network to make scraping convenient.
4. If required, redeploy the baseline image `f609f61` through the normal
   approved pipeline. No financial migration or data rollback is needed.

During the investigation/benchmark phase, no commit, push, deploy, production
configuration/data mutation, container start/stop or provider mutation was
performed. Subsequent owner approval separately authorized the five catalog
configuration values, commit and staging push, including its automatic deployment.
The production TOML update preserves unrelated values and ownership/mode and
retains a private server-side backup. The three pre-existing MongoDB health files
remain outside this change and were preserved byte-for-byte.

## Validation and remaining risks

Reproduce from the repository root using an already-running **local scratch**
PostgreSQL and `CLOUD_PLATFORM_TEST_POSTGRES_URL` supplied privately. The test
helper rejects non-loopback URLs and never falls back to the app database.
Archive extraction below targets a new ignored directory, not a checkout:

```powershell
git archive -o .ci-artifacts/catalog-baseline-f609.zip f609f617e4223c85fec2073371c9dbfaf637a3ff
Expand-Archive .ci-artifacts/catalog-baseline-f609.zip .ci-artifacts/catalog-baseline-f609
uv run python -m scripts.benchmark_catalog_sync --baseline-repository .ci-artifacts/catalog-baseline-f609/src/cloud_platform/modules/offers/repository.py --records 1000 --repetitions 5
uv run python -m scripts.benchmark_catalog_pipeline --baseline-root .ci-artifacts/catalog-baseline-f609 --records 1000
uv run --with 'fakeredis[lua]==2.40.0' python -m scripts.benchmark_catalog_discovery --baseline-transport .ci-artifacts/catalog-baseline-f609/src/cloud_platform/providers/leaseweb/transport.py
uv run --with 'fakeredis[lua]==2.40.0' pytest tests/live/test_postgres_catalog_batch.py tests/unit/test_catalog_discovery_cache.py tests/unit/test_catalog_probe_redis.py tests/unit/test_catalog_runtime_controls.py -q -o addopts=''
uv run python scripts/verify_ci.py --static
uv run python -m compileall -q src
uv run python -c "import cloud_platform.api.app, cloud_platform.bot.main, cloud_platform.worker.settings"
```

Do not overwrite an existing archive/extraction or use production connection
strings. The benchmark code creates/removes only its own random scratch schema.
Emulator packages are an explicit uv overlay; no application dependency/lock
change was made. Detailed local samples and command logs remain ignored in
`.ci-artifacts/catalog-*-benchmark*.json` and `.ci-artifacts/catalog-*.log`.

- 1,651 targeted unit regressions passed: Leaseweb, offers, catalog, hourly
  billing/checkout, financial boundaries, renewal, website bridge, config,
  worker queues/jobs, metrics, provider routes and CLI contracts. These test
  the affected financial/provider/publication interfaces, not the full release.
- Nine real PostgreSQL tests cover no-op/cold batching, rollback/recovery,
  owner repinning, pricing parity/provenance rollback, manual-price row-lock
  races, route inventory preservation and real concurrent coordinator locking.
- An additional 231-test run covering the PostgreSQL tests, REST v1/provider
  contracts, payment settlement/reconciliation, ordering/location metadata and
  CAS passed. Another 168 FX/USD pricing regressions passed. Twenty-one final cache,
  Lua-emulator and runtime-control tests passed, including field-aware rejection
  classification so an image error mentioning a region is not cached as an
  invalid region filter, and case-insensitive credential-header rotation.
  These runs overlap; their counts are not summed.
- Cache policy/Lua emulator tests cover scope, TTL/rotation/invalidation,
  admission limit, corrupt/offline Redis, timeout, 429/503 Retry-After,
  concurrency, actual retry attempt counts and mutation exclusion.
- Ruff, format, mypy and compile/import smoke are local checks. Full release
  suite and GitHub CI were not run; no push was authorized.
- Repository gate drift: current `verify_ci.py --staging` expands to the full
  release/coverage lane, contrary to AGENTS.md and this task's approved scope.
  The initial invocation stopped at mypy errors, which were then repaired;
  `verify_ci.py --static` passed. The release-expanding script was not changed
  or falsely reported as passing. The approved cheap checks run separately.
- Redis was validated using Lua emulation, not a live Redis daemon. Validate
  the deployed standalone Redis version and scraper reachability after approval.
  Redis Cluster multi-key Lua is not supported by this namespace layout.
- Cache TTL can delay recognition of newly granted provider access; targeted
  invalidation/credential rotation provide the explicit recovery path.
- First misses can overlap across independent processes. Publication still
  retains single-row CAS writes; remaining location/retirement/state writes
  have not all been batched. No zero-write or whole-system CPU claim is made.
- Root-cause attribution of the production peak, host-steal remediation and
  production latency/error counts require a separately authorized rollout and
  before/after observation. Local benchmarks cannot establish those facts.
