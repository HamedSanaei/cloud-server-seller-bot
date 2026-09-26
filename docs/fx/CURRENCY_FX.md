# Currency / FX Resolution

The platform has two deliberately separate FX families:

* **Domestic/payment:** AbanTether, used for IRT/IRR/USDT payment and wallet
  display/settlement paths. It is not a global catalog-price source.
* **Global catalog normalization:** Frankfurter V2, used only to turn a
  foreign provider's native cost into the configured canonical catalog
  currency. It is a reference-rate source, not an executable market and not a
  payment settlement source.

All financial arithmetic is `Decimal`; final customer rounding happens once,
with `ROUND_CEILING` in the target currency's audited minor unit.

## Audited currencies

| Code | Meaning | Exponent |
|------|---------|----------|
| IRT | Toman | 0 |
| IRR | Rial | 0 |
| EUR, GBP, SGD, AUD, CAD, USD | global fiat | 2 |
| JPY, KRW | global fiat | 0 |

`IRT 1_250_000` is 1,250,000 Toman. `JPY 691` is ¥691, never ¥6.91. An
unknown currency fails closed in conversion/pricing. Display formatting uses
the same exponent registry.

## AbanTether (domestic/payment)

Read-only public ticker (no API key):

* `GET https://api.abantether.com/api/v1/manager/otc/ticker?coin=EUR`
* `GET https://api.abantether.com/api/v1/manager/otc/ticker?coin=USDT`

Only the exact `EURIRT` and `USDTIRT` markets are accepted. `DISPLAY` and
`CHARGE` use the acquisition (`buy`) side; `LIQUIDATION` uses `sell`. The
`USD`/`USDT` relationship is an explicit configured proxy, never an implicit
identity. Settlement through the proxy is disabled by default and requires the
operator flag.

## Frankfurter V2 (global catalog)

The adapter calls only:

```text
GET https://api.frankfurter.dev/v2/rate/{base}/{quote}
```

The official response is a flat object, for example:

```json
{"date":"2026-01-05","base":"EUR","quote":"USD","rate":1.145}
```

The adapter parses numeric JSON tokens directly as `Decimal`, validates the
base/quote/date/rate, and records provider date separately from retrieval
time. There is one reference rate and therefore no bid/ask spread. It supports
the audited global currencies above, including JPY/KRW, and never handles IRT,
IRR, or USDT. `USD -> USD` is an explicit identity operation and makes no HTTP
request.

## Catalog pricing contract

For a foreign offer, auto-sync and the deliberate normalization command use:

```text
immutable provider-native exact Decimal
  -> exact global reference rate
  -> configured operator markup
  -> one final ceiling to target minor units
```

The native `provider_cost_minor` and `provider_cost_currency` remain unchanged
for audit. Hourly pricing prefers
`billing_parameters.provider_hourly_rate` (or the provider adapter's exact
hourly-rate key) as Decimal text; it fails closed if an hourly observation has
no exact rate rather than pricing from already-rounded cents. A sample:

```text
0.0453 EUR * 1.17 = 0.053001 USD
0.053001 * 1.25 = 0.06625125 USD
final customer price = $0.07/hour
```

The durable `pricing_metadata` JSONB records a versioned provenance snapshot:
source/target currencies and amounts, exact rate/provider date/retrieval and
expiry, stale marker, markup, rounding rule, and final minor amount. The
provider-native cost fields are included separately.

A bounded last-known-good global reference rate may be used for catalog
repricing only within `[fx.frankfurter].catalog_max_stale_seconds` (the
separate cache retention bound is `max_stale_seconds`). The resolver keeps a
short failure memo so a provider outage cannot turn into a request stampede.
An expired cache entry is refreshed live first; a stale value is considered
only after that live attempt fails (or while a failure memo is active), and the
failure memo is rechecked while holding the pair lock. The flag is persisted
as `fx_stale=true`. A missing/expired/too-old rate never becomes 1:1 or zero:

* a previously canonical price is preserved only when its native cost snapshot
  is unchanged and it remains safe;
* a new, noncanonical, or stale-cost offer is left unpriced and not sellable;
* manual prices and operator-disabled rows are never overwritten.

The entire sync shares one resolver and one cache; external calls scale with
distinct currency pairs, not offer count. In-process singleflight and a
versioned provider-family cache key prevent local stampedes. Redis is used in
production.

