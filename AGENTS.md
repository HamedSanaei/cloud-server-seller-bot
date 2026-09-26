# Repository instructions for Codex supervisor and sub-agents

## Mission
Build a maintainable Telegram-first cloud server platform. Hetzner is provider #1; the domain must remain provider-neutral so additional providers, including Iranian infrastructure providers, can be added through adapters.

## Supervisor/sub-agent policy
- Codex is the supervisor and final integrator.
- Prefer delegating bounded implementation/research/test tasks to the two already-configured Hetzner-backed LiteLLM sub-agents whenever tasks can be parallelized.
- Do not have two agents edit the same files concurrently.
- Supervisor owns cross-module architecture, schema migrations, dependency changes, public contracts and merge/integration decisions.
- For risky changes, assign one sub-agent to implement and the other to review/tests rather than duplicate implementation.
- A sub-agent must receive a task contract with: goal, allowed files, forbidden files, dependencies, acceptance criteria, test command and expected handoff.
- Sub-agents must not broaden scope. If a required cross-boundary change is discovered, return it to the supervisor as a blocker/proposal.
- Supervisor must inspect diffs, run required gates and update task status before considering work complete.

## Task source of truth
`docs/roadmap/TASKS.yaml` is the machine-readable backlog. `docs/roadmap/MILESTONES.md` explains product sequencing.

When asked to "continue the project" or "do the next tasks":
1. Select the highest-priority `ready` tasks whose dependencies are satisfied.
2. Parallelize only tasks with non-overlapping write scopes.
3. Create a short task contract for each sub-agent.
4. Integrate their work.
5. Run repository gates.
6. Update task status/evidence in the backlog or corresponding task note.
7. Summarize exactly what changed, what was verified, and the next ready tasks.

## Architecture invariants
- Modular monolith first; service extraction later only with measured need.
- Domain modules cannot import provider adapters, Telegram framework, FastAPI or database ORM models.
- Provider adapters implement ports under `cloud_platform.providers.base`.
- External mutations must be idempotent and reconciled.
- Wallet ledger is append-only/immutable.
- Financial calculations never use float.
- Database state changes plus emitted integration events must use transactional outbox semantics once persistence is implemented.
- Provider secrets must use encrypted secret storage; `.env` is local-development only.
- Never hard-code live provider prices. Catalog sync and explicit price books own prices.
- User-facing commands must enforce ownership and authorization in application services, not only at the UI layer.

### Storefront navigation invariants

- A provider configured with multiple commercial product families must show
  the family selector even when only one family currently has sellable
  inventory.
- Current inventory controls availability/count, not whether a configured
  family concept exists.
- Customer location navigation groups provider locations by normalized
  country/city before exposing provider-specific datacenter/hall ids.
- Country flags derive only from normalized ISO `country_code`; generic UI
  must never infer geography from provider-specific location codes.
- Provider-specific location normalization belongs in the provider adapter.
- Sibling datacenters in one city must be differentiated with real catalog
  facts (price/count/metadata), not unexplained "Location 1/2/3" labels.
- Hourly catalog sellability must prove that the pinned credential can supply
  all mandatory checkout/provisioning inputs, not merely list the instance
  type.

### Provider API contract rules

- Provider adapters must be tested against fixtures shaped like the
  provider's official documented payload, including envelope-level metadata.
- Do not flatten provider responses in mocks in ways that differ from
  official schemas.
- Financial fields must be sourced from their documented location in the
  response; no inferred currency/default.
- When official hourly prices have more precision than the platform's normal
  currency minor unit, preserve enough precision for correct accrual rather
  than silently rounding per-hour rates.

## Development lanes: `staging` (fast) and `main` (release-grade)

> **Staging iteration speed is a product requirement.**
> Never run release-grade local validation on a `staging` task unless the OWNER
> explicitly asks for release/main validation. The `staging` rule in this file
> overrides every generic gate wording that appears after it.

Two lanes, two risk profiles. Choose the lane by what the change is FOR, never
by convenience.

