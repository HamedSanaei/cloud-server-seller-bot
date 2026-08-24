"""Tests for the rolling/blue-green deploy driver (M12-006).

Acceptance: API/bot/worker deploy without double consumers.

The worker (arq) and the bot (Telegram long-polling) are EXCLUSIVE
consumers: two live generations would double-consume. The tests prove the
deploy plans drain an old generation completely - verified against actual
``compose ps`` output at execution time - before any new generation
starts, in both strategies, and that a plan violating the invariant is
refused before anything runs.
"""

from __future__ import annotations

import pytest

from scripts.deploy import (
    Deployer,
    DeployError,
    DoubleConsumerError,
    Runner,
    Step,
    assert_no_double_consumers,
    build_plan,
)

IMAGE = "registry.example.com/cloud-server-platform:abc123"


class FakeRunner:
    """Records executed commands; canned outputs per command tail."""

    def __init__(self, fail_on: str | None = None, ps_output: str = "") -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail_on = fail_on  # substring to make fail (exit 1)
        self.ps_output = ps_output

    def run(self, argv) -> tuple[int, str]:
        argv = tuple(argv)
        self.calls.append(argv)
        if "--status" in argv and "running" in argv:
            return 0, self.ps_output
        if self.fail_on and self.fail_on in " ".join(argv):
            return 1, f"error in {' '.join(argv)}"
        return 0, ""


@pytest.fixture()
def runner() -> FakeRunner:
    return FakeRunner()


# ---------------------------------------------------------------------------
# Plan shape
# ---------------------------------------------------------------------------


class TestRollingPlan:
    def test_migrations_run_first_and_once(self, runner: FakeRunner) -> None:
        plan = build_plan("rolling", image=IMAGE)
        kinds = [s.kind for s in plan]
        assert kinds[0] == "preflight"
        assert kinds.count("migrate") == 1
        migrate_index = kinds.index("migrate")
        # every start comes after migrations
        first_start = kinds.index("start")
        assert migrate_index < first_start

    def test_exclusive_services_drain_before_start(self) -> None:
        plan = build_plan("rolling", image=IMAGE)
        for service in ("worker", "bot"):
            steps = [s for s in plan if s.service == service]
            stop_index = next(
                i for i, s in enumerate(steps) if s.kind == "drain" and "stop" in s.argv
            )
            verify_index = next(
                i for i, s in enumerate(steps) if "ps" in s.argv and "--status" in s.argv
            )
            start_index = next(i for i, s in enumerate(steps) if s.kind == "start")
            assert stop_index < verify_index < start_index

    def test_api_rolls_health_gated(self) -> None:
        plan = build_plan("rolling", image=IMAGE)
        api_steps = [s for s in plan if s.service == "api"]
        assert len(api_steps) == 1
        assert "--wait" in api_steps[0].argv

    def test_unknown_service_rejected(self) -> None:
        with pytest.raises(ValueError, match="not an exclusive consumer"):
            build_plan("rolling", image=IMAGE, exclusive_services=("api",))

    def test_unknown_strategy_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown strategy"):
            build_plan("canary", image=IMAGE)


class TestBlueGreenPlan:
    def test_green_starts_only_after_all_blue_drained(self) -> None:
        """Across projects: no green EXCLUSIVE generation starts while any
        blue one may still consume. (The green api may roll in early - it is
        replaceable, not an exclusive consumer.)"""
        plan = build_plan("blue-green", image=IMAGE)
        # drain verifications of the BLUE project (no -p flag) ...
        blue_verifies = [i for i, s in enumerate(plan) if s.kind == "drain" and "ps" in s.argv]
        # ... vs green starts OF EXCLUSIVE SERVICES (argv carries -green)
        green_exclusive_starts = [
            i
            for i, s in enumerate(plan)
            if s.kind == "start"
            and s.service in ("worker", "bot")
            and any(a.endswith("-green") for a in s.argv)
        ]
        assert blue_verifies and green_exclusive_starts
        assert max(blue_verifies) < min(green_exclusive_starts)

    def test_invariant_holds_on_both_strategies(self) -> None:
        for strategy in ("rolling", "blue-green"):
            assert_no_double_consumers(build_plan(strategy, image=IMAGE))

    def test_teardown_of_blue_present(self) -> None:
        plan = build_plan("blue-green", image=IMAGE)
        assert any(s.kind == "teardown" for s in plan)


