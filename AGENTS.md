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
Before marking an implementation task done, run the narrowest relevant tests plus, when practical:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

Do not claim a gate passed unless it was actually run.

## Security constraints
- Never print provider/payment secrets in logs or test snapshots.
- Use structured redaction for tokens, Authorization headers, passwords and cloud-init secrets.
- Destructive provider operations require explicit application-layer authorization and an idempotency key.
- Do not implement "temporary" shortcuts that allow negative wallet races, double provisioning or deletion without reconciliation.

## Commit & contribution policy
- No agent may put its own name in a commit: no author/committer identity changes, and no "Co-Authored-By", "Generated with <tool>", signature or attribution footers of any kind.
- No agent may become a repository contributor: never add agent identities to CONTRIBUTORS files, git config `user.name`/`user.email`, repository metadata, tags or releases.
- Commits are authored solely under the repository owner's identity. Agents must never create commits, tags or releases attributed to themselves.
- Do not modify git author/committer configuration.

## Documentation discipline
Update architecture docs only when the invariant/contract truly changes. Avoid giant session-history docs. Preserve durable decisions, public contracts, task evidence and operational runbooks.