**`staging` — the active development/integration lane.** Every push to
`staging` runs `.github/workflows/deploy-staging.yml`: fast checks (`ruff
check`, `ruff format --check`, `compileall` over `src`, api/worker/bot import
smoke) -> cached Docker build -> immediate deploy of the exact pushed SHA to
the CURRENT server and the SAME Telegram bot. It does not wait for `ci`, does
not run the full suite, does not compute coverage and does not run
`pre-commit run --all-files`.

Normal feature development is therefore:

1. implement the change;
2. run the tests for the code actually touched (see the staging contract
   below — targeted only, never the whole suite);
3. run the staging gate locally: `make staging-check`;
4. inspect `git status --short` and the diff;
5. commit when the owner has asked for commits;
6. push to `staging` and let the deploy expose the behavior in Telegram.

An agent may report exactly:

```text
Staging-ready. Targeted validation passed. Full release suite was not executed.
```

It MUST NOT describe that as `production-ready`, `fully validated`, or `ready
to push`.

### Staging-local validation contract (authoritative)

If the target branch is `staging`, the required local validation is ONLY:

1. the tests directly related to the changed behavior, and
2. `make staging-check` — or, where `make` is unavailable (this Windows
   workstation has no `make` on PATH), the exact equivalent
   `uv run python scripts/verify_ci.py --staging`.

One command covers the whole contract:
`uv run python scripts/verify_ci.py --staging --test <exact test> [--test ...]`
(`make staging-test TESTS="<exact test> ..."`).

Nothing else is mandatory. In particular, a `staging` task MUST NOT
automatically run:

```text
uv run python scripts/verify_ci.py --push-ready
uv run pytest                       # the whole suite
uv run pytest --cov=...             # coverage
uv run mypy src
uv run pre-commit run --all-files
all of tests/live/*
docker build / docker compose up|down
tests/unit/test_deploy_production.py::TestEntrypointContract::test_built_image_executes_through_the_real_entrypoint
```

Those belong to `main`/release validation. This holds EVEN when the change
touches migrations, database repositories, `core/container.py`, shared
services, provider code or the business logger: validate the affected risk.

| Changed risk | Staging validation (then `make staging-check`) |
| --- | --- |
| Migration | the affected migration/unit test + `scripts/check_migrations.py` |
| PostgreSQL-specific behavior | ONLY the exact relevant real-PostgreSQL test(s) |
| Bot/UI | the exact related bot tests |
| Leaseweb/hourly behavior | the exact hourly/Leaseweb tests |
| Business logger | the exact business-log tests + the affected hourly tests |
| Shared service/DI | the affected module tests |

Do NOT escalate a staging change into repository-wide Level-3 validation.

### Local Docker policy (staging)

Do not use Docker Desktop on the owner's machine to prepare a staging push:
no image pulls, no local image builds, no compose up/down, no
`test_built_image_executes_through_the_real_entrypoint`, no full
`tests/live/*` run. The immutable application image is built by GitHub Actions
after the push. If ONE regression genuinely depends on PostgreSQL driver
semantics, run only that exact test against an already-running test database
(or let a GitHub-hosted run prove it) instead of turning the workstation into a
release runner.

**`main` — the stable/release lane.** `ci` runs in full (static gates,
pre-commit, full pytest with the coverage floor, migration/provider/artifact
gates, live PostgreSQL contracts). `deploy-production` no longer deploys
automatically: a release is an explicit `workflow_dispatch` with a full SHA
that is an ancestor of `main`. Automatic release delivery may only be restored
together with a server/bot of its own (see `docs/operations/PRODUCTION_DEPLOY.md`).

Full validation (and the coverage floor) applies ONLY to `main`/release work:

- release/merge preparation into `main`;
- a release build that includes database migrations with broad risk;
- security/authentication changes where full validation is warranted;
- payment settlement / critical financial changes;
- shared architecture changes;
- any explicit owner request for a staging change.

For a `staging` push NONE of these apply: the staging contract above is the
whole requirement, regardless of which subsystem the change touches.

### Staging environment facts (do not violate)

- One server, one compose project (`cloud-platform-production`), one
  server-owned `configuration.toml`/`deploy.env` and ONE Telegram bot token for
  both lanes. There must never be two processes polling that token: compose
  pins `bot` to `replicas: 1` and the deploy script refuses anything but
  exactly one running bot container.
