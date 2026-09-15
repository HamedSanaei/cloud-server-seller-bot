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

Individual feature/fix tasks do not need to satisfy global coverage thresholds.

For targeted tests:

- verify correctness of the changed behavior
- do not evaluate repository-wide coverage impact

The `--cov-fail-under=88` floor is enforced by the CI `test` job, which runs the full
suite on every pull request and every push to `main`. Do not add coverage thresholds
to Level 1/Level 2 local runs: a partial suite under-reports by construction, so its
coverage number is not comparable to the repository-wide floor. Coverage output from a
targeted run may be read for debugging, but must never be reported as a pass/fail gate.

If a change genuinely lowers repository-wide coverage, that is CI's finding to make, not
the agent's to pre-empt by running the full suite early.

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
