# Tetraminator gateway — second Iranian payment gateway

Status: **implemented** — `TetraminatorGateway` in
`src/cloud_platform/providers/tetraminator/` implements the provider-neutral
`PaymentGateway` port (`CREATE_PAYMENT` + `VERIFY_PAYMENT`; no refund
endpoint exists, so `REFUND` is deliberately not advertised).

Source contract: the user-provided Tetraminator seller documentation
snapshot. Nothing below is invented; anything Tetraminator-specific that is
OUR mechanism (not theirs) is labeled as such.

## 1. API (documented)

- `POST {base}/invoice/create` — headers `Content-Type: application/json`
  + `X-API-KEY`; body `{price (integer Toman), callback_url (http/https)}`
  → `201 {status, message, pay_id, payment_link}`.
- Documented errors: `400` (incomplete data / below minimum), `401`
  (invalid key), `403` (seller/store disabled).
- Payment: `GET {base}/payment/inquiry/{pay_id}` (header `X-API-KEY`) →
  `{status, payment_status, pay_id, amount}`.

## 2. What the documentation does NOT provide

- **No webhook signature.** The success callback is an unsigned
  `GET <callback_url>` with no body. The callback is therefore UNTRUSTED
  by design: it only identifies a local session, and every credit requires
  a server-side inquiry first.
- **No idempotency key.** Duplicate-invoice protection is local
  (idempotency-key session lookup + the unique
  `(gateway_key, gateway_payment_id)` pair).
- **No refund endpoint.** Not advertised, not implemented.

## 3. Money (audited boundary)

External `price`/`amount` fields are integer **Toman**. Internally the
platform keeps integer minor units and treats **IRT** minor units as Toman,
so the conversion is the identity (`amount_minor == price`) — no float, no
division, no FX. Only `IRT` wallets are supported; `EUR` (or anything else)
is refused rather than converted. Documented minimum invoice: **50,000
Toman**, enforced server-side in the adapter AND hidden in the Telegram UI
(gateways below the chosen amount are not offered).

## 4. Configuration (server-owned TOML only)

```toml
[payments.tetraminator]
enabled = false          # opt in explicitly
api_key = "CHANGE_ME"    # NEVER committed with a real value
base_url = "https://api.tetraminator.com/v1"
callback_url = "https://YOUR-PUBLIC-DOMAIN/webhooks/payments/tetraminator"
timeout_seconds = 30
```

The callback URL must be publicly reachable (HTTPS required in
production) — typically the same reverse proxy that serves the API.
The real API key lives only in `/etc/cloud-server-seller/configuration.toml`.

## 5. Callback edge (OUR security mechanism, not Tetraminator's)

- `GET /webhooks/payments/tetraminator?ref=<session-uuid>`. The reference
  is the local session id (unguessable UUID): it carries no user id, no
  amount and no secret, and nothing in it is trusted.
- The handler loads the session, requires `gateway_key == "tetraminator"`,
  uses the STORED `pay_id` (never a caller-supplied one), inquires, and
  requires `status is true` AND `payment_status == "paid"` AND pay_id match
  AND exact amount match before invoking the replay-safe deposit path.
- Amount mismatch (or pay_id mismatch) records a failure for operator
  review — never credits, never adjusts.
- Responses are minimal (`action`, `session_status`); no user, balance,
  key or traceback ever leaves the edge.

## 6. Durable flow and replay safety

1. Pending intent persisted first (no external id yet) → stable session id.
2. Callback URL built from that id → `invoice/create` → `pay_id` bound.
3. Same idempotency key with a bound `pay_id` reuses the session/link.
4. Verified callbacks credit exactly once via `PaymentWebhookService`
   (deterministic `deposit-tetraminator-{pay_id}` ledger key); replays and
   10 concurrent callbacks collapse to one deposit.
5. A bounded worker job (`reconcile_tetraminator_payments`, every 15 min)
   re-inquires sufficiently old PENDING sessions with a `pay_id` through
   the same credit path; transient inquiry failures keep the session
   pending.

## 7. Telegram UX

`افزایش موجودی` → amount → payment method (only when several compatible
gateways exist; skipped for exactly one) → provider `payment_link` button
(`پرداخت`). Gateway availability is capability-driven per wallet currency;
after a verified payment the existing durable notification mechanism
(recharge-succeeded business event) informs the customer.

## 8. Test evidence

- `tests/unit/test_tetraminator_gateway.py` (adapter: request shape,
  headers, Toman conversion, minimum, inquiry mapping, error table, no
  secret leakage with `tetra_TEST_SUPER_SECRET_API_KEY`).
- `tests/unit/test_tetraminator_callback.py` (untrusted callback, exact
  verification rule, mismatches, replay/concurrency, reconciliation).
- `tests/unit/test_wallet_recharge.py` (multi-gateway selection, minimum
  gating, persist-first flow) and monthly-UI gateway-screen tests.
- ZarinPal suite untouched and green; ledger stays append-only.
