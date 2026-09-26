"""Tests for the fast staging deployment lane (deploy-staging.yml).

Acceptance: every push to `staging` is validated cheaply, built into an
immutable image and deployed to the CURRENT server within minutes, reusing the
existing GitHub/server configuration (same environment secrets, same compose
project, same Telegram bot) - never a second bot, database, redis or deploy
path. The release lane stays stronger, never duplicated.

These are static/functional tests only: no test here performs a real deploy,
contacts GHCR, or opens an SSH session.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
STAGING_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy-staging.yml"
PRODUCTION_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy-production.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy-production.sh"
VERIFY_CI = REPO_ROOT / "scripts" / "verify_ci.py"
MAKEFILE = REPO_ROOT / "Makefile"
PROD_COMPOSE = REPO_ROOT / "deploy" / "production" / "docker-compose.yml"

SHARED_HOST_GROUP = "telegram-shared-host-deploy"


def _workflow() -> dict:
    return yaml.safe_load(STAGING_WORKFLOW.read_text(encoding="utf-8"))


def _on(doc: dict) -> dict:
    # PyYAML parses the `on:` key as boolean True (YAML 1.1).
    return doc[True]


def _text() -> str:
    return STAGING_WORKFLOW.read_text(encoding="utf-8")


def _run_commands(doc: dict | None = None) -> list[str]:
    """Every `run:` body in the workflow (YAML comments are stripped)."""
    workflow = doc if doc is not None else _workflow()
    commands: list[str] = []
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            run = step.get("run")
            if run:
                commands.append(run)
    return commands


class TestStagingTriggersAndConcurrency:
    def test_workflow_file_is_valid_yaml(self) -> None:
        assert _workflow()["name"] == "deploy-staging"

    def test_every_push_to_staging_deploys(self) -> None:
        on = _on(_workflow())
        assert on["push"]["branches"] == ["staging"]
        assert "workflow_dispatch" in on
        # Staging never deploys another branch, and never on PRs.
        assert "pull_request" not in on
        assert "workflow_run" not in on

    def test_superseded_staging_runs_are_cancelled(self) -> None:
        """Three quick pushes must produce one deployment (the newest)."""
        concurrency = _workflow()["concurrency"]
        assert concurrency["group"] == "staging-deploy"
        assert concurrency["cancel-in-progress"] is True

    def test_manual_dispatch_is_pinned_to_the_staging_branch(self) -> None:
        text = _text()
        assert 'refs/heads/staging" ]' in text
        assert "deploy-staging only runs on refs/heads/staging" in text

    def test_staging_jobs_are_ordered_checks_then_build_then_deploy(self) -> None:
        jobs = _workflow()["jobs"]
        assert list(jobs) == ["fast-checks", "build", "deploy"]
        assert jobs["build"]["needs"] == "fast-checks"
        assert jobs["deploy"]["needs"] == "build"

    def test_deploy_job_serializes_against_the_release_lane(self) -> None:
        """Same host, same deploy path: the lanes must never overlap."""
        for workflow, path in (
            (_workflow(), STAGING_WORKFLOW),
            (
                yaml.safe_load(PRODUCTION_WORKFLOW.read_text(encoding="utf-8")),
                PRODUCTION_WORKFLOW,
            ),
        ):
            shared = workflow["jobs"]["deploy"]["concurrency"]
            assert shared["group"] == SHARED_HOST_GROUP, path
            assert shared["cancel-in-progress"] is False, path


class TestStagingGateStaysFast:
    """The staging lane is deliberately lighter - and must stay that way."""

    def test_fast_check_job_installs_locked_deps_and_runs_the_lane_gate(self) -> None:
        steps = _workflow()["jobs"]["fast-checks"]["steps"]
        commands = [step["run"] for step in steps if "run" in step]
        assert "uv sync --frozen --all-groups" in commands
        assert "uv run python scripts/verify_ci.py --staging" in commands

    def test_no_repository_wide_tests_coverage_mypy_or_pre_commit(self) -> None:
        # Inspect the executed commands, not the prose: the workflow explains
        # what it deliberately omits, and those comments must stay.
        executed = "\n".join(_run_commands())
        assert executed
        for forbidden in ("pytest", "--cov", "--cov-fail-under", "pre-commit", "mypy"):
            assert forbidden not in executed, f"staging must not run {forbidden!r}"

    def test_staging_gate_is_the_canonical_script(self) -> None:
        """One definition of the lane's gates, shared with `make staging-check`."""
        text = VERIFY_CI.read_text(encoding="utf-8")
        assert "STAGING_STEPS" in text
        assert '["uv", "run", "ruff", "check", "."]' in text
        assert '["uv", "run", "ruff", "format", "--check", "."]' in text
        assert '"compileall"' in text
        smoke = "cloud_platform.api.app, cloud_platform.bot.main, cloud_platform.worker.settings"
        assert smoke in text
        # The lane gate never grows the release gates into the fast path.
        start = text.index("STAGING_STEPS: list[list[str]] = [")
        end = text.index("PUSH_READY_STEPS: list[list[str]] = [")
        assert 0 < start < end
        staging_block = text[start:end]
        assert "compileall" in staging_block
        for forbidden in ("pytest", "--cov", "mypy", "pre-commit"):
            assert forbidden not in staging_block, forbidden


