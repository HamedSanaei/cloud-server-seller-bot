"""Fail if generated CI/test reports are tracked by Git.

Denylist (generated artifacts, never source):
    pytest-results.xml
    junit.xml
    coverage.xml
    .coverage / .coverage.*
    .ci-artifacts/
    test-results/

Inspects tracked paths via ``git ls-files`` and fails with a clear message
when a generated artifact is committed. JUnit/coverage XML files are build
artifacts: they must live under the git-ignored ``.ci-artifacts/`` directory
locally (and as CI upload artifacts), never in source control.

Usage:
    uv run python scripts/check_generated_artifacts.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

#: Exact tracked file names that are always generated reports.
DENIED_FILES = frozenset(
    {
        "pytest-results.xml",
        "junit.xml",
        "coverage.xml",
        ".coverage",
    }
)

#: Tracked path prefixes that are always generated-report directories.
DENIED_PREFIXES = (
    ".ci-artifacts/",
    "test-results/",
    "htmlcov/",
)

#: Suffix rule for coverage sharding artifacts (``.coverage.<host>.<pid>``).
COVERAGE_SHARD_PREFIX = ".coverage."


def _tracked_files() -> list[str]:
    """Every path Git currently tracks (relative, POSIX-style)."""
    try:
        proc = subprocess.run(
            ["git", "ls-files"],
            capture_output=True,
            text=True,
            check=False,
            cwd=Path(__file__).resolve().parent.parent,
        )
    except FileNotFoundError:
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def find_violations(tracked: list[str]) -> list[str]:
    """Tracked paths that are generated CI artifacts."""
    violations: list[str] = []
    for path in tracked:
        name = path.rsplit("/", 1)[-1]
        if name in DENIED_FILES or name.startswith(COVERAGE_SHARD_PREFIX):
            violations.append(path)
            continue
        for prefix in DENIED_PREFIXES:
            if path == prefix.rstrip("/") or path.startswith(prefix):
                violations.append(path)
                break
    return sorted(violations)


def main() -> int:
    violations = find_violations(_tracked_files())
    if violations:
        print(f"GENERATED-ARTIFACT GATE FAILED: {len(violations)} violation(s)")
        for path in violations:
            print(f"  tracked generated artifact: {path}")
        print("Remove it from source control (e.g. `git rm <path>`) and keep")
        print("generated reports under the git-ignored .ci-artifacts/ directory.")
        return 1
    print("generated-artifact gate passed: no tracked CI reports")
    return 0


if __name__ == "__main__":
    sys.exit(main())
