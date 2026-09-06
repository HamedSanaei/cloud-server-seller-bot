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
| list images | `GET /publicCloud/v1/images?region=` |
| list instances | `GET /publicCloud/v1/instances?region=&limit=&offset=` |
| get instance | `GET /publicCloud/v1/instances/{id}` |
| launch instance | `POST /publicCloud/v1/instances` |
| terminate instance | `DELETE /publicCloud/v1/instances/{id}` |
| start / stop / reboot | `POST /publicCloud/v1/instances/{id}/start\|stop\|reboot` |

Response envelopes are parsed defensively (bare list or
`{regions,instanceTypes,types,images,instances,data,items}` keys;
single-resource reads accept a bare object or `{instance: {...}}`).

Observations: _(none yet)_

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

Observations: _(none yet — confirm whether instanceTypes carry prices)_

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
