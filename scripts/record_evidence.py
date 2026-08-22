#!/usr/bin/env python3
"""
Task evidence recorder.

Updates docs/roadmap/TASKS.yaml with completion evidence and status after a
task is finished. This enforces the conventional evidence format documented
in docs/agents/SUPERVISOR_PLAYBOOK.md.

Usage:
    uv run python scripts/record_evidence.py <task-id> --status done --evidence "<evidence text>"

Multiple --evidence flags can be used to append multiple evidence entries.
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


class IndentDumper(yaml.SafeDumper):
    """Custom YAML dumper that preserves list indentation under parent keys."""


def _list_representer(dumper: yaml.Dumper, data: list) -> yaml.Node:
    """Force list items to be indented under their parent key."""
    return dumper.represent_sequence(
        "tag:yaml.org,2002:seq",
        data,
        flow_style=False,
    )


IndentDumper.add_representer(list, _list_representer)


def load_tasks() -> tuple[dict, list[dict]]:
    """Load tasks from TASKS.yaml, returning the full data and tasks list."""
    with open(TASKS_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data, data["tasks"]


def save_tasks(data: dict) -> None:
    """Save tasks back to TASKS.yaml with consistent indentation.

    Uses indent=2 with the custom dumper to produce list items indented
    under their parent keys (matching the original format).
    """
    output = yaml.dump(
        data,
        Dumper=IndentDumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=120,
        indent=2,
    )
    # yaml.dump with default_flow_style=False already indents list items
    # under their parent keys. But we need to ensure empty lists render as []
    # Let the default behavior handle this.
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        f.write(output)


def find_task(tasks: list[dict], task_id: str) -> dict | None:
    """Find a task by its ID."""
    for task in tasks:
        if task["id"] == task_id:
            return task
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Record evidence for a completed task in TASKS.yaml"
    )
    parser.add_argument("task_id", help="Task ID (e.g., M00-003)")
    parser.add_argument(
        "--status",
        choices=["done", "blocked", "ready", "in_progress"],
        default="done",
        help="Status to set for the task (default: done)",
    )
    parser.add_argument(
        "--evidence",
        action="append",
        default=[],
        help="Evidence string. Can be specified multiple times.",
    )
    args = parser.parse_args()

    data, tasks = load_tasks()
    task = find_task(tasks, args.task_id)

    if task is None:
        print(f"ERROR: Task {args.task_id} not found in TASKS.yaml", file=sys.stderr)
        sys.exit(1)

    print(f"Task: {args.task_id} — {task.get('title', '(no title)')}")
    print(f"Current status: {task.get('status', 'unknown')}")
    print(f"New status: {args.status}")

    # Set status
    task["status"] = args.status

    # Timestamp prefix for evidence
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d")

    # Add evidence with timestamp
    existing = task.get("evidence", [])
    if existing is None:
        existing = []
    for ev in args.evidence:
        if ev.startswith("202") or ev.startswith("20"):
            # Already timestamped
            existing.append(ev)
        else:
            existing.append(f"{timestamp}: {ev}")
    task["evidence"] = existing

    # Update task_count
    data["task_count"] = len(tasks)

    save_tasks(data)

    print(f"Status updated to: {task['status']}")
    print(f"Evidence entries: {len(task['evidence'])}")
    for i, ev in enumerate(task["evidence"], 1):
        print(f"  [{i}] {ev}")


if __name__ == "__main__":
    main()
