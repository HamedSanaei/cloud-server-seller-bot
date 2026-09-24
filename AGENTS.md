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

## Quality gates

Static gates run on every change; test scope is layered (see Testing policy).

Before marking an implementation task done, run:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

Then run the tests required by the Testing policy. Do not claim a gate passed unless it was actually run.

## Testing policy

Do NOT automatically run the entire test suite after every change. Test scope is layered.

### Level 1 — Required after every change

Run only the tests directly related to the modified code:

| Change | Command |
| --- | --- |
| Bot UI | `pytest tests/unit/test_bot_*.py` |
| Leaseweb provider | `pytest tests/unit/test_leaseweb*.py` |
| Payments | `pytest tests/unit/test_payment*.py` |
| FX | `pytest tests/unit/test_fx*.py` |

### Level 2 — Related module validation

Run when the change touches shared components: `core/config.py`, `core/container.py`, database models, shared services or dependency injection. Requires the affected module tests plus the related integration tests.

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

### Level 3 — Full suite

Run the full pytest suite ONLY when:

- preparing a merge into `main`
- changing database migrations
- changing shared architecture
- modifying authentication/security
- modifying payment settlement logic
- modifying core infrastructure that affects many modules
- explicitly requested by the owner

## Coverage policy

Coverage threshold checks are required only for:

- pull requests targeting `main`
- release preparation
- major architectural changes
- any task being declared ready to push to `main`

Individual feature/fix tasks do not need to satisfy global coverage thresholds
during iterative development.

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

Any task whose result is expected to be pushed directly to `main`, released,
deployed, or handed to the owner as "ready to push" MUST reproduce the
repository's real GitHub Actions gates locally before completion.

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

## Tracked vs untracked pre-commit rule

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

## Pre-commit idempotency

If a pre-commit hook modifies a file, the validation FAILED even if the hook
repaired it. The agent must:

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

Do not claim a coverage threshold passed unless the run that enforced it actually happened
(see Coverage policy). Report targeted runs as correctness evidence, not as coverage evidence.

## Avoid unnecessary verification loops

Do not repeatedly run expensive commands after every small edit. Expensive checks include the full pytest suite, full coverage calculation, a complete Docker rebuild and full deployment validation. Run them only when the Testing policy or Coverage policy requires it.

## Security constraints
- Never print provider/payment secrets in logs or test snapshots.
- Use structured redaction for tokens, Authorization headers, passwords and cloud-init secrets.
- Destructive provider operations require explicit application-layer authorization and an idempotency key.
- Do not implement "temporary" shortcuts that allow negative wallet races, double provisioning or deletion without reconciliation.

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
