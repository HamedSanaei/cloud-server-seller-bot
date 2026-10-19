"""One canonical CI verification entrypoint (orchestration only).

Runs the same repository-owned gates GitHub Actions runs, in deterministic
order, for whichever lane you select (`--staging`, `--static`,
`--push-ready`). No business logic lives here: each step shells out to the
underlying command, so the individual commands stay authoritative.

Usage:
    uv run python scripts/verify_ci.py --push-ready   # full gates + coverage
    uv run python scripts/verify_ci.py --static       # fast gates only
    uv run python scripts/verify_ci.py --staging      # the staging lane gate
    uv run python scripts/verify_ci.py --staging \
        --test tests/unit/test_business_log.py \
        --test tests/unit/test_hourly_state_machine.py   # targeted + staging gate

Steps (--staging): Ruff lint/format, source compilation and API/bot/worker
import smoke. --test runs explicit regressions first. The staging workflow
additionally enforces targeted financial/deployment and real PostgreSQL contracts;
the full release suite, types and coverage remain in --push-ready and main CI.

Steps (--push-ready):
    1. ruff check
    2. ruff format check
    3. mypy
    4. pre-commit (must exit 0 AND modify zero files)
    5. full pytest + coverage into .ci-artifacts/
    6. exact, unrounded source-line coverage gate
    7. migration gate
    8. provider-branching gate
    9. Leaseweb generated-contract gate
    10. task validation
    11. generated-artifact gate
    12. git status / diff audit (reports only; never commits)
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
    ["uv", "run", "python", "scripts/check_coverage.py", ".ci-artifacts/coverage.xml"],
    ["uv", "run", "python", "scripts/check_migrations.py"],
    ["uv", "run", "python", "scripts/check_domain_provider_branching.py"],
    ["uv", "run", "python", "scripts/gen_leaseweb_coverage.py", "--check"],
    ["uv", "run", "python", "scripts/validate_tasks.py"],
    ["uv", "run", "python", "scripts/check_generated_artifacts.py"],
]

STAGING_STEPS = [
    ["uv", "run", "ruff", "check", "."],
    ["uv", "run", "ruff", "format", "--check", "."],
    ["uv", "run", "python", "-m", "compileall", "-q", "src"],
    [
        "uv",
        "run",
        "python",
        "-c",
        "import cloud_platform.api.app, cloud_platform.bot.main, cloud_platform.worker.settings",
    ],
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
    group.add_argument(
        "--staging",
        action="store_true",
        help="fast staging gate: Ruff, compile and API/bot/worker imports",
    )
    parser.add_argument(
        "--test",
        action="append",
        default=[],
        metavar="TARGET",
        help=(
            "explicit test path/test id to run before the staging steps "
            "(repeatable; requires --staging). No full release suite or coverage."
        ),
    )
    args = parser.parse_args(argv)

    targets = [target for target in args.test if str(target).strip()]
    if targets and not args.staging:
        parser.error("--test is only available with --staging (targeted staging validation)")

    if args.push_ready:
        steps = PUSH_READY_STEPS
        (ROOT / ".ci-artifacts").mkdir(parents=True, exist_ok=True)
    elif args.staging:
        steps = STAGING_STEPS
        (ROOT / ".ci-artifacts").mkdir(parents=True, exist_ok=True)
    else:
        steps = STATIC_STEPS
    if targets:
        # Targeted tests first: a broken behavior must fail before the cheap
        # static gate spends time on the rest of the tree.
        targeted = [
            "uv",
            "run",
            "pytest",
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
            *targets,
        ]
        steps = [targeted, *steps]
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
