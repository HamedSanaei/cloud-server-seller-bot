# Task backlog

**Total starter tasks: 139**

Statuses in `TASKS.yaml` are authoritative. Dependencies determine what Codex should pick next.

## M00 — Bootstrap & agent workflow
Make the repo reproducible and safe for multi-agent development.

| ID | Pri | Owner hint | Task |
|---|---|---|---|
| M00-001 | P0 | supervisor | Freeze baseline architecture and invariants |
| M00-002 | P0 | H1 | Initialize uv lockfile |
| M00-003 | P1 | H1 | Add pre-commit hooks |
| M00-004 | P0 | H2 | Add CI skeleton |
| M00-005 | P0 | supervisor | Add sub-agent task contract workflow |
| M00-006 | P1 | H2 | Add conventional task evidence format |

## M01 — Platform foundation
Database schema, migrations, outbox, logging and dependency wiring.

| ID | Pri | Owner hint | Task |
|---|---|---|---|
| M01-001 | P0 | supervisor | Create Alembic environment |
| M01-002 | P0 | supervisor | Implement user/provider/catalog base schema |
| M01-003 | P0 | supervisor | Implement wallet/ledger schema |
| M01-004 | P0 | supervisor | Implement compute/operation schema |
| M01-005 | P0 | supervisor | Implement billing schema |
| M01-006 | P0 | H1 | Implement outbox schema and repository |
| M01-007 | P0 | H2 | Implement structured logging and redaction |
| M01-008 | P1 | H1 | Add app dependency container/factories |
| M01-009 | P1 | H2 | Add PostgreSQL/Redis readiness checks |

## M02 — Identity & Telegram onboarding
Users, Telegram identity, roles, terms and basic commands.

| ID | Pri | Owner hint | Task |
|---|---|---|---|
| M02-001 | P0 | H1 | Implement User aggregate/repository |
| M02-002 | P0 | H1 | Implement Telegram identity binding |
| M02-003 | P0 | H2 | Implement /start onboarding flow |
| M02-004 | P1 | H1 | Implement terms acceptance versioning |
| M02-005 | P0 | H2 | Implement user status freeze/ban |
| M02-006 | P0 | supervisor | Implement admin/user RBAC primitives |
| M02-007 | P2 | H2 | Add locale/message catalog abstraction |

## M03 — Provider abstraction
Provider accounts, capabilities, normalized contracts and registry.

| ID | Pri | Owner hint | Task |
|---|---|---|---|
| M03-001 | P0 | supervisor | Persist Provider and ProviderAccount |
| M03-002 | P0 | supervisor | Implement encrypted credential envelope interface |
| M03-003 | P1 | H1 | Implement provider account health/status |
| M03-004 | P0 | H1 | Finalize capability matrix contract |
| M03-005 | P1 | H2 | Implement provider allocator interface |
| M03-006 | P0 | H2 | Add provider contract test suite |

## M04 — Hetzner catalog integration
Sync locations/plans/images/prices and expose sellable offers.

| ID | Pri | Owner hint | Task |
|---|---|---|---|
| M04-001 | P0 | H1 | Implement Hetzner auth/connectivity probe |
| M04-002 | P0 | H1 | Implement paginated location sync |
| M04-003 | P0 | H1 | Implement paginated server-type sync |
| M04-004 | P0 | H2 | Implement system image sync |
| M04-005 | P0 | supervisor | Implement provider pricing ingestion |
| M04-006 | P0 | H2 | Implement catalog sync job with lock |
| M04-007 | P0 | H1 | Implement sellable-offer enable/disable |
| M04-008 | P2 | H2 | Cache customer catalog safely |
| M04-009 | P0 | H1 | Handle rate-limit headers and 429 backoff |

## M05 — Wallet & accounting
Immutable ledger, holds, balance concurrency and admin adjustments.

| ID | Pri | Owner hint | Task |
|---|---|---|---|
| M05-001 | P0 | H1 | Implement wallet repository |
| M05-002 | P0 | supervisor | Implement append-only ledger posting |
| M05-003 | P0 | H2 | Implement consistent available balance query |
| M05-004 | P0 | supervisor | Implement wallet hold/reservation |
| M05-005 | P0 | H1 | Implement hold release/capture |
| M05-006 | P1 | H2 | Implement admin credit/debit adjustment |
| M05-007 | P0 | H2 | Add concurrency tests for wallet spending |
| M05-008 | P1 | H1 | Add ledger reconciliation report |

