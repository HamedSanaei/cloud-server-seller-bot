# Customer REST API v1 (M14-001)

Status: **contract frozen**. The paths, the error envelope and the
idempotency rules below are pinned by
`tests/unit/test_api_v1_contract.py`; breaking them requires a `/v2`.

## Conventions

- Base path: `/v1`. All resources are versioned; nothing unversioned is
  added to this surface.
- Authentication: requests authenticate with a **bearer API token**
  (`Authorization: Bearer cpt_...`). Tokens are revocable, stored as
  SHA-256 hashes only (the plaintext is shown exactly once at creation),
  and carry a set of scopes; each resource family requires one scope and
  a token without it gets `403` / `forbidden`. The `x-platform-user`
  header requires `api_allow_header_identity` AND an `app_env` whose trimmed,
  case-normalized value is `development` or `test`; staging, production and
  other environment names cannot enable this fallback.
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
| `unauthorized`         | 401  | missing/invalid credentials |
| `forbidden`            | 403  | token lacks a required scope |
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

## Rate limits

Every `/v1` request is rate-limited per identity (the API token's own id -
distinct tokens of one user get independent buckets) with a sliding
window configured by the operator (`api_rate_limit_per_minute`, default
240/minute). Every response carries:

| header                   | meaning |
|--------------------------|---------|
| `X-RateLimit-Limit`      | requests allowed per window |
| `X-RateLimit-Remaining`  | hits left in the current window |
| `X-RateLimit-Reset`      | approximate unix epoch second when the window frees |

Exceeding the limit returns `429` + envelope code `rate_limited` with a
`Retry-After` header (seconds to wait).

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
| `GET  /v1/ssh-keys`                        | live   | `ssh_keys:read`; fingerprints only, no material |
| `POST /v1/ssh-keys`                        | live   | `ssh_keys:write`; `{name, public_key}` -> fingerprint |
| `DELETE /v1/ssh-keys/{key_id}`             | live   | `ssh_keys:write`; 204 |
| `GET  /v1/auth/tokens`                     | live   | `tokens:manage`; token metadata, never material |
| `POST /v1/auth/tokens`                     | live   | `tokens:manage`; requested scopes must be a subset of the authenticating token's scopes; plaintext shown once |
| `DELETE /v1/auth/tokens/{token_id}`        | live   | `tokens:manage`; revokes immediately (idempotent) |

## Admin surface (`/v1/admin`, RBAC + audited)

Admin reads require `admin:read`; mutations require `admin:write`. These
token capabilities are required IN ADDITION TO an active persisted platform
User holding the corresponding admin permission (application-layer RBAC via
`PermissionChecker`). A token scope never grants an admin role. Unscoped,
inactive, non-admin and unknown actors get `403 forbidden` without exposing
resources. Mutations require a reason and an idempotency key, and are audited
as ADMIN-actor events (the audit chokepoint rejects events without a reason).

| method & path                              | permission              | notes |
|--------------------------------------------|-------------------------|-------|
| `GET  /v1/admin/users?q&offset&limit`      | `admin:manage_users`    | substring search over username/email |
| `GET  /v1/admin/users/{user_id}`           | `admin:manage_users`    | full profile |
| `POST /v1/admin/users/{user_id}/status`    | `admin:manage_users`    | `{status, reason}`; audited |
| `GET  /v1/admin/audit?actor_id=` or `?resource_type=&resource_id=` | `admin:manage_settings` | read-only trail query |

Status-change keys are scoped to the acting admin and bind the target user,
requested status, and reason. The status and its immutable
`admin.user.status_changed` audit receipt commit in one transaction. An exact
replay returns the original status snapshot, not the user's current status,
and never reapplies the change or appends another audit event. For example,
replaying an activation after a later ban returns `active` while leaving the
user banned. Reusing the same actor/key with a different target, status, or
reason returns `409 conflict`; a failed audit write rolls back the status.

## Scopes

Stable scope strings (`modules/tokens/domain.py::TokenScope`):

| scope            | gates |
|------------------|-------|
| `catalog:read`   | catalog offers |
| `wallet:read`    | wallet balance |
| `servers:read`   | server list/detail |
| `servers:write`  | power actions (+ reserved delete) |
| `ssh_keys:read`  | ssh-key list |
| `ssh_keys:write` | ssh-key register/delete |
| `tokens:manage`  | token create/list/revoke |
| `admin:read`     | administrative reads, also requiring active admin RBAC |
| `admin:write`    | administrative mutations, also requiring active admin RBAC |

Token creation never expands authority: `tokens:manage` permits management
but does not authorize minting any other scope the parent token lacks.
Existing catalog/customer-only tokens receive no implicit admin scopes;
an authorized issuer must issue replacement tokens with explicit scopes.

Issued credentials, ownership and scope sets are immutable. Repository
lifecycle saves only advance usage and revocation; a stale authentication
save cannot clear a committed revocation. Authentication rejects a token
revoked between its hash lookup and lifecycle save.

## Web sessions

The `/web` panels reuse the same revocable API tokens in the
`platform_session` cookie and authenticate them on every request. Cookies
are `HttpOnly` and `SameSite=Lax`; they are also `Secure` outside the explicit
normalized `development`/`test` environments, which permit local HTTP.
The customer panel loads and displays servers only with `servers:read` and
wallets only with `wallet:read`; either scoped section works independently.
A token with neither scope gets `403` without reading either resource.
The admin panel additionally requires `admin:read` and active persisted
admin RBAC; missing authority redirects to login before dashboard reads.
Wallet amounts use the canonical integer-minor-unit formatter: IRT/IRR are
zero-decimal units, and large USD balances preserve every cent without float
conversion. The raw minor-unit balance remains visible for reconciliation.

## Stability policy

- Codes are never renamed or reused; new codes are appended.
- Response fields are additive only within v1.
- Path shapes are frozen; new resources join under `/v1/...`.
- The OpenAPI schema must always contain the pinned path set (tested).
