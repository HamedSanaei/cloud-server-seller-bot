# Website + Telegram integration

The Python platform owns users linked from Telegram, wallet ledger, payments,
catalog/price policy, orders and infrastructure. The ServerCloud React/Fastify
project owns web login/session handling and acts as a server-side proxy, not a
second financial system. Existing bot management/renewal/billing flows remain
authoritative. Prices use the same sellable offers, currency provenance, integer
minor units and expected-price checks as bot purchases. No independent web markup
or wallet credit is applied in unified mode.

## Activation and account linking

1. Deploy the Python API, bot and worker with the same PostgreSQL, Redis, private
   configuration and confirmation signing secret. Production transient state must
   use Redis, not process memory. Keep `api.allow_header_identity = false`.
2. Complete normal bot onboarding (including applicable terms/identity checks).
   Send `/website` **in the private chat**. Paste the five-minute single-use code
   into the website's **تنظیمات ← اتصال تلگرام** page. The code is bearer material: do not
   forward it, put it in URLs, log it or send it to support.
3. Configure the website backend's `PLATFORM_API_URL` to the Python API **origin**
   (no path/query/embedded credentials), through trusted private networking or
   HTTPS. Never put it or a platform token in `VITE_*`. See the website repository's
   `docs/PLATFORM_INTEGRATION.md` for safe cutover and its new DB migration.
4. Python `/v1/console/link/exchange` consumes the code atomically and mints a
   revocable `cpt_` credential. Only its hash is stored here; only the website
   backend receives plaintext. Web storage encrypts it with AES-GCM and web-user
   AAD. A unique web DB constraint prevents linking one platform user twice.
5. `/v1/console/*` uses normal bearer authentication, scopes, per-token limits and
   mandatory mutation idempotency keys. Every operation reloads persisted account
   state. Admin routes additionally check configured Telegram ID + active admin
   RBAC; a web admin role alone grants no platform privilege. Revoke the website
   token through existing token management if compromised. Re-link with a fresh
   code after revocation; token count limits still apply.

Do not expose pairing codes or tokens in telemetry, request/response-body logs,
analytics or reverse-proxy traces. Restrict the exchange route to the website
backend at ingress where practical. No new shared global master token is needed.

## Additive website capabilities and deployment order

The console contract advertises `website-link-revoke:v1` and
`website-charge-requests:v1` in its `data.features` array. Publish the platform
image containing these capabilities before the website that requires them.
A healthy API with contract version `1` alone is not sufficient. Website
preflight rejects missing capabilities before stopping its live writers.

- `POST /v1/console/link/revoke` requires a scoped bearer and an idempotency
  key. It revokes only the authenticated bearer, never a caller-supplied token
  or another account. The website retains its identity binding on disconnect.
- `GET /v1/console/charge-requests` requires wallet-read scope and an active
  account. It reads only the owner's latest 50 payment sessions, preserving
  frozen credit amount/currency when present and provider-native facts
  otherwise. It does not settle payments, credit wallets or fabricate invoices.
- Readiness probes use no bearer and require `401 unauthorized` on both routes.
  They must never revoke a real account token or read customer financial data.

No schema change is required by these two endpoints. Retain the normal backup,
migration/schema, single-poller and health gates during the platform-first
rollout. Only retry the website deployment after the running platform image
advertises both features and the endpoint-auth probes succeed.

## Shared operator test server

As requested, **leaseweb-test** is the operator-owned test VPS with provider ID
**12717238** and **Public IPv4 217.20.121.89**. It is available to configured,
persisted active superadmins, not ordinary users. One internal owner is retained
for referential integrity; all current superadmins may manage the exact fixture.
It is operator-funded, not billed to a customer's wallet. It is NOT provisioned
again by this integration, and deletion through the shared web interface is denied.

