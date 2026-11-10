# TabaPay wallet recharge

The platform creates hosted TabaPay transactions through the existing wallet
recharge API used by the website and bot. The wallet amount stays in its
original currency, with the existing immutable settlement snapshot. TabaPay
accepts 100,000–500,000,000 IRR, equivalent to 10,000–50,000,000 IRT.

Set `[payments.tabapay]` in the private `configuration.toml` using the
example fields. Set `sandbox = true` only in an isolated development
environment. Keep `enabled = false` until the platform release, public
callback, and merchant domain configuration have been checked.

Expose the exact HTTPS callback path `/webhooks/payments/tabapay` to the
platform API using `deploy/production/nginx-tabapay-location.conf.example`.
Exclude it from CDN caching. Set `return_url` to the website's charge history.
The GET return verifies with TabaPay using the stored token and amount. The
authenticated POST accepts complete issuer confirmations for a known
transaction and acknowledges them only after the shared ledger credit.
Repeated callbacks and worker reconciliation use the same deposit key.

Before enabling new payments, check both callback methods on the deployed
revision with invalid fixtures, confirm the merchant's approved domain, and
test the create/return/verify flow in sandbox. Live payment testing requires
an actual customer-authorized payment. This integration does not request
TabaPay tax invoices or collect card or national-ID information.

[Official TabaPay API documentation](https://tabapay.ir/docs/api)
