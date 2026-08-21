# Target data model (planned)

This file is a design target. Migrations are implemented incrementally.

## Identity
- `users`
- `telegram_identities`
- `roles`, `user_roles`
- `terms_acceptances`

## Provider/catalog
- `providers`
- `provider_accounts`
- `provider_locations`
- `provider_plans`
- `provider_images`
- `catalog_sync_runs`
- `sellable_offers`
- `price_books`, `price_book_entries`

## Wallet/payments
- `wallets`
- `ledger_entries`
- `wallet_holds`
- `payment_sessions`
- `payment_events`

## Compute
- `servers`
- `server_price_snapshots`
- `server_credentials` (encrypted/short-lived where applicable)
- `resource_operations`
- `provider_actions`
- `resource_events`

## Billing
- `billing_periods`
- `usage_segments`
- `charges`
- `billing_runs`

## Reliability/integration
- `outbox_events`
- `inbox_deduplication`
- `job_leases`

## Governance
- `audit_events`
- `abuse_cases`
- `admin_notes`
- `risk_flags`

### Key uniqueness/idempotency constraints
- ledger entry: unique `(wallet_id, idempotency_key)`
- resource operation: unique `idempotency_key`
- provider resource: unique `(provider_account_id, provider_server_id)`
- Telegram identity: unique `telegram_user_id`
- payment event: unique `(gateway_key, external_event_id)`
- outbox event: primary key UUID; processing is at-least-once with consumer idempotency