## M06 — Pricing & billing
Price books, snapshots, usage accounting, margin and billing runs.

| ID | Pri | Owner hint | Task |
|---|---|---|---|
| M06-001 | P0 | H1 | Implement price books and margin rules |
| M06-002 | P0 | supervisor | Implement immutable server price snapshot |
| M06-003 | P0 | H1 | Implement billing policy abstraction |
| M06-004 | P0 | H2 | Implement usage segment calculator |
| M06-005 | P0 | supervisor | Implement periodic accrual job |
| M06-006 | P0 | H1 | Implement final deletion charge |
| M06-007 | P0 | supervisor | Implement low-balance policy |
| M06-008 | P1 | H2 | Implement provider-vs-customer margin report |
| M06-009 | P2 | H1 | Add monthly-cap support per price policy |

## M07 — Compute provisioning lifecycle
Idempotent create/delete/power plus reconciliation.

| ID | Pri | Owner hint | Task |
|---|---|---|---|
| M07-001 | P0 | supervisor | Implement create-server application command |
| M07-002 | P0 | H1 | Implement provisioning worker |
| M07-003 | P0 | H2 | Implement create timeout reconciliation |
| M07-004 | P1 | H1 | Implement provider action waiter strategy |
| M07-005 | P0 | H2 | Implement server state reconciliation |
| M07-006 | P0 | H1 | Implement power-on/off/reboot commands |
| M07-007 | P0 | supervisor | Implement delete-server saga |
| M07-008 | P0 | H2 | Implement delete retry/reconcile |
| M07-009 | P0 | H1 | Implement orphan provider-resource detector |
| M07-010 | P0 | H2 | Implement missing-provider-resource detector |
| M07-011 | P0 | supervisor | Implement per-account provisioning concurrency limit |

## M08 — Telegram customer UX
Catalog browsing, checkout, server management and notifications.

| ID | Pri | Owner hint | Task |
|---|---|---|---|
| M08-001 | P0 | supervisor | Design Telegram navigation/state machine |
| M08-002 | P0 | H1 | Implement catalog country/location view |
| M08-003 | P0 | H1 | Implement plan selection view |
| M08-004 | P0 | H2 | Implement OS selection view |
| M08-005 | P0 | H2 | Implement purchase confirmation view |
| M08-006 | P1 | H1 | Implement provisioning progress notifications |
| M08-007 | P0 | H2 | Implement My Servers list/detail |
| M08-008 | P0 | H1 | Implement power controls UI |
| M08-009 | P0 | H2 | Implement delete confirmation UI |
| M08-010 | P1 | H1 | Implement wallet/ledger history UI |
| M08-011 | P0 | H2 | Implement low-balance notifications |

## M09 — Payments
Gateway abstraction, first Iranian payment adapter and webhook safety.

| ID | Pri | Owner hint | Task |
|---|---|---|---|
| M09-001 | P0 | supervisor | Finalize payment gateway port |
| M09-002 | P0 | H1 | Implement payment session persistence |
| M09-003 | P0 | H2 | Implement signed/replay-safe webhook endpoint |
| M09-004 | P0 | H1 | Integrate first Iranian payment gateway |
| M09-005 | P0 | supervisor | Post successful payment to ledger |
| M09-006 | P1 | H2 | Implement payment status Telegram UX |
| M09-007 | P1 | H1 | Implement payment reconciliation job |

## M10 — Admin, security & abuse
RBAC, audit, limits, abuse workflows and kill switches.

| ID | Pri | Owner hint | Task |
|---|---|---|---|
| M10-001 | P0 | H1 | Implement immutable audit repository |
| M10-002 | P0 | H2 | Audit admin wallet/provider operations |
| M10-003 | P0 | H1 | Implement provisioning quotas per user |
| M10-004 | P0 | supervisor | Implement cost circuit breakers |
| M10-005 | P0 | H2 | Implement provider/location maintenance switches |
| M10-006 | P0 | supervisor | Implement abuse case model/workflow |
| M10-007 | P0 | H1 | Implement user freeze + resource containment command |
| M10-008 | P1 | H2 | Implement secret rotation workflow |
| M10-009 | P0 | H2 | Add security regression tests |

