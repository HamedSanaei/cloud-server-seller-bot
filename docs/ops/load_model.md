# Load model (M16-001)

Acceptance: **user/order/job/provider assumptions documented.**

The assumptions live twice: as prose here and as executable values in
`src/cloud_platform/planning/load_model.py`. The tests
(`tests/unit/test_load_model.py`) keep the two honest - mixes must sum to
1, derived rates must match hand computation, provider budgets must keep
the mandated headroom.

## 1. Users (`UserPopulation`)

| assumption | value | why |
|------------|-------|-----|
| registered | 10 000 | Telegram-first launch scale; cheap to onboard |
| daily active | 1 500 | ~15% DAU/MAU-typical for infra utilities |
| peak concurrent | 120 | evening burst; drives pools/connections, not averages |

## 2. Orders / actions (`OrderMix`)

Fractions of ALL user actions (must sum to exactly 1):

| action | fraction | rationale |
|--------|----------|-----------|
| server_read (list/detail) | 0.45 | status checking dominates a bot UX |
| catalog_read | 0.20 | browsing offers/prices |
| wallet_read | 0.15 | balance checks before/after actions |
| power_action | 0.12 | on/off/reboot - the common mutation |
| ssh_key_op | 0.03 | occasional key management |
| server_create | 0.02 | deliberate, wallet-gated |
| snapshot_op | 0.01 | scheduled + manual |
| server_delete | 0.015 | rare, confirmation-gated |
| rebuild | 0.005 | rare, confirmed destructive |

Read-heavy by design; every mutation carries an idempotency key.

## 3. Jobs (`JobProfile`)

| job | cadence | notes |
|-----|---------|-------|
| billing tick | 60 s | metering evaluates servers hourly per quantum; the tick spreads the hour's work |
| reconciliation sweep | 300 s | provider-vs-ledger drift repair |
| outbox flush | 5 s | transactional-outbox drain keeps event lag tiny |
| worker poll | 2 s | operation claim loop (also wakes on enqueue) |

## 4. Providers (`ProviderCapacity`)

The Hetzner Cloud API allows ~3600 write requests/hour per project.
Policy: use at most **50%** of any published limit at modeled peak
(`HEADROOM_FACTOR` = 0.5) - reconciler sweeps after incidents and catalog
syncs need burst headroom inside the same window.

## 5. Derived numbers (checked by tests)

- steady arrival: `daily_active * 40 actions/day / 86400 ≈ 0.69 rps`
- burst arrival: all 120 peak-concurrent users act within one
  `PEAK_ACTION_SPREAD_SECONDS = 60` window → `2.0 rps`
- provider-bound writes at burst: create+delete+power+rebuild+snapshot
  fractions of the burst rate ≈ `2.0 × 0.17 = 0.34 rps`
- Hetzner budget: `3600/h × 0.5 / 3600s = 0.5 rps ≥ 0.34 rps` ✓

If an assumption changes, change it here AND in `load_model.py`; the
tests fail loudly when prose and code diverge.