class TestMakefileStagingCheck:
    def test_target_runs_the_lane_gate_without_the_full_suite(self) -> None:
        text = MAKEFILE.read_text(encoding="utf-8")
        assert "staging-check:" in text
        target = text[text.index("staging-check:") :]
        target = target[: target.index("\n\n")] if "\n\n" in target else target
        assert "uv run python scripts/verify_ci.py --staging" in target
        for forbidden in ("pytest", "--cov", "pre-commit", "mypy"):
            assert forbidden not in target, forbidden
        assert "staging-check" in text.splitlines()[0]


class TestStagingImage:
    def test_build_uses_the_push_sha_and_the_repository_dockerfile(self) -> None:
        text = _text()
        assert "docker/build-push-action" in text
        assert "file: ./Dockerfile" in text
        assert "platforms: linux/amd64" in text
        assert 'sha="${{ github.sha }}"' in text

    def test_immutable_sha_tag_is_pushed_alongside_a_staging_pointer(self) -> None:
        text = _text()
        assert "${{ steps.resolve.outputs.image }}:${{ steps.resolve.outputs.sha }}" in text
        assert "${{ steps.resolve.outputs.image }}:staging" in text
        # The DEPLOYED reference is the immutable SHA tag, never `staging`.
        deploy_line = [line for line in text.splitlines() if "PLATFORM_IMAGE_NEW=" in line]
        assert len(deploy_line) == 1
        assert "needs.build.outputs.image }}:${{ needs.build.outputs.sha }}" in deploy_line[0]
        assert ":staging'" not in deploy_line[0]

    def test_github_actions_layer_cache_is_enabled(self) -> None:
        text = _text()
        assert "cache-from: type=gha" in text
        assert "cache-to: type=gha,mode=max" in text

    def test_existing_sha_tag_is_reused_not_rebuilt(self) -> None:
        text = _text()
        assert "docker manifest inspect" in text
        assert "reusing it unchanged" in text


class TestStagingReusesExistingInfrastructure:
    def test_uses_the_existing_environment_configuration(self) -> None:
        """No duplicated secrets: the lanes read the same GitHub configuration."""
        deploy = _workflow()["jobs"]["deploy"]
        assert deploy["environment"] == "production"
        text = _text()
        for var in ("PROD_HOST", "PROD_USER", "PROD_PORT", "PROD_DEPLOY_PATH"):
            assert f"vars.{var}" in text, var
        for secret in ("PROD_SSH_KEY", "PROD_KNOWN_HOSTS"):
            assert f"secrets.{secret}" in text, secret

    def test_no_secret_is_copied_into_yaml_or_printed(self) -> None:
        text = _text()
        assert "set -x" not in text
        assert 'echo "${{ secrets.' not in text
        assert "echo ${PROD_SSH_KEY}" not in text
        assert "cat ${HOME}/.ssh/prod_key" not in text
        # The GHCR token travels on stdin only.
        assert "--password-stdin" in text
        assert "printf '%s' \"${GHCR_TOKEN}\" | ssh" in text

    def test_host_key_verification_is_never_disabled(self) -> None:
        text = _text()
        assert "StrictHostKeyChecking=no" not in text
        assert "StrictHostKeyChecking=yes" in text
        assert text.count("StrictHostKeyChecking=yes") >= 3

    def test_ssh_key_is_ephemeral_with_mode_600(self) -> None:
        text = _text()
        assert "chmod 600" in text
        assert 'rm -f "${HOME}/.ssh/prod_key"' in text

    def test_no_second_stack_is_introduced(self) -> None:
        """The isolated M12-004 stack is NOT this lane.

        It is named in a comment (so nobody wires it in later), never executed,
        and no second compose project/image/volume set is created here.
        """
        executed = "\n".join(_run_commands())
        assert "deploy/staging" not in executed
        assert "scripts/deploy.py" not in executed
        assert "compose down" not in executed
        text = _text()
        assert "deploy/staging/docker-compose.yml` (M12-004) is NOT used" in text
        assert "cloud-platform-production" in text  # the existing compose project

    def test_permissions_are_minimal(self) -> None:
        permissions = _workflow()["permissions"]
        assert permissions["contents"] == "read"
        assert permissions["packages"] == "write"


