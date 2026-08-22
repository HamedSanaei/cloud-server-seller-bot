# Sub-agent task contract template

Copy this for every delegated task. The supervisor can auto-generate this
contract using:

```bash
uv run python scripts/assign_task.py <TASK_ID> --agent <agent-name>
```

This script reads `docs/roadmap/TASKS.yaml` and produces a contract with
dependencies, acceptance criteria, and handoff fields pre-filled.

```text
TASK: <ID> — <title>
GOAL: <one concrete outcome>

CONTEXT:
- Relevant architecture invariant(s): <...>
- Existing public contract to preserve: <...>

ALLOWED WRITE SCOPE:
- path/a/**
- tests/path/a/**

READ-ONLY CONTEXT:
- path/b/interface.py
- docs/architecture/ARCHITECTURE.md

FORBIDDEN:
- no schema migrations
- no dependency changes
- no public interface changes outside allowed scope
- no unrelated formatting/refactors

ACCEPTANCE CRITERIA:
1. ...
2. ...
3. ...

TESTS TO RUN:
- uv run pytest tests/...
- uv run ruff check <scope>

HANDOFF:
- changed files
- test commands/results
- assumptions
- blockers/proposals
```
