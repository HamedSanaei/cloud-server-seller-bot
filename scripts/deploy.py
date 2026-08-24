"""Rolling / blue-green deploy driver (M12-006).

Acceptance: API/bot/worker deploy without double consumers.

The platform has two kinds of runtime consumers:

- **Exclusive consumers** - the arq ``worker`` and the Telegram ``bot``
  (long polling). Two generations running at once would double-consume
  queue jobs / poll getUpdates concurrently. For these the ONLY safe
  order is DRAIN-THEN-START: the old generation is stopped and fully gone
  before the new generation starts. This holds in BOTH strategies.
- **Replaceable consumers** - the stateless ``api``. It may roll behind
  health checks, or go blue-green (a sibling project serves while the old
  one drains).

This driver makes that order DATA, not convention: :func:`build_plan`
emits an explicit step list, :func:`assert_no_double_consumers` proves the
exclusive-consumer invariant on any plan (and refuses to run otherwise),
and :class:`Deployer` executes steps through an injectable command runner,
so the whole thing is testable without Docker.

Usage::

    uv run python scripts/deploy.py --image registry/repo:<sha> \
        --strategy rolling --dry-run     # print + validate the plan
    uv run python scripts/deploy.py --image registry/repo:<sha> \
        --strategy rolling               # execute against docker compose

Blue-green runs the new stack under a sibling compose project
(``<project>-green``): migrate once (shared), start the green api, gate on
health, drain ALL blue exclusive consumers, start the green ones, cut the
api over, tear the blue stack down. The exclusive services never overlap
across colors - that is exactly what the invariant enforces.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass


class DeployError(Exception):
    """A deploy step failed; message names the step."""


class DoubleConsumerError(Exception):
    """A plan would run two generations of an exclusive consumer."""


#: Services that must NEVER have two live generations (queue jobs /
#: Telegram long-polling would be consumed twice).
EXCLUSIVE_SERVICES: tuple[str, ...] = ("worker", "bot")


@dataclass(frozen=True)
class Step:
    """One concrete command of a deploy plan."""

    name: str
    service: str  # "" for whole-stack steps
    kind: str  # preflight | migrate | start | drain | smoke | teardown
    argv: tuple[str, ...]


def _compose(project: str | None, *args: str) -> tuple[str, ...]:
    argv = ["docker", "compose"]
    if project:
        argv += ["-p", project]
    return (*argv, *args)


def build_plan(
    strategy: str,
    *,
    image: str,
    compose_file: str = "deploy/staging/docker-compose.yml",
    project: str | None = None,
    exclusive_services: Sequence[str] = EXCLUSIVE_SERVICES,
) -> list[Step]:
    """The ordered deploy steps for ``strategy``.

    ``image`` is exported into the environment by the caller (the compose
    file reads PLATFORM_IMAGE); every ``up`` here recreates with it.
    """
    if strategy not in {"rolling", "blue-green"}:
        raise ValueError(f"unknown strategy: {strategy!r}")
    services: list[str] = []
    for service in exclusive_services:
        if service not in EXCLUSIVE_SERVICES:
            raise ValueError(
                f"{service!r} is not an exclusive consumer (known: {', '.join(EXCLUSIVE_SERVICES)})"
            )
        services.append(service)

    env_arg = ("--env-file", ".env")
    steps: list[Step] = [
        Step(
            "validate compose file",
            "",
            "preflight",
            _compose(project, "-f", compose_file, *env_arg, "config", "--quiet"),
        ),
        # migrations ALWAYS first and only once: both colors share one DB.
        Step(
            "apply migrations",
            "",
            "migrate",
            _compose(project, "-f", compose_file, *env_arg, "up", "-d", "--wait", "migrate"),
        ),
    ]

    if strategy == "rolling":
        # Health-gated recreation of the stateless api first...
        steps.append(
            Step(
                "roll api (health-gated)",
                "api",
                "start",
                _compose(
                    project, "-f", compose_file, *env_arg, "up", "-d", "--no-deps", "--wait", "api"
                ),
            )
        )
        # ...then each exclusive consumer: FULLY drained before started.
        for service in services:
            steps.append(
                Step(
                    f"drain {service} (SIGTERM, wait exit)",
                    service,
                    "drain",
                    _compose(
                        project, "-f", compose_file, *env_arg, "stop", "--timeout", "30", service
                    ),
                )
            )
            steps.append(
                Step(
                    f"wait {service} fully stopped",
                    service,
                    "drain",
                    _compose(
                        project, "-f", compose_file, *env_arg, "ps", "--status", "running", service
                    ),
                )
            )
            steps.append(
                Step(
                    f"start {service} (new image)",
                    service,
                    "start",
                    _compose(
                        project,
                        "-f",
                        compose_file,
                        *env_arg,
                        "up",
                        "-d",
                        "--no-deps",
                        "--wait",
                        service,
                    ),
                )
            )
        return steps

    # ---- blue-green -----------------------------------------------------
    # Green is a SIBLING PROJECT sharing the network/volumes/DB.
    green = f"{project or 'cloud-platform-staging'}-green"
    steps.append(
        Step(
            "start green api",
            "api",
            "start",
            _compose(green, "-f", compose_file, *env_arg, "up", "-d", "--wait", "api"),
        )
    )
    # Exclusive consumers: drain EVERY blue generation before ANY green
    # generation starts - across projects, not just within one.
    for service in services:
        steps.append(
            Step(
                f"drain blue {service}",
                service,
                "drain",
                _compose(project, "-f", compose_file, *env_arg, "stop", "--timeout", "30", service),
            )
        )
        steps.append(
            Step(
                f"wait blue {service} fully stopped",
                service,
                "drain",
                _compose(
                    project, "-f", compose_file, *env_arg, "ps", "--status", "running", service
                ),
            )
        )
    # Cutover: green takes the traffic (published port moves with the
    # project's api service), then the blue api is retired.
    steps.append(
        Step(
            "cut traffic over to green",
            "api",
            "cutover",
            _compose(project, "-f", compose_file, *env_arg, "stop", "--timeout", "30", "api"),
        )
    )
    for service in services:
        steps.append(
            Step(
                f"start green {service}",
                service,
                "start",
                _compose(green, "-f", compose_file, *env_arg, "up", "-d", "--wait", service),
            )
        )
    steps.append(
        Step(
            "retire blue api container",
            "api",
            "teardown",
            _compose(project, "-f", compose_file, *env_arg, "rm", "--force", "-v", "api"),
        )
    )
    return steps


def assert_no_double_consumers(plan: Sequence[Step]) -> None:
    """Prove the exclusive-consumer invariant on a plan.

    For every exclusive service there must exist a completed drain step
    (``stop`` AND a verified-empty ``ps``) BEFORE its first start step -
    including across blue/green projects. A plan that starts a second
    generation while the first may still consume raises.
    """
    for service in EXCLUSIVE_SERVICES:
        relevant = [s for s in plan if s.service == service]
        if not relevant:
            continue
        drained = False
        for step in relevant:
            if _is_drain_verification(step):
                # the verification half of the drain: empty output means gone
                drained = True
                continue
            if step.kind == "start" and not drained:
                raise DoubleConsumerError(
                    f"plan starts {service!r} before it is fully drained (double consumers)"
                )


class Runner:
    """Executes one command; returns (exit code, combined output)."""

    def run(self, argv: Sequence[str]) -> tuple[int, str]:  # pragma: no cover - trivial
        completed = subprocess.run(argv, check=False, capture_output=True, text=True)
        sys.stdout.write(completed.stdout)
        sys.stderr.write(completed.stderr)
        return completed.returncode, completed.stdout + completed.stderr


def _is_drain_verification(step: Step) -> bool:
    return (
        step.kind == "drain"
        and "ps" in step.argv
        and "--status" in step.argv
        and "running" in step.argv
    )


class Deployer:
    def __init__(self, runner: Runner, plan: Sequence[Step]) -> None:
        self._runner = runner
        self.plan = list(plan)

    def execute(self) -> None:
        assert_no_double_consumers(self.plan)
        for step in self.plan:
            code, output = self._runner.run(step.argv)
            if _is_drain_verification(step):
                # `compose ps --status running <svc>` lists any STILL-RUNNING
                # generation. A non-empty listing means the old generation is
                # not gone yet - starting the new one now would double-consume.
                if output.strip():
                    raise DeployError(
                        f"step {step.name!r}: {step.service} still has running "
                        "containers - refusing to start a second generation"
                    )
                continue
            if code != 0:
                raise DeployError(f"step {step.name!r} failed (exit {code})")

    def dry_run(self) -> str:
        """Validate the plan WITHOUT executing; returns the rendered plan."""
        assert_no_double_consumers(self.plan)
        lines = [f"$ {' '.join(step.argv)}  # {step.kind}: {step.name}" for step in self.plan]
        return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="PLATFORM_IMAGE ref (registry/repo:<sha>)")
    parser.add_argument("--strategy", choices=("rolling", "blue-green"), default="rolling")
    parser.add_argument("--compose-file", default="deploy/staging/docker-compose.yml")
    parser.add_argument("--project-name", default=None, help="compose project name")
    parser.add_argument("--dry-run", action="store_true", help="print + validate only")
    args = parser.parse_args(argv)

    import os

    os.environ.setdefault("PLATFORM_IMAGE", args.image)
    plan = build_plan(
        args.strategy,
        image=args.image,
        compose_file=args.compose_file,
        project=args.project_name,
    )
    deployer = Deployer(Runner(), plan)
    try:
        if args.dry_run:
            print(deployer.dry_run())
            return 0
        deployer.execute()
    except (DoubleConsumerError, DeployError) as exc:
        print(f"DEPLOY FAILED: {exc}", file=sys.stderr)
        return 1
    print("deploy complete: migrations applied, api rolled, exclusive consumers drained")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
