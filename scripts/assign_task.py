#!/usr/bin/env python3
"""
Sub-agent task assignment helper.

Generates a task contract for a sub-agent by reading the task definition
from docs/roadmap/TASKS.yaml and producing the contract in the conventional
format documented in docs/agents/TASK_CONTRACT.md.

Usage:
    uv run python scripts/assign_task.py <task-id> [--output <file>]

If --output is omitted, the contract is printed to stdout.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is not installed. Run: uv add --dev pyyaml", file=sys.stderr)
    sys.exit(1)


REPO_ROOT = Path(__file__).resolve().parent.parent
TASKS_FILE = REPO_ROOT / "docs" / "roadmap" / "TASKS.yaml"
CONTRACT_TEMPLATE = REPO_ROOT / "docs" / "agents" / "TASK_CONTRACT.md"


def load_tasks() -> list[dict]:
    """Load and return all tasks from TASKS.yaml."""
    with open(TASKS_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data["tasks"]


def find_task(tasks: list[dict], task_id: str) -> dict | None:
    """Find a task by its ID."""
    for task in tasks:
        if task["id"] == task_id:
            return task
    return None


def format_dependencies(tasks: list[dict], task: dict) -> str:
    """Format dependency information for a task."""
    deps = task.get("depends_on", [])
    if not deps:
        return "No dependencies."

    lines = []
    for dep_id in deps:
        dep = find_task(tasks, dep_id)
        if dep:
            lines.append(f"- **{dep_id}**: {dep['title']} (status: {dep.get('status', 'unknown')})")
        else:
            lines.append(f"- **{dep_id}**: (not found)")
    return "\n".join(lines)


def generate_contract(tasks: list[dict], task_id: str, agent_name: str | None = None) -> str:
    """Generate a task contract string."""
    task = find_task(tasks, task_id)
    if task is None:
        print(f"ERROR: Task {task_id} not found.", file=sys.stderr)
        sys.exit(1)

    milestone = task.get("milestone", "unknown")
    title = task.get("title", "(no title)")
    priority = task.get("priority", "P?")
    owner = task.get("owner_hint", "unassigned")
    acceptance = task.get("acceptance", [])
    evidence = task.get("evidence", [])
    status = task.get("status", "unknown")

    agent_label = agent_name or owner or "sub-agent"

    timestamp = datetime.now(UTC).strftime("%Y-%m-%d")

    contract = f"""```text
TASK: {task_id} — {title}
GOAL: <Codex supervisor assigns to {agent_label}. Complete this task according to
      acceptance criteria and record evidence in the backlog. This agent MUST NOT
      modify files outside the allowed scope or change architecture without
      supervisor approval.

MILESTONE: {milestone}
PRIORITY: {priority}
OWNER_HINT: {owner}
STATUS: {status}
ASSIGNED_AT: {timestamp}

CONTEXT:
- Relevant architecture invariant(s): see AGENTS.md Architecture invariants.
  Domain modules cannot import provider adapters, Telegram framework, FastAPI
  or database ORM models. External mutations must be idempotent. Wallet ledger
  is append-only. Financial calculations never use float.
- Existing public contract to preserve: task contract template in
  docs/agents/TASK_CONTRACT.md

DEPENDENCIES (must all be 'done' before starting):
{format_dependencies(tasks, task)}

ALLOWED WRITE SCOPE:
- <sub-agent determines concrete paths based on task — supervisor confirms before edit>

FORBIDDEN:
- no schema migrations without supervisor approval
- no dependency changes without supervisor approval
- no public interface changes outside allowed scope
- no unrelated formatting/refactors
- no provider/framework/ORM imports into domain modules

ACCEPTANCE CRITERIA:
"""

    for i, crit in enumerate(acceptance, 1):
        contract += f"{i}. {crit}\n"

    contract += """
EXISTING EVIDENCE:
"""
    if evidence:
        for ev in evidence:
            contract += f"- {ev}\n"
    else:
        contract += "- (none)\n"

    contract += """
TESTS TO RUN:
- uv run ruff check .
- uv run ruff format --check .
- uv run mypy src
- uv run pytest tests/ (or relevant subset)
- uv run python scripts/validate_tasks.py

HANDOFF:
- changed files (list)
- behavior implemented (summary)
- tests run + exact result
- assumptions (if any)
- blockers/proposals (if any)
- confirmation: no unrelated changes
```
"""
    return contract


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a sub-agent task contract from TASKS.yaml"
    )
    parser.add_argument("task_id", help="Task ID (e.g., M00-003)")
    parser.add_argument(
        "--agent",
        default=None,
        help="Agent name (e.g., H1, H2, supervisor). Defaults to owner_hint.",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Output file path. If omitted, prints to stdout.",
    )
    args = parser.parse_args()

    tasks = load_tasks()
    contract = generate_contract(tasks, args.task_id, args.agent)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(contract, encoding="utf-8")
        print(f"Contract written to {args.output}", file=sys.stderr)
    else:
        print(contract)


if __name__ == "__main__":
    main()