- Do NOT create a second database, Redis, deploy path, bot, or duplicate
  infrastructure secrets. `deploy/staging/docker-compose.yml` (M12-004)
  describes a separate isolated stack and is NOT the staging lane.
- Both lanes share the deploy engine `scripts/deploy-production.sh`.
  `DEPLOY_PROFILE=staging` skips only the one-shot provider catalog refresh
  (the worker's scheduled coordinator owns provider facts) and keeps every
  other gate, including migrations, schema parity, single-bot, health and
  storefront readiness. A failed staging deploy rolls back exactly like a
  release deploy.
- Staging deploys are `staging-deploy` with `cancel-in-progress: true`: the
  newest push wins and superseded runs are cancelled. The manual release
  deploy joins `telegram-shared-host-deploy`, so the two lanes never mutate
  the host at the same time.
- Never weaken the release lane to make staging faster, and never claim one
  lane's evidence for the other.

## Quality gates

Which gates are mandatory depends ONLY on the lane the change targets.

**Target branch `staging` — the normal feature-development lane:**

```bash
uv run pytest <the exact tests related to the changed behavior>
make staging-check          # ruff check, ruff format --check, compileall, import smoke
```

That is the COMPLETE required local validation. Nothing else is mandatory, no
matter which subsystem the change touches (see the staging contract above).

**Target branch `main` / a release:**

```bash
uv run python scripts/verify_ci.py --push-ready
```

which is the only mode that requires `mypy src`, `pre-commit run --all-files`,
the full suite and the coverage floor.

`mypy src` is a `main`/release gate: it is required for release-bound work and
runs in `ci` on every pull request, but it is NOT a per-task requirement for
staging iterations. Do not claim a gate passed unless it was actually run.

## Testing policy

Lane precedence comes FIRST: for a `staging` change the required tests are only
the ones directly related to the changed behavior, followed by
`make staging-check`. The layered levels below never escalate a staging push
into repository-wide validation — they define how to pick the affected risk,
and (for `main`) when the whole suite is warranted.

Do NOT automatically run the entire test suite after every change.

### Level 1 — Targeted tests (every change, both lanes)

Run only the tests directly related to the modified code:

| Change | Command |
| --- | --- |
| Bot UI | `pytest tests/unit/test_bot_*.py` |
| Leaseweb provider | `pytest tests/unit/test_leaseweb*.py` |
| Payments | `pytest tests/unit/test_payment*.py` |
| FX | `pytest tests/unit/test_fx*.py` |
| Business logger | `pytest tests/unit/test_business_log.py` (+ the affected feature tests) |
| Hourly cloud/Leaseweb | `pytest tests/unit/test_hourly_*.py tests/unit/test_leaseweb*.py` |

Prefer naming the exact test ids over whole globs; a targeted run is evidence
about the changed behavior only.

### Level 2 — Related module validation (affected scope, still targeted)

Run when the change touches shared components: `core/config.py`,
`core/container.py`, database models, shared services or dependency injection.
Requires the affected module tests plus the related integration tests — NOT the
full suite, and NOT (for staging) `mypy` or coverage.

### Database integer contract

- Values passed to PostgreSQL BIGINT APIs, including advisory-lock keys,
  must be validated against signed int64 bounds.
- Deterministic byte/hash-derived integers must specify signedness explicitly.
- Infrastructure primitives whose correctness depends on PostgreSQL semantics
  require at least one real-PostgreSQL integration test; mock-only tests are
  insufficient.
- A push-ready catalog-sync change must exercise the coordinator far enough
  to acquire its real PostgreSQL advisory lock; provider tests alone do not
  prove the periodic pipeline can start.

### Level 3 — Full suite (main/release ONLY)

Run the full pytest suite when PREPARING MAIN/RELEASE work:

- preparing a merge into `main`
- a release build or a release-blocking change: database migrations, shared
  architecture, authentication/security, payment settlement, core
  infrastructure that affects many modules
- explicitly requested by the owner for a staging change

A `staging` push NEVER inherits these conditions: a migration, a shared
architecture change, an auth change or a payment change on `staging` is still
validated with its affected targeted tests plus `make staging-check`.

## Coverage policy

Coverage threshold checks are required only for:

- pull requests targeting `main`
- release preparation
- major architectural changes
- any task being declared ready to push to `main`

Individual feature/fix tasks do not need to satisfy global coverage thresholds
during iterative development, and a `staging` push NEVER needs one — do not
start a coverage run to prepare a staging push. Coverage belongs to pull
requests targeting `main`, releases and `main`-bound work.

For targeted tests:

- verify correctness of the changed behavior
- do not evaluate repository-wide coverage impact

The `--cov-fail-under=88` floor is enforced by the CI `test` job, which runs the full
suite on every pull request and every push to `main`. Do not add coverage thresholds
to Level 1/Level 2 local runs: a partial suite under-reports by construction, so its
coverage number is not comparable to the repository-wide floor. Coverage output from a
targeted run may be read for debugging, but must never be reported as a pass/fail gate.

During iterative development, targeted tests are still preferred. Do NOT run the
expensive complete coverage suite after every tiny edit. BUT once a task is being
declared ready for push to `main` (see Push-ready / main-bound validation), the exact
repository-wide CI coverage gate MUST be executed locally. CI must not be the first
place where a push-ready change discovers the 88% coverage failure.

## Push-ready / main-bound validation

If the target branch is `staging`, STOP — this section does not apply.

Any task whose result is expected to be pushed directly to `main`, released,
deployed, or handed to the owner as "ready to push" MUST reproduce the
repository's real GitHub Actions gates locally before completion. A push to
`staging` is NOT such a task: it needs the targeted tests plus
`make staging-check`, not this section.

For such tasks the agent MUST run (or equivalently `uv run python scripts/verify_ci.py --push-ready`):

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src

uv run pre-commit run --all-files

mkdir -p .ci-artifacts
uv run pytest \
  --junitxml=.ci-artifacts/pytest-results.xml \
  --cov=src \
  --cov-report=xml:.ci-artifacts/coverage.xml \
  --cov-report=term \
  --cov-fail-under=88

uv run python scripts/check_migrations.py
uv run python scripts/check_domain_provider_branching.py
uv run python scripts/gen_leaseweb_coverage.py --check
uv run python scripts/validate_tasks.py
uv run python scripts/check_generated_artifacts.py
```

The agent MUST NOT say "ready to push", "all green", or "full validation
passed" unless all applicable commands actually exited 0.

## Generated-artifact hygiene

Agents must NEVER commit generated validation artifacts, including:

```text
pytest-results.xml
junit.xml
coverage.xml
.coverage*
htmlcov/
.ci-artifacts/
test-results/
temporary logs
local test reports
```

Generated reports belong under the git-ignored `.ci-artifacts/` directory
(and CI upload artifacts), never in source control. Do NOT allowlist a
generated report's secret-scanner finding, do NOT add it to
`.secrets.baseline`, and do NOT weaken detect-secrets to accommodate one:
delete the artifact from tracking instead.

Before completion of any code task, agents MUST run:

```bash
git status --short
```

and inspect EVERY listed file. A generated report must not be included
simply because `git add .` would include it.

For push-ready tasks additionally verify:

```bash
git ls-files pytest-results.xml coverage.xml
```

returns no generated report files.

After running full CI-equivalent validation, run:

```bash
git status --short
```

AGAIN, because the validation itself may generate files. If validation
changed tracked files unexpectedly, the task is NOT complete.

## Tracked vs untracked pre-commit rule (main/release)

Applies to `main`/release work; `pre-commit` is not part of staging validation.
`pre-commit run --all-files` primarily validates files known to Git. Newly
created/untracked files must not be assumed covered by that statement.

Before final validation, inspect:

```bash
git status --short
```

Explicitly include new files or validate the tracked+untracked working set
when necessary. For secret scanning, use the robust check that covers
untracked files (Unix):

```bash
git ls-files -co --exclude-standard -z \
  | xargs -0 uv run pre-commit run detect-secrets --files
```

On Windows/PowerShell, use an equivalent explicit-file invocation instead of
copying the Unix pipeline blindly (e.g. pass the listed files explicitly to
`uv run pre-commit run detect-secrets --files`).

Do NOT require this expensive explicit scan after every small edit. Require it
for push-ready/security-sensitive changes or when new files were created.

## Pre-commit idempotency (main/release)

Applies when pre-commit is actually run (`ci`, `--push-ready`, or an explicit
owner request). If a pre-commit hook modifies a file, the validation FAILED
even if the hook repaired it. The agent must:

1. inspect the modification
2. re-run relevant tests if needed
3. run pre-commit again

The FINAL pre-commit run must exit 0 AND modify zero files. This specifically
prevents `end-of-file-fixer modified file` from being reported as a
successful gate.

## Test reporting

Every task completion report must state the tests executed, their result, and why those tests were selected:

```
Tests executed:
- <command> — <result> — <reason selected>
```

If the full suite was not executed, do not claim the project is fully validated. Use this wording:

> Targeted validation completed. Full suite not executed because the change scope does not require it.

For a staging-lane change that wording is:

> Staging-ready. Targeted validation passed. Full release suite was not executed.

Do not claim a coverage threshold passed unless the run that enforced it actually happened
(see Coverage policy). Report targeted runs as correctness evidence, not as coverage evidence.

## Avoid unnecessary verification loops

Do not repeatedly run expensive commands after every small edit. Expensive checks include the full pytest suite, full coverage calculation, a complete Docker rebuild and full deployment validation. Run them only when the Testing policy or Coverage policy requires it.

## Security constraints
- Never print provider/payment secrets in logs or test snapshots.
- Use structured redaction for tokens, Authorization headers, passwords and cloud-init secrets.
- Destructive provider operations require explicit application-layer authorization and an idempotency key.
- Do not implement "temporary" shortcuts that allow negative wallet races, double provisioning or deletion without reconciliation.

## Production Server Access

The production environment of this product runs on the server/service
identified as:

- Production service: `Leaseweb (DE rep for fin)`

When an issue is production-only (runtime behavior, logs, container/service
state, deployed revision, connectivity) and the local repository cannot
establish the facts, connect DIRECTLY to that server with OpenSSH. Production
access is a normal diagnostic capability, not a last resort: a bug report that
depends on production runtime state must not be answered from source-code
assumptions alone.

### SSH usage

- Prefer the existing OpenSSH configuration under the current user's `~/.ssh/`
  directory, and resolve the host entry for `Leaseweb (DE rep for fin)` from it.
- NEVER guess or invent an IP address, hostname, username, password, port or
  private-key path. If the host does not resolve from the configuration, stop
  and ask instead of guessing.
- Never print, expose, copy or commit private SSH keys, passwords, tokens or
  other secrets.
- Use non-interactive SSH commands where practical:
  `ssh <configured-host> "<diagnostic-command>"`.
- On this Windows development machine, if a normal OpenSSH invocation does not
  work from the Desktop Commander shell, the configured SSH entrypoint is
  `C:\Users\Hamed\.ssh\ssh-dc.cmd` (a thin wrapper that runs the
  Git-for-Windows `ssh.exe` against the same `~/.ssh` configuration).
- This repository is PUBLIC: host addresses, credentials and server-specific
  secrets must never be written into tracked files, tests, logs or issue text.

### What production access may be used for

Read-only inspection is expected, proactively, whenever it materially helps:

- reading application/service logs;
- Docker/compose container status, logs and health;
- systemd unit status and `journalctl` entries;
- inspecting runtime configuration WITHOUT exposing secret values;
- checking the deployed commit/version (the running platform image tag);
- checking application health and storefront readiness
  (`docker compose ... exec -T api python -m cloud_platform.cli ...`);
- verifying database/network/service connectivity;
- confirming whether a reported issue exists only in production;
- validating the result of an already-authorized deployment.

The deployed stack is `deploy/production/docker-compose.yml` (compose project
`cloud-platform-production`) at the server path the deploy pipeline uses
(`PROD_DEPLOY_PATH`; see `docs/operations/PRODUCTION_DEPLOY.md`). The server owns
`deploy.env` and `configuration.toml`: inspect their KEYS if needed, but never
print, copy or commit their values.

### Production safety rules

Production access is diagnostic/read-only by default. Without an explicit
instruction from the user, DO NOT:

- modify production files or configuration;
- edit environment variables or secrets;
- modify database records or schema;
- restart or redeploy services merely as an experiment;
- delete logs, files, containers, volumes or data;
- run migrations;
- install or remove packages;
- run destructive Docker/system/database commands;
- change firewall, network or SSH settings;
- run `git reset`, `checkout`, `pull` or otherwise modify the production
  working tree;
- start a second stack, database, Redis or Telegram bot poller on that host:
  exactly one bot polls the shared token, and services are replaced only by the
  `staging` fast lane or an explicit `deploy-production` dispatch.

If a production change is necessary, determine the root cause first and make the
source-controlled fix in the repository whenever appropriate. Mutate production
only when the user's task clearly authorizes that change, and prefer a pipeline
deploy over an ad-hoc edit on the server.

### Investigation workflow

For production-related failures, use this order when appropriate:

1. Inspect the local code/configuration.
2. Connect to `Leaseweb (DE rep for fin)` through the configured OpenSSH host.
3. Inspect the relevant production logs/status/runtime state.
4. Correlate the production evidence with the source code.
5. Identify the actual root cause.
6. Implement the durable fix in source control.
7. Run the smallest relevant local tests first.
8. Deploy or modify production only when the task authorizes it.
9. Verify the resulting production behavior through SSH/logs if useful.

## Commit & contribution policy

- Agents must NEVER create commits, tags or releases, and must never push to a remote repository, unless explicitly requested by the repository owner.
- No agent may put its own name in a commit: no author/committer identity changes, and no "Co-Authored-By", "Generated with <tool>", signature or attribution footers of any kind.
- No agent may become a repository contributor: never add agent identities to CONTRIBUTORS files, git config `user.name`/`user.email`, repository metadata, tags or releases.
- Commits are authored solely under the repository owner's identity. Agents must never create commits, tags or releases attributed to themselves.
- Do not modify git author/committer configuration.
- After completing every code change task, the agent MUST provide a suggested commit message (even when it did not commit).

### Foreign catalog currency invariant

- Provider-native cost/currency must be preserved exactly for audit.
- Foreign storefront selling prices use the configured canonical catalog currency; currently USD.
- Cross-currency auto-pricing converts provider cost before markup.
- FX arithmetic is Decimal-only and must preserve exact provider hourly rates until the final customer-currency rounding boundary.
- A sellable foreign offer may not use a selling currency different from the configured catalog currency.
- Existing order/hourly-instance price snapshots are immutable; later FX movements reprice catalog offers only, not accepted contracts.
- Temporary FX failure must never cause a guessed rate, 1:1 conversion or mass catalog retirement.
- External FX calls scale with distinct currency pairs, never offer count.
- The platform supports two FX families: domestic/Iranian (AbanTether) and global fiat (Frankfurter). Do not conflate them.
- Wallet settlement is atomic (balance mutation plus its immutable ledger fact
  in one transaction); a consumed idempotency key with different facts fails
  closed instead of replaying quietly.

### Suggested commit message format

Conventional Commits:

```
<type>(<scope>): <short description>
```

Examples:

```
feat(leaseweb): aggregate catalog across multiple credentials
fix(bot): restore main menu keyboard routing for unknown messages
fix(payments): prevent duplicate payment credit settlement
refactor(fx): centralize currency conversion formatting
```

The final report must include:

```
Suggested commit:
`<commit message>`

Summary:
- files changed
- behavior changed
- tests executed
- any migration/configuration impact
```

## Documentation discipline
Update architecture docs only when the invariant/contract truly changes. Avoid giant session-history docs. Preserve durable decisions, public contracts, task evidence and operational runbooks.

## Final task completion format

Every completed task must end with:

```markdown
## Completion summary

Changed:
- ...

Validation:
- ...

Suggested commit:
`type(scope): description`

Next steps:
- ...
```

The final report MUST distinguish intended source changes from
generated/unexpected files. Run:

```bash
git status --short
git diff --check
```

and inspect the output. The whole worktree is not required to be clean (the
agent may correctly have uncommitted implementation changes), but there must
be ZERO accidentally tracked/generated CI reports.
