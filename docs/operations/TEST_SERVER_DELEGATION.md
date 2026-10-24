# Reserved test-server access (console)

Ownership is not delegation. Never change `servers.user_id`, the provider-account
pin, operator funding, price, wallet, invoice, order or subscription to make an
operator test asset visible. Migration **0061** adds an independent access table.
The migration creates no grants and imports no provider resources.

The console advertises `website-server-delegation:v1`. The updated Cloud bridge
requires that feature and an unauthorized/no-bearer probe of the new grant route
before cutover, so the platform must be published before the matching website.
The probe cannot grant access or touch any real resource.

## Policy

Only a currently configured, active Telegram Super Admin with `ADMIN_WRITE`
may grant or revoke access. Mutation rechecks persisted role and the enabled
superadmin assignment under the existing reconciliation lock, and locks the
server, recipient and grant. Audit and grant commit together. Keys are scoped to
the actor and bound to all request facts; a conflicting replay fails closed.
Replaying an old grant after revocation does not reactivate it.

The delegate must be an active platform user with a Telegram identity. Resolve
that UUID from the website's existing, verified `platform_links` relation for the
requested account; do not infer identity by equal email, edit the link, mint a
token for another user, or use a legacy web server ID. Use a real authenticated
superadmin session/token for grant creation, never impersonate the delegate.

This phase supports **explicit read-only delegation**: `view` is required;
`refresh`, `traffic`, `ip_list`, `monitoring` are optional. No write scope was
selected by the owner. Power, reinstall/delete, credential/console access,
SSH-port, IP mutations, commercial renewal and wallet controls cannot be granted.
Existing superadmin management remains unchanged. Reserved identity AND operator
funding checks remain mandatory on every access, including historical owners.
Account freeze/revoke or resource-binding drift removes access. Revocation is
still allowed after drift, so stale grants can be explicitly retired.

## Approved activation steps (not performed by source implementation)

1. Obtain release/migration approval and validate a recoverable backup. Run the
   exact opt-in PostgreSQL regression below against a scratch loopback DB, or
   in CI, before applying migration 0061. Do not create another production stack.
2. Check the existing resource is registered using the canonical provider ID/IP
   constants in `modules/servers/test_identity.py`. If absent, use the existing
   authenticated superadmin `POST /v1/console/admin/test-server/register` with an
   idempotency key. That endpoint only reads provider inventory and imports a
   verified local record; it must not order/create or modify a VPS. It refuses
   ambiguous credential-account ownership or identity/funding mismatches.
3. Capture its returned **platform server UUID** and the target's linked
   **platform user UUID**, after checking both identities. Do not put real
   account identifiers, emails, tokens or host configuration into this public
   repository or test fixtures.
4. Grant with a configured superadmin token and a fresh idempotency key:

   `POST /v1/console/admin/servers/{platform_server_uuid}/delegations/{platform_user_uuid}`

   Body: `{"operations":["view","refresh"],"reason":"Approved test visibility"}`.
   The Cloud backend permits this route under `/api/platform/...` and forwards
   only the caller's server-owned linked token. It cannot elevate an ordinary
   website admin or user into a canonical superadmin.
5. Using the target's own authenticated linked account, GET `/v1/console/servers`
   and `/v1/console/servers/{platform_server_uuid}`. Check My Servers renders the
   row and sends management/history requests using this UUID. Verify forbidden
   requests fail without creating command intents or touching providers.
6. Revoke with a fresh key:

   `POST /v1/console/admin/servers/{platform_server_uuid}/delegations/{platform_user_uuid}/revoke`

   Body: `{"reason":"Approved test access ended"}`. Verify the next list is empty
   for this server, detail/history/credentials fail, old grant replay does not
   restore access, and the owner plus other superadmins still have access.
7. Compare owner/funding/price fields and financial row counts before/after.
   No Leaseweb order, invoice, subscription or wallet charge is part of this
   procedure. Do not exercise power/reinstall/delete as a visibility test.

## Validation

Targeted tests: `tests/unit/test_server_delegation.py`,
`tests/unit/test_website_bridge.py`, `tests/unit/test_servers_management_service.py`.
Staging gate: `uv run python scripts/verify_ci.py --staging`.
Migration safety: `uv run python scripts/check_migrations.py`.

Exact PostgreSQL contract: set an explicit **scratch loopback**
`CLOUD_PLATFORM_TEST_POSTGRES_URL`, then run
`uv run pytest tests/live/test_postgres_server_access_grants.py -q`.
It creates a random isolated schema, applies the real additive migration,
checks concurrent replay/revocation and unchanged wallet/ledger, then removes
only that schema. Without that URL the test skips; a skip is not a PostgreSQL pass.
The staging PostgreSQL job explicitly runs it; release CI includes it through
the `test_postgres_*.py` selection. Do not deploy if this contract fails.

The source implementation alone does not prove a live account sees the VPS:
approval, migration, registration (if absent) and a real admin grant are required.
