# Currency / FX Resolution

Provider-neutral, platform-level financial subsystem. The single reusable
path for displaying money, converting catalog prices for display, and
bridging wallet recharge amounts between the wallet currency and the payment
gateway settlement currency.

## Supported currencies

| Code | Meaning | Minor units |
|------|---------|-------------|
| IRT  | Toman   | zero-decimal: minor == Toman |
| IRR  | Rial    | zero-decimal: minor == Rial (`1 IRT = 10 IRR` exact) |
| EUR  | Euro    | 2 decimals: minor == cents |
| USD  | US dollar (display) | 2 decimals: minor == cents |

`IRT` is shown as `تومان` in Persian UI (never divided by 100, never shown
as `IRT`). `EUR 499 minor` renders `€4.99`; `USD 499 minor` renders `$4.99`.

## AbanTether source (first live FX source)

Read-only public ticker (no API key):

- `GET https://api.abantether.com/api/v1/manager/otc/ticker?coin=EUR`
- `GET https://api.abantether.com/api/v1/manager/otc/ticker?coin=USDT`

Only the EXACT market keys `EURIRT` and `USDTIRT` are read (never substring
matches; `EURI` is a different asset and is never used as EUR). Entries
require `active == true` and positive Decimal prices parsed from strings
(never float). `buy_price` is IRT to BUY one unit (acquisition side);
`sell_price` is IRT received when SELLING one unit.

## Buy vs sell semantics

- `DISPLAY` and `CHARGE` use the acquisition (`buy_price`) side so the shown
  or charged price never undercharges.
- `LIQUIDATION` uses the `sell_price` side (reporting/payout contexts).

Callers pass an explicit `FxPurpose`; there is no ambiguous `convert()`.

## USD / USDT proxy warning

Live data provides `EURIRT` and `USDTIRT` — there is NO verified fiat
`USDIRT` market. USD display uses the USDT market EXPLICITLY as a
configured proxy (`proxy=true, proxy_asset="USDT"` in every result).

- Default: `allow_usdt_proxy_for_display = true`,
  `allow_usdt_proxy_for_settlement = false`.
- A USD wallet top-up through the proxy is REJECTED by default; enabling it
  requires the operator flag plus audit metadata. A future real USD source
  replaces the proxy without changing catalog/payment/domain code.

## Routes (owned by the resolver; never duplicated at call sites)

- Identity (`IRT->IRT`, `EUR->EUR`, `USD->USD`, `IRR->IRR`): no provider call.
- Exact `1 IRT = 10 IRR` both directions: no provider call.
- `EUR <-> IRT` through `EURIRT`.
- `USD(display) <-> IRT` through `USDTIRT` proxy when allowed.
- `EUR <-> USD` through IRT: `EUR * EURIRT.buy / USDTIRT.buy` (DISPLAY/CHARGE).
- `EUR/USD <-> IRR` via IRT anchor + exact x10.
- `CHARGE` rounds UP (ceiling) to the target smallest unit; `DISPLAY` uses
  half-up; `LIQUIDATION` uses floor.

## Cache / staleness

- `quote_ttl_seconds = 60`: fresh cache is used without a live fetch.
- Expired cache triggers a live fetch (singleflight per market), validates,
  saves last-known-good, returns fresh.
- Live failure falls back to last-known-good ONLY if age <= the purpose
  limit (`max_stale_seconds = 300` for display;
  `charge_max_stale_seconds = 30` for financially binding CHARGE/LIQUIDATION),
  marked `stale=true`. Older quotes FAIL CLOSED (never 0, never rate=1,
  never silent fallbacks).
- Production uses a Redis last-known-good store; dev/test use in-memory.

## Recharge snapshot (why callback never re-fetches FX)

1. Customer picks a CREDIT amount in the wallet currency.
2. The service resolves a fresh CHARGE quote and converts to the gateway
   settlement amount (Tetraminator: IRT; ZarinPal: IRR via exact x10, or
   EUR->IRT->IRR through FX).
3. The durable session persists BOTH sides plus the conversion snapshot
   (source/rate/path/observed/proxy).
4. The gateway invoice uses the settlement side.
5. The GET callback performs server-side inquiry and verifies the PROVIDER
   amount against the persisted settlement amount (exact pay_id + exact
   amount); only then is the wallet credited with the FROZEN credit side.
6. Replays and reconciliation reuse the same snapshot — a market move before
   the callback never changes the wallet credit.

## Gateway settlement

- Tetraminator: IRT only, minimum 50,000 Toman enforced after conversion.
- ZarinPal: IRR only (`IRT->IRR` exact x10; `EUR->IRT->IRR` through FX).
- No implicit `EUR<->IRT` exchange rate outside the resolver; no silent
  `USD=USDT` settlement.

## Checkout note

`MonthlyCheckoutService` still requires the hold currency to match the
offer's selling currency (holds are currency-checked by reconciliation).
Cross-currency checkout (selling != wallet) is NOT part of this task: the
resolver API is designed so checkout can later use the same CHARGE
conversion without a second FX implementation, but the billable provider
POST flow is intentionally untouched here.

## Adding another FX source later

Implement the `FxRateSource` port (`get_quote` + `close`), register it in
the container (infrastructure only), and point `fx.provider` at it. Domain,
catalog, recharge, Telegram and checkout code do not change.

## Operations

- Config lives in server-owned `configuration.toml` (`[fx]`,
  `[fx.abantether]`); examples carry safe placeholders only.
- Pre-flight: `uv run python -m cloud_platform.cli fx doctor` (read-only:
  config, EUR/IRT, USD proxy, cache; never trades, never prints prices).
- Metrics: `fx_quote_requests_total`, `fx_quote_failures_total`,
  `fx_cache_hits_total`, `fx_stale_quote_uses_total` (closed label sets).
- Wallet currency report (no balance mutation):
  `fx doctor` prints wallet counts grouped by currency.
