# Cloud Server Platform Starter

A production-oriented starter for a Telegram-first, hourly-billed cloud server platform.

The first provider is Hetzner Cloud. The architecture is intentionally provider-neutral so Iranian or other providers can be added later without rewriting wallet, billing, Telegram UX, or orchestration.

## Architecture in one line

Telegram Bot / REST API -> Application Services -> Domain Modules -> Provider Ports -> Provider Adapters -> Hetzner / Future Providers

Persistent state lives in PostgreSQL. Redis is used for ARQ jobs, short-lived locks and cache. Long-running provider mutations are modeled as idempotent operations and reconciled asynchronously.

## Why a modular monolith first?

- One deployable codebase is much easier to operate while the product is young.
- Domain boundaries are explicit from day one.
- Modules communicate through application interfaces/events, not table reach-through.
- Hot modules can later be extracted into services with minimal redesign.

## Current starter scope

Implemented skeletons:

- FastAPI service and health endpoints
- aiogram Telegram bot shell
- ARQ worker shell
- typed settings
- provider interface/capability model
- Hetzner adapter skeleton with rate-limit metadata and error mapping
- provider registry
- pricing/billing primitives
- immutable wallet-ledger domain primitives
- compute lifecycle state model
- SQLAlchemy base/session wiring
- test scaffolding
- Docker Compose for PostgreSQL + Redis + app services
- Codex supervisor rules and sub-agent playbook
- machine-readable backlog plus milestone roadmap

This is intentionally a **starter**, not a finished reseller. Real payments, production migrations, secrets/KMS, fraud controls, provider reconciliation, and full Telegram flows are planned as tasks rather than faked.

## Local development

1. Copy `.env.example` to `.env`.
2. Put a Hetzner test/project token in `HETZNER_API_TOKEN` only for local testing.
3. Install dependencies:

```bash
uv sync --all-groups
```

4. Start infrastructure:

```bash
docker compose up -d postgres redis
```

5. Run API:

```bash
uv run uvicorn cloud_platform.api.app:create_app --factory --reload
```

6. Run worker:

```bash
uv run arq cloud_platform.worker.settings.WorkerSettings
```

7. Run bot:

```bash
uv run python -m cloud_platform.bot.main
```

## Tests and quality

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

## Development with Codex + sub-agents

Read these first:

- `AGENTS.md`
- `docs/agents/SUPERVISOR_PLAYBOOK.md`
- `docs/agents/TASK_CONTRACT.md`
- `docs/roadmap/MILESTONES.md`
- `docs/roadmap/TASKS.yaml`

Codex is the supervisor. The two Hetzner/LiteLLM sub-agents should do bounded implementation work in parallel. The supervisor owns architecture, schema coordination, task assignment, integration, conflict resolution and final verification.

## Non-negotiable invariants

1. Money is represented as integer minor units or `Decimal`; never binary float.
2. Every external mutation has an idempotency key.
3. Provider state is never trusted blindly; reconciliation is mandatory.
4. Wallet ledger entries are immutable.
5. No cloud provider SDK/type leaks into domain modules.
6. Never store plaintext API tokens in the database.
7. A server's provider cost and customer selling price are separate snapshots.
8. Deletion is a saga: requested -> provider confirmed absent -> billing finalized.
9. User-facing resource ownership is always checked server-side.
10. Production destructive actions need audit trails.
