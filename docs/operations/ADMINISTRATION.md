# Telegram administration and corporate funding

The private `configuration.toml` is the only administrator-identity source:

```toml
[telegram]
superadmin_user_ids = [123456789, 987654321]
```

These are integer **Telegram User IDs**, not usernames or `admin_chat_id`.
The latter is a notification destination. Startup reconciles existing accounts
and pending assignments; a pending identity gains access on its first trusted
private `/start`. Existing users, identity records, wallets and history remain.
Removing an identity revokes its configured authorization, including stale
callbacks. The public API cannot supply or promote a Telegram identity.

## Customer wallet denomination

Customer wallets are denominated in whole Toman (`IRT`), not USD or Iranian
rial (`IRR`). Domain defaults, trusted Telegram onboarding, repository defaults
and the database insertion default agree on `IRT`. AtlasPay Toman payments and
manual Toman adjustments therefore need no wallet FX conversion.

Provider-native costs, foreign catalog quotes and the USD invoice required by
NOWPayments are separate accounting facts, not additional customer wallets.
Any cross-currency payment must freeze a permitted, fresh conversion and credit
the Toman wallet only. Changing a wallet default must not enable USDT-proxy
settlement or invent an exchange rate.

Historical non-IRT wallets require an explicitly reviewed migration. Do not
rename an existing currency, reset a balance, rewrite old ledger entries, or
change a pending invoice's frozen credit amount/currency. Even a zero balance
does not authorize migration while an older payment remains unresolved; hold
it for an explicit operator decision and preserve the original financial facts.
Migration `0060` changes only the insertion default, never existing rows.

For an explicitly approved **zero-balance, no-ledger/no-hold** wallet only,
the operator-only `SqlAlchemyEmptyTomanCutover` command can cancel one reviewed
unpaid invoice and change the denomination atomically. It keeps the invoice's
original identity and monetary fields, records an immutable cancellation receipt,
and blocks late callbacks from crediting it. This is local cancellation, not a
claim that a provider's bank/payment link was deleted. Other obligations, paid
invoices or monetary history require financial review and block this command.
It never fabricates an FX rate or zero-value ledger entries.

Authorized accounts get the corporate server/renewal/my-servers/management/support
keyboard, without a personal wallet or recharge action. Management controls are
application-authorized, not merely hidden buttons. Customer wallet adjustment
input is signed **whole Toman**; the confirmation freezes its conversion into
the actual wallet currency, the reason and the unique ledger operation. Debit
cannot overdraw. Administrator identities are not customer credit targets.

## Corporate accounting

Persisted `customer_wallet` remains the default for historical contracts. Only
the configured active owner's own new contract uses `operator`. Customer-owned
contracts remain wallet-funded regardless of who initiates a support operation.
Corporate coverage uses an authorization grant and an immutable provider-cost
expense receipt, never an unlimited balance, fake wallet, or synthetic deposit.
Native provider amounts retain exact decimal strings and their currency.

New spending, dispatch and renewal recheck authorization. Previously accepted
provider facts can settle after revocation without incurring a second expense.
Revocation retains an existing monthly paid term. Corporate hourly resources
are suspended/reported without wallet-driven deletion; provider-specific costs
for stopped resources may still exist. An inconclusive provider observation is
reported, not treated as proof that billing stopped.

Reports separate actual wallet-funded charges/refunds, gateway cash receipts,
corporate nominal coverage and native corporate costs. These categories must
not be added together as sales. Historical totals do not depend on today's user
role. Direct/batch messaging freezes up to 100 confirmed recipients and leases
up to 20 deliveries. Telegram has no idempotent send key: a lost acknowledgement
is `ambiguous` and is never automatically resent.

## Payment setup

Fill the canonical `[payments.atlaspay]` and `[payments.nowpayments]` sections
privately. Do not commit real keys, JWT credentials or IPN secrets. Existing
invoices remain reconcilable when creation is disabled; the management override
controls new invoice creation only. AtlasPay displays the provider-returned
payable amount and settles only after authenticated identity/reference/amount
verification with full received-payment evidence.

NOWPayments creates a hosted USD invoice; crypto choice stays on its page.
`gateway_payment_id` stores the invoice ID; `provider_payment_id` pins the
distinct concrete payment ID. Cloud-specific order references prevent payments
from another application using the same merchant account from settling here.

Set `callback_url` to the cloud domain's exact
`https://<cloud-domain>/webhooks/payments/nowpayments`. Install the example
`deploy/production/nginx-nowpayments-location.conf.example` inside only that
cloud HTTPS server block, using its existing API port. Run `nginx -t` before
reloading. Do not copy another project's callback or merchant-wide settings.
The native `x-nowpayments-sig` HMAC-SHA512 is checked before any scheduling;
notifications never authorize a credit. Polling, manual inquiry and IPN share
authoritative verification and idempotent settlement; only fully paid `finished`
payments credit the frozen wallet amount. Manual-review invoices stay held.

After an ambiguous invoice POST, recovery is read-only and never re-POSTs.
An unpaid invoice absent from payment listings can remain unresolved until
the provider exposes a concrete payment; do not invent its ID or create another
invoice with the same attempt. Public origin/WAF problems must be distinguished
using safe invalid-signature probes; never disable site-wide protections.

## Reviewed historical cutover and deployment

Use `python -m cloud_platform.cli funding cutover <private-manifest.json>` to
preview, and add `--apply` only for the explicitly reviewed manifest. It anchors
server UUID, owner, source, state, provider identity, current coverage and reason;
it is not a role-based bulk update. Multiple owners require separate self-owned
manifests. Failed/unaccepted or deleted history is classified without resurrecting
resources or inventing company costs. Accepted coverage requires real native cost
evidence and immutable linked receipts. Refund only an actual unrefunded personal
debit; released holds and corporate expenses are not refundable personal payments.

Before any production write, back up the existing private configuration, relevant
Nginx file and database while holding the deployment path's `.deployment.lock`.
Preserve unrelated configuration and the shared database/Redis/bot token. Validate
the candidate configuration without displaying secrets. Do not run new-setting
validation using an old image that does not yet know those settings.

Complete code review and targeted staging/real-PostgreSQL checks before pushing
`staging`: that push automatically validates, builds and deploys its exact SHA
to the existing shared stack. Never start a second bot poller. Verify the running
image SHA, migration/schema parity, service health, configured access, public
IPN rejection of invalid signatures and both configured gateways' read-only
readiness. Only then notify already-registered administrators with their new
keyboard. No billable purchase/payment probe is part of this procedure.
