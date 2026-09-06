# ZarinPal gateway (M09-004) — first Iranian payment gateway

Status: **implemented** — `ZarinPalGateway` in
`src/cloud_platform/providers/zarinpal/` implements the provider-neutral
`PaymentGateway` port (`create_payment` + `verify_payment`; v4 has no refund
endpoint, so `REFUND` is deliberately not advertised).

## 1. API

- `POST {base}/request.json` — `{merchant_id, amount (Rial int), callback_url,
  description, metadata{reference, idempotency_key}}` →
  `{data: {code: 100, authority, message}, errors: {}}`.
- Pay URL: `{StartPay}/{authority}` (`StartPay` = `https://www.zarinpal.com/pg/StartPay`,
  sandbox `https://sandbox.zarinpal.com/pg/StartPay`).
- `POST {base}/verify.json` — `{merchant_id, amount, authority}` →
  `{data: {code: 100|101 (success + already-verified), ref_id}, errors: {}}`;
  codes `-51/-53/-54` mean FAILED.
- Sandbox base: `https://sandbox.zarinpal.com/pg/v4/payment`.

## 2. Configuration (.env)

```bash
ZARINPAL_MERCHANT_ID=<merchant-id>
ZARINPAL_SANDBOX=true        # false in production
ZARINPAL_BASE_URL=https://api.zarinpal.com/pg/v4/payment
ZARINPAL_CALLBACK_URL=https://api.example.com/webhooks/payments/zarinpal
```

The webhook HMAC secret for `POST /webhooks/payments/zarinpal` lives in
`PAYMENT_GATEWAY_SECRETS={"zarinpal": "<random-32-bytes-hex>"}`.

## 3. Money

Amounts are integer Rial (minor units), validated (`> 0`), `Decimal`-free
integers end-to-end. Only `IRR` is accepted — a `EUR` request fails fast
before any I/O.

## 4. Replay safety

- Gateway `authority` is the payment session's external id
  (`(gateway_key, gateway_payment_id)` UNIQUE).
- Deposits use the deterministic ledger key `deposit-zarinpal-{authority}`.
- `PaymentReconciliationService` (M09-007) rechecks stuck PENDING sessions
  via `verify_with_amount` and routes through the same
  `PaymentWebhookService.process_callback` — never a second deposit.

## 5. Test evidence

- Offline: `tests/unit/test_zarinpal_gateway.py` (9 tests: round-trip,
  sandbox URL, failure table, currency gating, no secret leakage),
  `test_payment_reconciliation.py` (4 tests), `test_payment_status_ux.py`.
- Live sandbox: set `ZARINPAL_SANDBOX=true` + a sandbox merchant id, create a
  10,000 Rial payment, pay with a sandbox card, assert the wallet deposit +
  `ref_id` metadata; record the authority/ref_id here before going live.
