"""One canonical push-ready CI verification entrypoint (orchestration only).

Runs the same repository-owned gates GitHub Actions runs, in deterministic
order. No business logic lives here: each step shells out to the underlying
command, so the individual commands stay authoritative.

Usage:
    uv run python scripts/verify_ci.py --push-ready   # full gates + coverage
    uv run python scripts/verify_ci.py --static       # fast gates only

Steps (--push-ready):
    1. ruff check
    2. ruff format check
    3. mypy
    4. pre-commit (must exit 0 AND modify zero files)
    5. full pytest + coverage into .ci-artifacts/
    6. migration gate
    7. provider-branching gate
    8. Leaseweb generated-contract gate
    9. task validation
    10. generated-artifact gate
    11. git status / diff audit (reports only; never commits)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

STATIC_STEPS: list[list[str]] = [
    ["uv", "run", "ruff", "check", "."],
    ["uv", "run", "ruff", "format", "--check", "."],
    ["uv", "run", "mypy", "src"],
]

PUSH_READY_STEPS: list[list[str]] = [
    ["uv", "run", "ruff", "check", "."],
    ["uv", "run", "ruff", "format", "--check", "."],
    ["uv", "run", "mypy", "src"],
    ["uv", "run", "pre-commit", "run", "--all-files"],
    [
        "uv",
        "run",
        "pytest",
        "--junitxml=.ci-artifacts/pytest-results.xml",
        "--cov=src",
        "--cov-report=xml:.ci-artifacts/coverage.xml",
        "--cov-report=term",
        "--cov-fail-under=88",
    ],
    ["uv", "run", "python", "scripts/check_migrations.py"],
    ["uv", "run", "python", "scripts/check_domain_provider_branching.py"],
    ["uv", "run", "python", "scripts/gen_leaseweb_coverage.py", "--check"],
    ["uv", "run", "python", "scripts/validate_tasks.py"],
    ["uv", "run", "python", "scripts/check_generated_artifacts.py"],
]


def _run(cmd: list[str]) -> int:
    print(f"+ {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=ROOT)
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--push-ready", action="store_true", help="full gates incl. coverage")
    group.add_argument("--static", action="store_true", help="fast gates only")
    args = parser.parse_args(argv)

    steps = PUSH_READY_STEPS if args.push_ready else STATIC_STEPS
    if args.push_ready:
        (ROOT / ".ci-artifacts").mkdir(parents=True, exist_ok=True)
    for cmd in steps:
        code = _run(cmd)
        if code != 0:
            print(f"verify_ci FAILED at: {' '.join(cmd)} (exit {code})")
            return code
    # Final audit: report, never commit. Validation itself may generate
    # files; they must be ignored artifacts, not staged changes.
    for audit in (["git", "diff", "--check"], ["git", "status", "--short"]):
        _run(audit)
    print("verify_ci passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