Use `/testserver` in the bot, the website's **شناسایی و اتصال leaseweb-test**
button, or `uv run python scripts/register_test_server.py`. Registration only
reads provider inventory, proves BOTH ID and IP in exactly one configured
credential account, pins that account and inserts a local row if absent. It
fails closed on ambiguous accounts, conflicting existing rows or unstable state.
The script reconciles configured superadmins first. Provider credentials remain
in private configuration; never copy them into either client or documentation.

The bot and web controls support power on/off, reboot, image selection/reinstall
and password reset subject to existing provider capabilities and feature policy.
Stop/reinstall/reset require a signed, expiring, single-use confirmation bound
to the resource and arguments. Passwords are claimed privately and acknowledged
after delivery; never included in notices or operation history. The web client
retains a claimed password in component memory only until acknowledged/navigation.

Management requests have durable command receipts with unique keys and payload
digests, serialized per server. An in-flight/unknown request is never automatically
dispatched again. The user-facing lifecycle is `pending`, `running`, `success`,
`failed`, or `outcome_unknown`; transport receipt `completed` alone is not success.
The worker reconciles provider identity, power state, boot/image or replacement
credential evidence before final success. Encrypted credentials, final receipt
and customer notice commit atomically. Both interfaces expose the same history,
including a safe failure reason and evidence; no password is included.

## Optional SSH port testing (explicit opt-in)

Port management is guest-OS configuration, NOT a Leaseweb control-plane API.
It is disabled until `api.test_server_ssh_alias` is configured and an operator:

- installs `scripts/guest/serverhub-ssh-port` as root-owned executable at
  `/usr/local/sbin/serverhub-ssh-port` **inside this test VPS only**;
- grants a dedicated SSH identity noninteractive sudo for **only that helper**,
  not arbitrary shell commands;
- supplies a private directory containing `config`, the dedicated identity and
  verified `known_hosts`; the API and bot mount it read-only at
  `/etc/cloud-platform/management-ssh` (the worker has no guest SSH mount);
- configures the alias in that `config` with the exact test IP, strict host-key
  verification, the mounted identity and known-hosts paths; verifies host keys
  out of band and grants only the actual application image user's required read
  access (do not assume a fixed UID/GID from another image);
- verifies the guest uses OpenSSH service configuration, not socket activation,
  and allows the new TCP port in any external/custom firewall.

Then use `/testport 2222` or the website's test-server port control and explicitly
confirm. Ports must be integers 1024–65535. The helper locks changes, validates
`sshd` configuration, retains old listening ports, handles active UFW and reloads
SSH. A second connection to the new port must succeed before reporting success.
On timeout/reachability failure the outcome is unknown; check the server before
retrying. Keeping the old port avoids deliberate lockout. Removing an old port,
custom firewall repair and installing this helper are not automatic actions.

## Notifications and results

Customer-safe events share the durable business-log outbox and independent
Telegram delivery, even if the optional operator channel is disabled. Wallet
deposit/admin adjustment notices are written in the SAME transaction as ledger
and balance; abuse creation/account status notices are also atomic with their
domain write. Customer destination comes from persisted user ID → Telegram ID,
never a request-provided chat ID. Allowed event fields exclude provider diagnostics,
credentials and tokens. Existing low-balance/runway and provisioning notifiers
continue running; monthly renewal and server-management events also fan out to
customers. Web inbox reads only events owned by the linked platform user.

Delivery is retried with bounded backoff. An abandoned event needs operator
attention. Exactly-once Telegram transport is not claimed: a crash after Telegram
accepts a message and before its receipt commits can cause a duplicate message.
It cannot duplicate wallet credit or provider work.

No live server mutation, guest installation, deployment, legacy balance import
or production cutover is performed merely by applying these source changes.

## Operational activation and pricing

Deploy API, Telegram bot and worker from one immutable full-SHA image. Deploy the
web frontend and API/worker from their matching full-SHA images. Both sides send
and enforce console contract `1` and the exact application environment name.
Both release branches target the original production runtime with environment
`production`; a genuinely staging-configured client cannot impersonate that runtime.
Readiness also requires disabled header identity, a real confirmation signing
key, IRT wallets/display and the configured USD/USDT settlement policy.
See `deploy/staging/README.md` and `docs/operations/PRODUCTION_DEPLOY.md`.

