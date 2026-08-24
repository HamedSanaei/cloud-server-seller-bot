# Customer REST API v1 (M14-001)

Status: **contract frozen**. The paths, the error envelope and the
idempotency rules below are pinned by
`tests/unit/test_api_v1_contract.py`; breaking them requires a `/v2`.

## Conventions

- Base path: `/v1`. All resources are versioned; nothing unversioned is
  added to this surface.
- Identity: every request carries the acting user via the identity header
  (dependency seam: `api.v1.dependencies.get_current_user_id`). Requests
  without a valid identity get `401` + envelope code `unauthorized`.
- Ownership: application services enforce ownership. A foreign resource is
  indistinguishable from a missing one (`404` / `not_found`) — no
  existence leaks.
- Money: all amounts are integer minor units (never float), currency
  declared per object.

## Error envelope (the one true error shape)

Every failure returns:

```json
{
  "error": {
    "code": "<stable machine-readable code>",
    "message": "<human-readable detail>",
    "details": { }
  }
}
```

Stable codes (`api.v1.errors.ErrorCode`):

| code                   | HTTP | meaning |
|------------------------|------|---------|
| `validation_error`     | 400  | malformed body/query/header |
| `invalid_ssh_key`      | 400  | key material failed parsing |
| `unauthorized`         | 401  | missing/invalid identity |
| `not_found`            | 404  | unknown OR foreign resource |
| `conflict`             | 409  | state conflict (e.g. failed op) |
| `quota_exceeded`       | 409  | per-user limit reached |
| `action_not_allowed`   | 409  | power action invalid in state |
| `operation_in_progress`| 409  | another operation holds the server |
| `idempotency_required` | 428  | mutating request without a key |
| `rate_limited`         | 429  | client throttled |
| `not_implemented`      | 501  | contract reserved, wiring pending |
| `internal_error`       | 500  | unexpected failure |

## Idempotency

Every mutating endpoint (`POST`, `DELETE`) REQUIRES an `Idempotency-Key`
header:

- missing/blank -> `428` + `idempotency_required`;
- longer than 128 chars -> `400` + `validation_error`;
- the key joins the platform ledger-operation key, so retrying the same
  logical command can never double-act (a completed operation replays its
  outcome instead of re-executing).

Read endpoints (`GET`) never require a key.

## Resources (v1)

| method & path                              | status | notes |
|--------------------------------------------|--------|-------|
| `GET  /v1/catalog/offers`                  | live   | enabled offers only |
| `GET  /v1/wallet`                          | live   | balance in minor units |
| `GET  /v1/servers`                         | live   | paged list, owner-scoped |
| `GET  /v1/servers/{server_id}`             | live   | foreign id = 404 |
| `POST /v1/servers/{server_id}/actions/{action}` | live | `power-on` \| `power-off` \| `reboot`; 202 + replay/requeue flags |
| `DELETE /v1/servers/{server_id}`           | 501    | reserved; saga wiring ships with the REST order/delete phase |
| `POST /v1/servers`                         | -      | not exposed until order saga phase |
| `GET  /v1/ssh-keys`                        | live   | fingerprints only, no material |
| `POST /v1/ssh-keys`                        | live   | `{name, public_key}` -> fingerprint |
| `DELETE /v1/ssh-keys/{key_id}`             | live   | 204 |

## Stability policy

- Codes are never renamed or reused; new codes are appended.
- Response fields are additive only within v1.
- Path shapes are frozen; new resources join under `/v1/...`.
- The OpenAPI schema must always contain the pinned path set (tested).
