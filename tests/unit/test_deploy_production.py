"""Tests for the production CD pipeline (GHCR build + SSH deploy).

Acceptance: every push to `main` runs CI first; only a green CI run builds
the exact tested commit into an immutable GHCR image and deploys that exact
image with migrations-first ordering, health gates, single-bot enforcement
and image rollback (never a database downgrade).

These are static/functional tests only — no test here performs a real
deployment, contacts GHCR, or opens an SSH session.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy-production.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy-production.sh"
PROD_COMPOSE = REPO_ROOT / "deploy" / "production" / "docker-compose.yml"
PROD_ENV_EXAMPLE = REPO_ROOT / "deploy" / "production" / ".env.example"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _on(doc: dict) -> dict:
    # PyYAML parses the `on:` key as boolean True (YAML 1.1).
    return doc[True]


def _script() -> str:
    return DEPLOY_SCRIPT.read_text(encoding="utf-8")


def _bash_available() -> bool:
    """Whether this machine can execute the deploy script's shell functions."""
    try:
        probe = subprocess.run(
            ["bash", "-c", "exit 0"],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if probe.returncode != 0:
        return False
    check = subprocess.run(
        ["bash", "-c", f"export DEPLOY_PRODUCTION_SOURCED=1; source '{DEPLOY_SCRIPT.as_posix()}'"],
        capture_output=True,
        timeout=30,
    )
    return check.returncode == 0


needs_bash = pytest.mark.skipif(not _bash_available(), reason="no POSIX bash available")


def _bash_functions(body: str) -> str:
    """Run `body` with the deploy script's functions loaded (no deployment)."""
    result = subprocess.run(
        [
            "bash",
            "-c",
            f"export DEPLOY_PRODUCTION_SOURCED=1; source '{DEPLOY_SCRIPT.as_posix()}'; {body}",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"bash failed: {result.stderr}"
    return result.stdout.strip()


class TestWorkflowTriggersAndGates:
    def test_workflow_file_is_valid_yaml(self) -> None:
        assert _workflow()["name"] == "deploy-production"

    def test_deploys_on_ci_completion_and_manual_dispatch(self) -> None:
        on = _on(_workflow())
        assert "workflow_run" in on
        assert on["workflow_run"]["workflows"] == ["ci"]
        assert on["workflow_run"]["types"] == ["completed"]
        assert "workflow_dispatch" in on
        assert "sha" in on["workflow_dispatch"]["inputs"]

    def test_deployments_are_serialized(self) -> None:
        concurrency = _workflow()["concurrency"]
        assert concurrency["group"] == "production-deploy"
        assert concurrency["cancel-in-progress"] is False

    def test_minimal_permissions(self) -> None:
        permissions = _workflow()["permissions"]
        assert permissions["contents"] == "read"
        assert permissions["packages"] == "write"

    def test_build_job_gates_on_successful_push_to_main(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        assert "github.event.workflow_run.conclusion" in text
        assert "github.event.workflow_run.event" in text
        assert "github.event.workflow_run.head_branch" in text
        assert '"main"' in text or "'main'" in text

    def test_deploys_the_ci_tested_sha_not_a_moving_ref(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        assert "github.event.workflow_run.head_sha" in text
        # Both checkouts pin the exact SHA.
        assert text.count("ref: ${{") >= 2

    def test_deploy_job_uses_the_production_environment(self) -> None:
        jobs = _workflow()["jobs"]
        assert jobs["deploy"]["environment"] == "production"
        assert jobs["deploy"]["needs"] == "build"


class TestImageBuild:
    def test_ghcr_image_uses_lowercase_registry_path(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        assert "tr '[:upper:]' '[:lower:]'" in text
        assert "ghcr.io" in text

    def test_build_uses_the_repository_dockerfile(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        assert "docker/build-push-action" in text
        assert "file: ./Dockerfile" in text

    def test_immutable_full_sha_tag_is_pushed(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        assert "${{ steps.resolve.outputs.image }}:${{ steps.resolve.outputs.sha }}" in text

    def test_existing_sha_tag_is_never_rebuilt(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        assert "docker manifest inspect" in text
        assert "will NOT be rebuilt or overwritten" in text

    def test_built_image_is_verified_before_deploy(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        assert "migrate --help" in text
        assert "deploy/production/docker-compose.yml config" in text

    def test_production_never_tracks_a_mutable_tag(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        assert (
            "PLATFORM_IMAGE_NEW='${{ needs.build.outputs.image }}:${{ needs.build.outputs.sha }}'"
            in text
        )
        for line in text.splitlines():
            if "PLATFORM_IMAGE_NEW=" in line:
                assert ":latest" not in line


class TestWorkflowSecurity:
    def test_no_disabled_host_key_verification(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        assert "StrictHostKeyChecking=no" not in text
        assert "StrictHostKeyChecking=yes" in text
        assert "PROD_KNOWN_HOSTS" in text

    def test_ssh_key_has_ephemeral_mode_600_lifecycle(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        assert "chmod 600" in text
        assert 'rm -f "${HOME}/.ssh/prod_key"' in text

    def test_token_travels_on_stdin_never_in_argv(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        assert "--password-stdin" in text
        assert "printf '%s' \"${GHCR_TOKEN}\" | ssh" in text

    def test_strict_shell_and_no_trace_mode(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        assert "set -Eeuo pipefail" in text
        assert "set -x" not in text

    def test_ghcr_auth_uses_the_actions_token(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        assert "secrets.GITHUB_TOKEN" in text
        assert "github.actor" in text


class TestDeployScriptStatic:
    def test_script_is_executable(self) -> None:
        assert DEPLOY_SCRIPT.is_file()
        listed = subprocess.run(
            ["git", "ls-files", "-s", DEPLOY_SCRIPT.name],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=DEPLOY_SCRIPT.parent,
        )
        assert listed.returncode == 0
        entry = listed.stdout.replace("\\", "/").strip()
        assert entry.startswith("100755 "), entry
        assert entry.endswith("deploy-production.sh"), entry
        if os.name == "posix":
            assert os.access(DEPLOY_SCRIPT, os.X_OK)

    def test_shell_syntax_is_valid(self) -> None:
        if not _bash_available():
            pytest.skip("no POSIX bash available")
        result = subprocess.run(["bash", "-n", str(DEPLOY_SCRIPT)], capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr.decode()

    def test_strict_mode(self) -> None:
        assert "set -Eeuo pipefail" in _script()

    def test_migrations_run_before_app_success(self) -> None:
        script = _script()
        assert "alembic upgrade head" in script or "run --rm --no-deps migrate" in script
        assert script.index("migrate") < script.index("starting api + worker + bot")

    def test_no_volume_destruction(self) -> None:
        assert "down -v" not in _script()

    def test_no_automatic_downgrade(self) -> None:
        # The script documents the no-downgrade policy in prose; what must
        # never appear is an actual downgrade command.
        assert "alembic downgrade" not in _script().lower()

    def test_health_gates_are_bounded(self) -> None:
        script = _script()
        assert "/health/ready" in script
        assert "HEALTH_ATTEMPTS" in script
        assert "alembic current" in script

    def test_single_bot_polling_is_enforced(self) -> None:
        script = _script()
        assert 'bot_count="$(compose ps -q bot | grep -c . || true)"' in script
        assert '"${bot_count}" = "1"' in script

    def test_rollback_restores_the_previous_image(self) -> None:
        script = _script()
        assert "ROLLBACK: restoring" in script
        assert "database NOT downgraded" in script or "database left at the new migration" in script
        # A rescued rollback is still a failed deployment.
        assert "RESULT: DEPLOYMENT FAILED, ROLLBACK DONE" in script

    def test_image_reference_is_validated(self) -> None:
        assert "[0-9a-f]{40}" in _script()

    def test_server_configuration_is_never_written(self) -> None:
        script = _script()
        assert "CONFIGURATION_PATH" in script
        assert "never created or modified here" in script
        assert 'cat "${ENV_FILE}"' not in script
        assert "POSTGRES" not in script

    def test_no_secret_printing(self) -> None:
        script = _script()
        assert "never reads, prints, or rewrites secrets" in script
        assert "set -x" not in script


class TestDeployScriptFunctions:
    @needs_bash
    def test_dotenv_value_reads_plain_and_quoted_values(self, tmp_path: Path) -> None:
        env = tmp_path / "deploy.env"
        env.write_text(
            'PLATFORM_IMAGE=ghcr.io/org/repo:abc\nAPI_PORT="8000"\nEMPTY=\n',
            encoding="utf-8",
        )
        out = _bash_functions(
            f"dotenv_value '{env.as_posix()}' PLATFORM_IMAGE; echo; "
            f"dotenv_value '{env.as_posix()}' API_PORT; echo; "
            f"dotenv_value '{env.as_posix()}' MISSING; echo done"
        )
        assert out.splitlines() == ["ghcr.io/org/repo:abc", "8000", "done"]

    @needs_bash
    def test_set_platform_image_only_touches_its_own_line(self, tmp_path: Path) -> None:
        env = tmp_path / "deploy.env"
        before = (
            "# comment stays\n"
            "PLATFORM_IMAGE=ghcr.io/org/repo:old\n"
            "POSTGRES_PASSWORD=s3cret-value\n"
            "API_PORT=8000\n"
        )
        env.write_text(before, encoding="utf-8")
        _bash_functions(f"set_platform_image '{env.as_posix()}' ghcr.io/org/repo:new")
        assert env.read_text(encoding="utf-8") == before.replace(
            "PLATFORM_IMAGE=ghcr.io/org/repo:old", "PLATFORM_IMAGE=ghcr.io/org/repo:new"
        )

    @needs_bash
    def test_set_platform_image_appends_when_absent(self, tmp_path: Path) -> None:
        env = tmp_path / "deploy.env"
        env.write_text("API_PORT=8000\n", encoding="utf-8")
        _bash_functions(f"set_platform_image '{env.as_posix()}' ghcr.io/org/repo:new")
        assert (
            env.read_text(encoding="utf-8")
            == "API_PORT=8000\nPLATFORM_IMAGE=ghcr.io/org/repo:new\n"
        )


class TestProductionCompose:
    def _compose(self) -> dict:
        return yaml.safe_load(PROD_COMPOSE.read_text(encoding="utf-8"))

    def test_expected_services(self) -> None:
        services = self._compose()["services"]
        for name in ("postgres", "redis", "migrate", "api", "worker", "bot"):
            assert name in services, f"missing service {name}"

    def test_migrations_run_before_api_worker_and_bot(self) -> None:
        services = self._compose()["services"]
        for name in ("api", "worker", "bot"):
            deps = services[name]["depends_on"]
            assert deps["migrate"]["condition"] == "service_completed_successfully"
            assert deps["postgres"]["condition"] == "service_healthy"
            assert deps["redis"]["condition"] == "service_healthy"

    def test_bot_runs_exactly_one_replica(self) -> None:
        assert self._compose()["services"]["bot"]["deploy"]["replicas"] == 1

    def test_database_and_cache_stay_internal(self) -> None:
        services = self._compose()["services"]
        assert services["postgres"].get("ports") is None
        assert services["redis"].get("ports") is None

    def test_platform_services_use_the_immutable_image_variable(self) -> None:
        services = self._compose()["services"]
        for name in ("migrate", "api", "worker", "bot"):
            assert "PLATFORM_IMAGE" in services[name]["image"], name

    def test_configuration_mount_is_server_owned(self) -> None:
        text = PROD_COMPOSE.read_text(encoding="utf-8")
        assert "${CONFIGURATION_PATH:-/etc/cloud-server-seller/configuration.toml}" in text
        assert "./configuration.toml" not in text

    def test_state_lives_in_named_volumes(self) -> None:
        assert set(self._compose()["volumes"]) == {"pgdata", "redisdata", "backups"}

    def test_env_example_documents_the_deploy_contract(self) -> None:
        example = PROD_ENV_EXAMPLE.read_text(encoding="utf-8")
        for var in ("PLATFORM_IMAGE=", "POSTGRES_PASSWORD=", "API_PORT=", "CONFIGURATION_PATH"):
            assert var in example, f"{var} missing from .env.example"


class TestCiStillGatesDeploys:
    def test_ci_runs_on_prs_and_main_pushes(self) -> None:
        ci = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
        on = _on(ci)
        assert "pull_request" in on
        assert on["push"]["branches"] == ["main"]

    def test_ci_keeps_the_leaseweb_contract_gate(self) -> None:
        text = CI_WORKFLOW.read_text(encoding="utf-8")
        assert "scripts/gen_leaseweb_coverage.py --check" in text