Explicit values in an existing private TOML override the new defaults. Before
activation, review `[app] default_currency = "IRT"` and
`[fx] allow_usdt_proxy_for_settlement = true`; retain the persistent encryption
and signing keys. Back up configuration and reconcile wallet denominations before
changing them in the controlled writer-drain window. Never reinterpret or sum
legacy foreign-currency balances as toman. The web `.env` must contain
`PLATFORM_API_URL=http://cloud-platform-api:8000`; deployment rejects an empty
origin instead of reporting a successful legacy/unified cutover.

USD and USDT are internal one-to-one quote bases. Settlement resolves a fresh
purpose-specific FX snapshot, converts through the USDT rial rate and settles in
integer IRT; 10 rial is 1 toman. Monthly customer holds are also IRT, retaining
the original provider cost and FX trace. Replays reuse the frozen order/ledger,
not a new quote or credit. Never invent a fallback rate or silently relabel an
existing foreign-currency wallet; reconcile and approve any legacy currency
conversion before cutover.

Low-balance notices use the same durable customer outbox as deposits, abuse,
shutdown and management results. A runway below 72 hours is warned every six
hours while the same shortage episode remains active. The next-hour balance
guard still requests shutdown at zero or below one hour's required funding;
eligible wallet-suspended servers regain their power-on control only after
sufficient funding, rather than trusting a stale browser balance.

### Exercised integration and remaining activation boundary

The isolated local stack exercised the registered `/website` bot handler, real
Redis single-use pairing, both HTTP APIs, web UI, canonical PostgreSQL wallet,
audited synthetic credit, replay protection, customer inbox and authorization.
The actual Telegram network transport was replaced only by a recording
transport; this does not prove delivery to a real chat or production account
pairing. Real ARQ/Redis heartbeat progress and the lightweight worker probe were
also exercised independently of production.

Provider reads verified ID **12717238** and IP **217.20.121.89** in exactly one
configured Leaseweb account, with state `RUNNING`. Two integrated registrations
returned the same local fixture ID; ordinary-user access returned 404. A missing
local Provider row on a clean installation is inserted idempotently without
changing native ownership. `leaseweb-test` is the local display alias; the
provider's native reference differs and was not renamed.

No provider power/password/image/port/delete mutation, guest helper installation
or Production database/configuration change was performed by this verification.
Before live power testing, verify current dependencies, pinned recovery access
and the initial state; restore that state afterward. Reinstall/delete and other
destructive changes require separate approval. The operator selected the original
site/bot/runtime for staging releases and will supply the GitHub deployment
secrets personally. Live delivery requires that release and an authenticated
operator Telegram interaction; isolated tests do not prove network delivery.

The PostgreSQL/HTTP and registered-dispatcher regression scenarios also exercise
positive shared-server power, asynchronous password/image completion, private
one-time credential claim/ack, callback hijacking and replay, and the result
visible from the opposite interface. External provider/Telegram transports are
isolated in these automated scenarios; they are not live VPS mutation evidence.

Shared-fixture power uses the existing audited administrator support intent and
executor, retaining the real requesting administrator rather than impersonating
the FK owner. Persisted superadmin assignment is revalidated at enqueue and
immediately before provider execution, including worker retries. Ordinary/legacy
power routes continue denying the reserved resource. JSONB model defaults now
use SQL expressions matching the existing migrations; no additional financial
schema revision or customer-data rewrite is required for that correction.

Hourly restart affordability resolves the canonical **user ID** to its IRT wallet,
then subtracts active holds by the distinct **wallet ID**. Mixing these identities
made funded restarts remain unavailable and could prevent balance-policy evaluation.
The real PostgreSQL/HTTP regression checks below-hour funding, restoration at the
exact next-hour amount and an idempotent single prepayment/provider dispatch.