# ---------------------------------------------------------------------------
# The invariant refuses unsafe plans
# ---------------------------------------------------------------------------


def _bad_plan() -> list[Step]:
    return [
        Step("start worker", "worker", "start", ("docker", "compose", "up", "-d", "worker")),
        Step("drain worker", "worker", "drain", ("docker", "compose", "stop", "worker")),
    ]


class TestInvariant:
    def test_bad_plan_raises(self) -> None:
        with pytest.raises(DoubleConsumerError, match="double consumers"):
            assert_no_double_consumers(_bad_plan())

    def test_deployer_refuses_to_execute_a_bad_plan(self) -> None:
        runner = FakeRunner()
        deployer = Deployer(runner, _bad_plan())
        with pytest.raises(DoubleConsumerError):
            deployer.execute()
        assert runner.calls == []  # nothing ran

    def test_plan_without_worker_is_trivially_safe(self) -> None:
        assert_no_double_consumers(
            [s for s in build_plan("rolling", image=IMAGE) if s.service != "worker"]
        )


# ---------------------------------------------------------------------------
# Execution semantics
# ---------------------------------------------------------------------------


class TestExecution:
    async def test_rolling_execution_order(self) -> None:
        runner = FakeRunner()
        deployer = Deployer(runner, build_plan("rolling", image=IMAGE))
        deployer.execute()
        joined = [" ".join(c) for c in runner.calls]
        # preflight -> migrate -> api -> per-service drain/verify/start
        assert "config --quiet" in joined[0]
        assert "--wait migrate" in joined[1]
        assert "up -d --no-deps --wait api" in joined[2]
        worker_stop = next(
            i for i, c in enumerate(joined) if c.endswith("stop --timeout 30 worker")
        )
        worker_verify = next(i for i, c in enumerate(joined) if "ps --status running worker" in c)
        worker_up = next(i for i, c in enumerate(joined) if c.endswith("--wait worker"))
        assert worker_stop < worker_verify < worker_up

    def test_still_running_container_blocks_the_deploy(self) -> None:
        """The drain verification reads REAL ps output: a still-running old
        generation aborts the deploy instead of double-consuming."""
        runner = FakeRunner(ps_output="worker  Running")  # old gen NOT gone
        deployer = Deployer(runner, build_plan("rolling", image=IMAGE))
        with pytest.raises(DeployError, match="second generation"):
            deployer.execute()
        # it stopped right after the failed verification - no worker up ran
        started = [" ".join(c) for c in runner.calls if "up" in c and "worker" in c]
        assert started == []

    def test_failed_step_names_itself(self) -> None:
        runner = FakeRunner(fail_on="--wait migrate")
        deployer = Deployer(runner, build_plan("rolling", image=IMAGE))
        with pytest.raises(DeployError, match="apply migrations"):
            deployer.execute()

    def test_dry_run_executes_nothing_but_validates(self) -> None:
        runner = FakeRunner()
        rendered = Deployer(runner, build_plan("blue-green", image=IMAGE)).dry_run()
        assert runner.calls == []
        assert "docker compose" in rendered
        assert "green" in rendered


# ---------------------------------------------------------------------------
# CLI wiring (dry-run only; no docker needed)
# ---------------------------------------------------------------------------


class TestMain:
    def test_dry_run_main_returns_zero(
        self, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scripts import deploy as deploy_mod

        monkeypatch.chdir = lambda p: None  # no-op safety
        code = deploy_mod.main(["--image", IMAGE, "--strategy", "rolling", "--dry-run"])
        assert code == 0
        out = capsys.readouterr().out
        assert "migrate" in out

    def test_runner_protocol_is_satisfied(self) -> None:
        assert isinstance(Runner(), Runner)
