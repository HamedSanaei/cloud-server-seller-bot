# Architecture

## Goals

- Telegram-first UX with a stable REST API underneath.
- Hourly/PAYG resale without double charges or double provisioning.
- Multi-provider from the domain perspective; Hetzner is simply the first adapter.
- Easy addition of Iranian providers, even if their APIs differ significantly.
- Start operationally simple, scale through clear seams rather than premature microservices.

## Frozen dependency rules (baseline)

- `cloud_platform.modules` domain code may import only the standard library and
  provider-neutral utilities from `cloud_platform.core`.
- Domain code must not import `cloud_platform.providers`, `cloud_platform.db`, SQLAlchemy,
  aiogram, FastAPI or any other transport, persistence or provider-adapter package.
- Provider adapters implement the normalized port in `cloud_platform.providers.base`; payment
  gateway adapters implement ports owned by the payments module. Adapters may depend on domain
  types, never the reverse.
- These rules are enforced by review until a later bootstrap task adds an automated import-boundary
  gate.

## Context

```text
                 +----------------------+
                 |      Telegram        |
                 +----------+-----------+
                            |
                      aiogram adapter
                            |
+----------+       +--------v---------+       +----------------+
| Admin UI | ----> | Application/API  | <---- | Payment Webhook |
+----------+       +--------+---------+       +----------------+
                            |
       +--------------------+-----------------------------+
       |                    |              |              |
  Identity/User          Compute         Wallet        Catalog
       |                    |              |              |
       +--------------------+------+-------+--------------+
                                  |
                             Billing/Orders
                                  |
                         Provider Port / Registry
                                  |
                +-----------------+-----------------+
                |                                   |
          Hetzner Adapter                    Iran Provider Adapter
                |                                   |
          Hetzner Cloud API                    Future APIs

PostgreSQL: source of truth
Redis/ARQ: jobs, short-lived locks/cache
Outbox: reliable async integration events
Observability: logs + metrics + traces
```

## Deployment shape (phase 1)

Three process types from one codebase:

- `api`: FastAPI, webhooks and future admin API.
- `bot`: Telegram update adapter. It calls application services; it does not contain business rules.
- `worker`: provisioning, reconciliation, billing, catalog synchronization and notification jobs.

They share PostgreSQL and Redis. This provides independent horizontal scaling without microservice data ownership complexity.

## Domain modules

### Identity
Telegram users, roles, status, terms acceptance, optional verification/risk metadata.

### Wallet
Append-only ledger. Balance is derived/cached from ledger entries. Writes are serialized/guarded so concurrent purchases cannot overspend.

### Payments
Gateway-neutral payment sessions and webhook verification. Iranian gateways are adapters, not wallet logic.

### Catalog
Normalized locations, plans, images and capabilities. Provider catalog is synchronized and mapped to sellable offers.

### Pricing
Price books, margins, promotions, currency conversion policy and immutable purchase-time price snapshots.

### Compute
Server aggregate and lifecycle. Owns desired state, provider identity mapping and user ownership.

### Provisioning/Operations
Idempotent operation records, retries, provider calls, action tracking and reconciliation.

### Billing
Usage windows, provider-cost snapshot, customer-charge snapshot, quantum policy, monthly caps (optional), ledger posting and finalization.

### Audit/Abuse
Immutable audit trail for sensitive operations plus abuse-case workflow. Provider abuse correspondence should eventually map to resource/user/provider-account.

### Notifications
Telegram and future email/SMS adapters. Delivery is asynchronous and retryable.

## Provider abstraction

The domain never depends on Hetzner request/response shapes. A provider adapter maps the provider into normalized contracts:

- locations
- plans
- images
- server lifecycle
- power operations
- networking/IP features
- snapshots/backups
- pricing/catalog metadata
- capability flags

A future Iranian provider may not support every capability. UI and application services query capability flags and degrade gracefully.

## Provider accounts

Do not assume exactly one Hetzner token forever. Persist a logical `provider_account` entity with:

- provider key
- encrypted credentials reference
- status/health
- capacity/quota metadata
- allowed locations/plans
- allocation weight/priority
- rate-limit snapshot
- risk/maintenance state

This lets the platform spread load over multiple Hetzner projects/accounts or migrate resources to other providers later.

## Idempotency and reconciliation

External cloud APIs are distributed systems. Timeouts can occur after the provider has created/deleted a resource.

Every mutation therefore gets an internal operation row with a unique idempotency key before the provider call. The operation records intent and provider correlation data. Retrying never blindly repeats creation: workers first reconcile labels/known IDs/state.

Deletion is considered complete only after the provider confirms the resource no longer exists. Billing finalization is tied to confirmed lifecycle timestamps rather than Telegram button timing.

## Billing correctness

Provider cost and customer price are separate immutable snapshots on the resource/order. Live catalog changes must never alter historical charges.

Hetzner-style rounded hourly cost is one policy. A future provider may bill per minute/day/month; that is represented by policy + provider-specific cost facts, not special cases in the Telegram bot.

Recommended financial model:

1. Check available wallet balance.
2. Create a reservation/hold for minimum required spend.
3. Persist provisioning operation and server intent transactionally.
4. Provision asynchronously.
5. Periodically accrue/finalize usage into immutable ledger entries.
6. If balance is insufficient, follow configurable grace/notification/auto-delete policy.
7. On deletion, reconcile provider absence then finalize the last charge and release remaining holds.

## Transactional outbox

Any transaction that changes durable state and needs an asynchronous side effect also writes an outbox record in the same DB transaction. A worker publishes/processes it after commit. This prevents "DB committed but job enqueue failed" gaps.

## Scaling plan

Before extracting services, scale process types horizontally and partition work queues. Candidate future service extractions only when measured:

- provider orchestration workers (high I/O)
- billing/ledger (high correctness/isolation)
- notifications
- analytics/reporting

Keep PostgreSQL as source of truth until data ownership pressure justifies separate stores.

## Rate limiting

Provider adapters expose rate-limit snapshots. Workers use bounded batches, jittered retry/backoff and per-provider-account concurrency controls. Do not poll asynchronous provider actions aggressively.

## Security

- API tokens encrypted at rest using envelope encryption/KMS or a secrets manager.
- Strict redaction in logs.
- Telegram identity cannot be the only authorization check; every resource lookup is scoped to internal user ID.
- Webhook signature/verification and replay protection.
- Admin RBAC and step-up confirmation for destructive bulk actions.
- Cloud-init/user-data treated as secrets.
- SSH credentials preferably public-key based; generated passwords, if used, should be one-time display/encrypted short-lived secrets.

## Iran provider readiness

The provider port intentionally does not require "Hetzner concepts" such as network zones. Each adapter advertises capabilities and supplies metadata. Provider-specific fields remain opaque metadata unless they become product-level concepts across two or more providers.