## M11 — Reliability & observability
Metrics, tracing, retries, dashboards, backups and DR.

| ID | Pri | Owner hint | Task |
|---|---|---|---|
| M11-001 | P0 | H1 | Add Prometheus metrics |
| M11-002 | P1 | H2 | Add OpenTelemetry tracing |
| M11-003 | P0 | H1 | Implement provider retry/backoff policy |
| M11-004 | P0 | H2 | Implement dead-letter/manual-review tooling |
| M11-005 | P1 | H2 | Create Grafana dashboard definitions |
| M11-006 | P0 | supervisor | Create alert rules |
| M11-007 | P0 | H1 | Automate PostgreSQL backups |
| M11-008 | P0 | supervisor | Document/test restore drill |
| M11-009 | P1 | H2 | Add chaos tests for provider timeouts |

## M12 — CI/CD & production deployment
Quality gates, images, migrations and safe rollout.

| ID | Pri | Owner hint | Task |
|---|---|---|---|
| M12-001 | P0 | H1 | Harden GitHub Actions quality pipeline |
| M12-002 | P0 | H1 | Build and publish OCI image |
| M12-003 | P0 | supervisor | Add migration compatibility gate |
| M12-004 | P0 | H2 | Create staging compose/deployment |
| M12-005 | P0 | supervisor | Create production deployment runbook |
| M12-006 | P1 | H2 | Add rolling/blue-green strategy |
| M12-007 | P0 | H1 | Add post-deploy smoke tests |

## M13 — Advanced Hetzner features
SSH keys, rebuild/rescue, snapshots, firewall, IPs, volumes/networks.

| ID | Pri | Owner hint | Task |
|---|---|---|---|
| M13-001 | P1 | H1 | Implement SSH key CRUD/sync |
| M13-002 | P1 | H2 | Implement rebuild flow |
| M13-003 | P1 | H1 | Implement rescue mode |
| M13-004 | P1 | H2 | Implement snapshots |
| M13-005 | P2 | H1 | Implement backups toggle |
| M13-006 | P1 | H2 | Implement firewall management |
| M13-007 | P2 | H1 | Implement rDNS |
| M13-008 | P2 | H2 | Implement Primary/Floating IP features |
| M13-009 | P2 | H1 | Implement volumes |
| M13-010 | P2 | H2 | Implement private networks |

## M14 — Web panel & public API
Customer/admin web surfaces and scoped customer API tokens.

| ID | Pri | Owner hint | Task |
|---|---|---|---|
| M14-001 | P1 | supervisor | Define customer REST API v1 |
| M14-002 | P1 | H1 | Implement customer API tokens/scopes |
| M14-003 | P1 | H2 | Implement admin API |
| M14-004 | P2 | H1 | Build customer web panel shell |
| M14-005 | P2 | H2 | Build admin web panel shell |
| M14-006 | P1 | H1 | Add API rate limits |

## M15 — Iran provider expansion
Add a real Iranian provider without modifying core domains.

| ID | Pri | Owner hint | Task |
|---|---|---|---|
| M15-001 | P0 | supervisor | Select first Iranian provider and document API contract |
| M15-002 | P0 | H1 | Implement Iranian provider adapter |
| M15-003 | P0 | H2 | Implement Iranian catalog/pricing mapper |
| M15-004 | P0 | H1 | Implement provider-specific reconciliation |
| M15-005 | P0 | H2 | Enable capability-driven Telegram UX |
| M15-006 | P0 | supervisor | Run multi-provider billing parity tests |
| M15-007 | P0 | supervisor | Prove zero core-domain provider branching |

## M16 — Scale & service extraction
Load testing, queue partitioning and evidence-based extraction.

| ID | Pri | Owner hint | Task |
|---|---|---|---|
| M16-001 | P1 | H1 | Create realistic load model |
| M16-002 | P1 | H1 | Load test API and worker queues |
| M16-003 | P1 | H2 | Tune PostgreSQL indexes/queries |
| M16-004 | P1 | H2 | Partition worker queues by responsibility |
| M16-005 | P1 | H1 | Implement provider-account sharding allocator |
| M16-006 | P2 | supervisor | Evaluate billing service extraction |
| M16-007 | P2 | supervisor | Evaluate provider-worker extraction |
| M16-008 | P1 | supervisor | Run disaster/cost runaway game day |