### Catalog publication horizon (never publish a price that dies first)

A newly published catalog price must stay provable until the NEXT scheduled
refresh has completed. Otherwise the storefront empties between two healthy
syncs the moment its reference rate runs out — with every provider read fine.

That is exactly what happened on 2026-09-26: a cached reference quote with ~2
minutes of TTL left was still classified `fresh`, so one Leaseweb hourly run
published every offer with `catalog_valid_until` two minutes away. The foreign
market went invisible for ~12 minutes until the next sync repriced it, and only
the generic "no valid FX provenance" readiness line hinted at why.

The horizon is derived from configuration, never hardcoded:

```text
storefront.catalog_sync.interval_seconds
  + min(storefront.catalog_sync.timeout_seconds, interval_seconds)
  + storefront.catalog_sync.fx_safety_margin_seconds        (default 300)
```

`GlobalFiatFxResolver.get_catalog_rate(..., min_remaining_lifetime_seconds=...)`
therefore never returns a technically-fresh-but-too-short quote as fresh. It
refreshes live first and only then uses the bounded last-known-good policy,
which is persisted explicitly as `fx_stale=true` with
`catalog_valid_until = observed_at + catalog_max_stale_seconds` plus its
`fx_stale_limit_seconds`. If neither can cover the horizon the call fails
closed.

`CatalogOfferPricer` enforces the same rule at write time, measured from ONE
anchor per catalog run (a 2-3 minute provider walk must not shorten what it
publishes), so a short-lived replacement is refused instead of written. A row
whose own bounded provenance is still valid keeps its price and stays on sale
while the failure is audited; only an unprovable price is cleared. One refused
row never aborts the rest of the walk, so a single bad pair cannot leave a
half-refreshed catalog. If a market
does fall to zero, the run logs
`storefront blackout occurred: reason=fx_provenance_expired|fx_pricing_failed|
bounded_catalog_fx_exhausted pair=... previous_sellable=... resulting_sellable=...`
and each run records per-pair `observed_at`/`expires_at`/`catalog_valid_until`
and remaining lifetime at sync start and publish.

Transactional settlement is untouched: payments keep using
`get_rate(allow_catalog_stale=False)` and never inherit the catalog horizon.

## Identity and domestic routes

* Identity conversions make no source call.
* `1 IRT = 10 IRR` is exact in both directions and makes no source call.
* AbanTether routes EUR/USDT to IRT according to the explicit domestic policy.
* Global catalog pricing never routes an IRT/IRR/USDT pair through Frankfurter.
* Existing accepted orders and hourly snapshots retain their creation-time
  selling amount and currency; later FX movement only reprices catalog rows.

## Configuration

```toml
[fx]
enabled = true
domestic_enabled = true
global_enabled = true
domestic_provider = "abantether"
global_fiat_provider = "frankfurter"
catalog_pricing_currency = "USD"
# Domestic UI/payment compatibility only; not a catalog selling currency.
default_display_currency = "IRT"

[fx.frankfurter]
base_url = "https://api.frankfurter.dev"
request_timeout_seconds = 5
quote_ttl_seconds = 3600
max_stale_seconds = 345600
catalog_max_stale_seconds = 86400

[storefront.catalog_sync]
enabled = true
interval_seconds = 900
timeout_seconds = 600
# Safety margin on interval + clamped timeout (the catalog publication horizon).
fx_safety_margin_seconds = 300
```

`fx.provider` remains accepted as a backward-compatible alias for the
domestic provider. Production requires HTTPS, a safe absolute Frankfurter URL
with no credentials/query/fragment, and a positive timeout. The configuration
validator rejects an unsupported target or an invalid stale window.

Automatic policies are under `[storefront.pricing.<provider>]` (and an
optional `[storefront.pricing."<provider>.<billing-model>"]` override). The
legacy root `[storefront_pricing.*]` block is not read.

## CLI and operations

```bash
uv run python -m cloud_platform.cli fx doctor
uv run python -m cloud_platform.cli fx rates --target USD
uv run python -m cloud_platform.cli offers doctor
uv run python -m cloud_platform.cli offers normalize-selling-currency --target USD --dry-run
uv run python -m cloud_platform.cli catalog auto-sync run
uv run python -m cloud_platform.cli offers normalize-selling-currency --target USD --execute
uv run python -m cloud_platform.cli offers readiness
uv run python -m cloud_platform.cli catalog auto-sync doctor
```