class TestStagingDeploysThroughTheSharedEngine:
    def test_deploys_the_exact_sha_with_the_staging_profile(self) -> None:
        text = _text()
        assert "< scripts/deploy-production.sh" in text
        assert "DEPLOY_PROFILE='staging' bash -s" in text
        assert "EXPECTED_HEAD=" in text
        assert "CANDIDATE_COMPOSE_FILE=" in text
        assert "EXPECTED_COMPOSE_SHA256=" in text

    def test_compose_candidate_travels_from_the_same_commit(self) -> None:
        text = _text()
        assert "sha256sum deploy/production/docker-compose.yml" in text
        assert "candidate=" in text
        assert text.index("Deliver release compose candidate") < text.index(
            "Deploy the exact image over SSH"
        )

    def test_migrations_are_never_skipped(self) -> None:
        """Staging shares the development database: migrations still run."""
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        assert "run --rm --no-deps migrate" in script
        assert "alembic upgrade head" in script
        assert "alembic downgrade" not in script.lower()
        text = _text()
        assert "SKIP_MIGRATIONS" not in text
        assert "SKIP_SCHEMA" not in text

    def test_exactly_one_bot_is_enforced_by_the_shared_engine(self) -> None:
        """One Telegram token => exactly one poller, in either lane."""
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        assert 'bot_count="$(compose_candidate ps -q bot | grep -c . || true)"' in script
        assert '"${bot_count}" = "1"' in script
        compose = yaml.safe_load(PROD_COMPOSE.read_text(encoding="utf-8"))
        assert compose["services"]["bot"]["deploy"]["replicas"] == 1
        # The lane must not start a bot of its own outside that compose project.
        assert "docker run" not in _text()
        assert "up -d" not in _text()

    def test_health_and_storefront_checks_are_delegated_not_skipped(self) -> None:
        text = _text()
        assert "DEPLOY_PROFILE=staging" in text
        assert "storefront readiness" in text
        assert "/health/ready" not in text, "the shared engine owns the health gate"
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        assert "/health/ready" in script
        assert "python -m cloud_platform.cli offers readiness" in script


class TestReleaseLaneIsNotWeakened:
    def test_staging_never_runs_the_release_pipeline(self) -> None:
        """No `ci` dependency and no release gates in the fast lane."""
        text = _text()
        assert "workflow_run" not in text
        for gate in (
            "scripts/check_migrations.py",
            "scripts/check_domain_provider_branching.py",
            "scripts/gen_leaseweb_coverage.py",
            "scripts/validate_tasks.py",
        ):
            assert gate not in text, gate

    def test_main_ci_keeps_its_high_confidence_gates(self) -> None:
        ci = CI_WORKFLOW.read_text(encoding="utf-8")
        assert "uv run mypy src" in ci
        assert "uv run pre-commit run --all-files" in ci
        assert "--cov-fail-under=88" in ci
        assert "tests/live/test_postgres_*.py" in ci
        on = _on(yaml.safe_load(ci))
        assert on["push"]["branches"] == ["main"]
        assert "pull_request" in on

    def test_production_workflow_still_exists_and_is_manual(self) -> None:
        production = yaml.safe_load(PRODUCTION_WORKFLOW.read_text(encoding="utf-8"))
        on = _on(production)
        assert "workflow_dispatch" in on
        assert "workflow_run" not in on
        assert "push" not in on

    @pytest.mark.parametrize("workflow", [STAGING_WORKFLOW, PRODUCTION_WORKFLOW])
    def test_both_lanes_create_no_commits_or_tags(self, workflow: Path) -> None:
        text = workflow.read_text(encoding="utf-8")
        for forbidden in ("git commit", "git tag", "git push", "git reset", "git checkout -b"):
            assert forbidden not in text, forbidden
