# Codex supervisor playbook

The objective is to use Codex for judgment/integration while pushing bounded implementation volume to the two Hetzner-backed LiteLLM sub-agents.

## Good task topology

```text
                         Codex Supervisor
                     /         |          \
             Agent H1      Agent H2      Supervisor-only
             feature       tests/review   schema/contracts
                     \         |          /
                         Integration
                             |
                         Quality gates
```

## Assignment rules

### Send to sub-agents
- one module implementation with stable contract
- provider endpoint mapper
- focused tests
- documentation tied to implemented behavior
- pure refactor with fixed boundaries
- research spike that returns findings without changing architecture

### Keep with supervisor
- database schema integration when multiple modules collide
- public interface changes
- dependency/toolchain changes
- security policy decisions
- money/accounting invariants
- merging conflicting implementations
- final task status/evidence

## Two-agent patterns

### Pattern A: parallel modules
H1 implements provider catalog sync; H2 implements catalog unit tests/fixtures in separate test files.

### Pattern B: implement + adversarial review
H1 implements wallet reservation. H2 receives the interface/spec and writes race/idempotency failure cases without editing H1 files.

### Pattern C: research + build
H1 checks provider API edge cases and returns a concise contract; H2 implements against the supervisor-approved contract.

## Avoid
- both agents editing `pyproject.toml`
- both agents generating Alembic heads
- both agents changing the same domain interface
- giant prompts like "implement milestone 7"
- agents silently changing architecture to unblock themselves

## Handoff evidence
Every sub-agent response should include:
1. files changed
2. behavior implemented
3. tests run + exact result
4. assumptions
5. blockers/follow-ups
6. no unrelated changes confirmation

## Supervisor completion loop
1. Verify scope.
2. Inspect diff, especially financial/provider error paths.
3. Resolve architectural proposals explicitly.
4. Run targeted tests.
5. Run full gates if the task impacts shared contracts.
6. Update backlog evidence/status.
7. Choose the next dependency-unblocked tasks.