`normalize-selling-currency` is deliberately gated by exactly one of
`--dry-run` or `--execute`. It shares one global resolver, preserves manual
intent, recomputes auto rows from native cost plus policy markup, converts
manual rows from their existing selling amount without a second markup, and is
idempotent. It never changes provider-native cost or an operator-disabled row.

`catalog auto-sync run` executes exactly one complete provider refresh (the
worker cron pass: official read-only APIs, server-owned markup, publication)
with the DEDICATED catalog timeout. It comes BEFORE canonicalization because a
row's price can only be converted against provider facts the row already
carries: a row whose exact provider rate is missing, or whose integer cost was
stored under a wrong minor-unit assumption, is refused by the price book until
the provider is re-observed. Canonicalization first, without the refresh, can
only report it as `failed`.

Its result is classified, and the exit code reports only REAL failures:
`normalized` (or `WOULD` in dry-run), `intentionally skipped` (operator-disabled
rows, a provider without an automatic pricing policy, manual domestic IRT/IRR
prices) and `failed` (pricing/FX/conversion errors, plus a row whose price
changed under us — a simple re-run resolves those). Exit 0 means "nothing was
left fail-closed", exit 1 means at least one row is still not canonical.

`offers readiness` is the machine-usable release gate (exit 0 = ready): for
every provider that is configured with a market, enabled, credentialed and
auto-priced, a catalog with stored offers must have at least one sellable row
in the catalog currency, and it must not have discovered plans that persisted
nothing. Providers the operator never configured (no market, `enabled = false`,
no credential, no pricing policy) are never required to have offers, and a
catalog whose stored offers are ALL operator-disabled is reported as operator
intent rather than an outage. `scripts/deploy-production.sh` runs this before
promoting a release, because green health checks cannot see an empty store.

All customer browse/checkout/create paths reject a foreign offer unless its
selling currency is the configured target, and a wallet must match the offer's
selling currency. Monthly OS/panel callbacks carry signed, stable selectors
(digests of exact live option names), never list indexes; the terminal selector
also binds the confirmation-time integer price and currency, so a catalog
change cannot be charged through an old button. The bot's foreign catalog
presentation shows the canonical amount only; mixed native/USD display amounts
are not shown to customers.

## Post-deploy verification (read-only)

```bash
uv run python -m cloud_platform.cli fx doctor
uv run python -m cloud_platform.cli offers doctor
uv run python -m cloud_platform.cli offers readiness
uv run python -m cloud_platform.cli catalog auto-sync doctor
uv run python -m cloud_platform.cli fx rates --target USD
uv run alembic current
```

Confirm migrations `0043` and `0044` (current head) applied cleanly, the configured `[fx]`
family switches and blocks, provider-cost columns still show their original
currencies, and every sellable foreign row has
`pricing_metadata.fx_provider = "frankfurter"`, `selling_currency = "USD"`, and
an auditable exact conversion. No deployment command in this document performs
a provider mutation or changes a wallet balance.

## Legacy non-USD wallets (explicit operator plan)

New wallets are created in USD (`DEFAULT_WALLET_CURRENCY`). Existing non-USD
balances are NEVER silently rewritten: checkout and accrual fail closed when
the wallet currency does not match the selling currency, and the mismatch is
surfaced as a `ValueError` naming both currencies.

Migration `0044` repairs one safe legacy edge case automatically: an
already-existing wallet with a zero balance and no ledger entries, holds,
servers, or payment sessions is still pristine, so changing its empty
denomination to USD cannot reinterpret any money or service history. Every
non-pristine wallet remains untouched.

To migrate a legacy wallet that is not pristine, the operator must do all of
the following deliberately, per wallet:

1. Confirm the wallet has no RUNNING hourly server and no CAPTURED hold
   (an active contract keeps its frozen snapshot regardless).
2. Record the exact pre-migration balance and currency off-system.
3. Zero the old balance with an audited `wallet_adjust` (negative amount,
   explicit reason) — this creates the immutable ledger fact.
4. Credit the identical major-unit value into a new USD wallet only after an
   explicit, separately recorded conversion decision (rate, source, date);
   the platform never invents that rate.
5. Keep the old wallet row for audit; never delete ledger history.

There is no automatic conversion, no background rewrite, and no code path
that relabels a balance.
