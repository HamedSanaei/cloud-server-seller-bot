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
DOCKERFILE = REPO_ROOT / "Dockerfile"
ENTRYPOINT = REPO_ROOT / "docker-entrypoint.sh"
GITATTRIBUTES = REPO_ROOT / ".gitattributes"


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


_OLD_SHA = "a" * 40
_NEW_SHA = "b" * 40
_OLD_IMAGE = f"ghcr.io/test/repo:{_OLD_SHA}"
_NEW_IMAGE = f"ghcr.io/test/repo:{_NEW_SHA}"

_STUB_DOCKER = """#!/bin/sh
# Fake docker for fail-closed tests: records every invocation, never mutates.
echo "$@" >> "$DOCKER_CALLS_LOG"
case " $* " in
    *" pull "*)
        [ "$STUB_FAIL_PULL" = "1" ] && exit 1
        ;;
esac
exit 0
"""


def _fail_closed_harness(tmp_path: Path, *, missing: str, fail_pull: bool = False) -> str:
    """Run deploy() with a stub docker and return its stdout plus RC marker.

    `missing` is one of "configuration", "compose", "env", or "nothing".
    The invocation mirrors production (`if deploy; then ... else ... fi`),
    i.e. the errexit-suppressed context where the original bug continued.
    """
    assert missing in ("configuration", "compose", "env", "nothing")
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    if missing != "env":
        (deploy_dir / "deploy.env").write_text(
            f"PLATFORM_IMAGE={_OLD_IMAGE}\nAPI_PORT=8000\n", encoding="utf-8"
        )
    if missing != "compose":
        (deploy_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    config = tmp_path / "configuration.toml"
    if missing != "configuration":
        config.write_text("[app]\n", encoding="utf-8")
    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir()
    (stub_bin / "docker").write_text(_STUB_DOCKER, encoding="utf-8")
    (stub_bin / "docker").chmod(0o755)
    calls = tmp_path / "docker-calls.log"
    script = (
        f'export DEPLOY_PRODUCTION_SOURCED=1 PATH="{stub_bin.as_posix()}:$PATH" '
        f"DOCKER_CALLS_LOG='{calls.as_posix()}' STUB_FAIL_PULL={'1' if fail_pull else '0'} "
        f"DEPLOY_PATH='{deploy_dir.as_posix()}' PLATFORM_IMAGE_NEW='{_NEW_IMAGE}' "
        f"COMPOSE_FILE='{(deploy_dir / 'docker-compose.yml').as_posix()}' "
        f"ENV_FILE='{(deploy_dir / 'deploy.env').as_posix()}' "
        f"CONFIGURATION_PATH='{config.as_posix()}' EXPECTED_HEAD='' "
        f"HEALTH_ATTEMPTS=3 HEALTH_INTERVAL=1; "
        f"source '{DEPLOY_SCRIPT.as_posix()}'; "
        "if deploy; then echo HARNESS_RC=0; else echo HARNESS_RC=$?; fi"
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, f"harness itself failed: {result.stderr}"
    return result.stdout


def _docker_calls(tmp_path: Path) -> str:
    calls = tmp_path / "docker-calls.log"
    return calls.read_text(encoding="utf-8") if calls.exists() else ""


class TestDeployFailClosed:
    """Preflight failures must stop deploy() before anything is mutated.

    Regression cover for the production incident where a missing
    configuration.toml printed FAIL yet deploy continued into pull,
    postgres/redis startup, and migration.
    """

    @needs_bash
    def test_missing_configuration_stops_before_any_mutation(self, tmp_path: Path) -> None:
        out = _fail_closed_harness(tmp_path, missing="configuration")
        assert "HARNESS_RC=0" not in out
        calls = _docker_calls(tmp_path)
        assert "pull" not in calls
        assert " up " not in f" {calls} "
        assert " run " not in f" {calls} "
        env = tmp_path / "deploy" / "deploy.env"
        assert f"PLATFORM_IMAGE={_OLD_IMAGE}" in env.read_text(encoding="utf-8")
        assert _NEW_SHA not in env.read_text(encoding="utf-8")

    @needs_bash
    def test_missing_compose_file_stops_before_any_mutation(self, tmp_path: Path) -> None:
        out = _fail_closed_harness(tmp_path, missing="compose")
        assert "HARNESS_RC=0" not in out
        calls = _docker_calls(tmp_path)
        assert "pull" not in calls
        assert " up " not in f" {calls} "
        assert " run " not in f" {calls} "
        env = tmp_path / "deploy" / "deploy.env"
        assert f"PLATFORM_IMAGE={_OLD_IMAGE}" in env.read_text(encoding="utf-8")

    @needs_bash
    def test_missing_env_file_stops_before_any_mutation(self, tmp_path: Path) -> None:
        out = _fail_closed_harness(tmp_path, missing="env")
        assert "HARNESS_RC=0" not in out
        calls = _docker_calls(tmp_path)
        assert "pull" not in calls
        assert " up " not in f" {calls} "
        assert " run " not in f" {calls} "
        assert not (tmp_path / "deploy" / "deploy.env").exists()

    @needs_bash
    def test_pull_failure_restores_reference_without_starting_services(
        self, tmp_path: Path
    ) -> None:
        out = _fail_closed_harness(tmp_path, missing="nothing", fail_pull=True)
        assert "HARNESS_RC=0" not in out
        calls = _docker_calls(tmp_path)
        assert "pull" in calls
        assert " up " not in f" {calls} "
        assert " run " not in f" {calls} "
        env = tmp_path / "deploy" / "deploy.env"
        assert f"PLATFORM_IMAGE={_OLD_IMAGE}" in env.read_text(encoding="utf-8")


_SERVICE_UP_STUB_DOCKER = """#!/bin/sh
# Fake docker for the service-start failure test: healthy infra, a migrate
# that succeeds, and an `up -d api worker bot` that fails ONCE with a
# port-bind error (then succeeds, so the rollback restart works).
echo "$@" >> "$DOCKER_CALLS_LOG"
case " $* " in
    *" inspect "*)
        case "$*" in
            *"Health"*) echo healthy ;;
            *) echo running ;;
        esac
        exit 0
        ;;
    *" ps -q "*)
        echo "cid123"
        exit 0
        ;;
    *" up -d api worker bot "*)
        n="$(cat "$UP_FAIL_COUNTER" 2>/dev/null || echo 0)"
        n=$((n + 1))
        printf '%s' "$n" > "$UP_FAIL_COUNTER"
        if [ "$n" = "1" ]; then
            echo "failed to set up container networking: failed to bind host"
                "port 0.0.0.0:8000/tcp: address already in use" >&2
            exit 1
        fi
        exit 0
        ;;
esac
exit 0
"""

_STUB_SLEEP = """#!/bin/sh
# Records waits instead of performing them: a fail-fast path must not wait.
echo "$@" >> "$SLEEP_CALLS_LOG"
exit 0
"""

_STUB_PYTHON = """#!/bin/sh
# Records readiness probes instead of performing them.
echo "$@" >> "$PYTHON_CALLS_LOG"
exit 1
"""


def _service_start_failure_harness(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run main() with stubbed docker/sleep/python3; the first app `up` fails.

    Returns the completed harness process (stdout, stderr, returncode).
    """
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    (deploy_dir / "deploy.env").write_text(
        f"PLATFORM_IMAGE={_OLD_IMAGE}\nAPI_PORT=8000\n", encoding="utf-8"
    )
    (deploy_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    config = tmp_path / "configuration.toml"
    config.write_text("[app]\n", encoding="utf-8")
    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir()
    (stub_bin / "docker").write_text(_SERVICE_UP_STUB_DOCKER, encoding="utf-8")
    (stub_bin / "docker").chmod(0o755)
    (stub_bin / "sleep").write_text(_STUB_SLEEP, encoding="utf-8")
    (stub_bin / "sleep").chmod(0o755)
    (stub_bin / "python3").write_text(_STUB_PYTHON, encoding="utf-8")
    (stub_bin / "python3").chmod(0o755)
    script = (
        f'export DEPLOY_PRODUCTION_SOURCED=1 PATH="{stub_bin.as_posix()}:$PATH" '
        f"DOCKER_CALLS_LOG='{(tmp_path / 'docker-calls.log').as_posix()}' "
        f"SLEEP_CALLS_LOG='{(tmp_path / 'sleep-calls.log').as_posix()}' "
        f"PYTHON_CALLS_LOG='{(tmp_path / 'python-calls.log').as_posix()}' "
        f"UP_FAIL_COUNTER='{(tmp_path / 'up-counter').as_posix()}' "
        f"DEPLOY_PATH='{deploy_dir.as_posix()}' PLATFORM_IMAGE_NEW='{_NEW_IMAGE}' "
        f"COMPOSE_FILE='{(deploy_dir / 'docker-compose.yml').as_posix()}' "
        f"ENV_FILE='{(deploy_dir / 'deploy.env').as_posix()}' "
        f"CONFIGURATION_PATH='{config.as_posix()}' EXPECTED_HEAD='' "
        f"HEALTH_ATTEMPTS=36 HEALTH_INTERVAL=5; "
        f"source '{DEPLOY_SCRIPT.as_posix()}'; "
        # Sourcing the script leaks its `set -e` into this shell; disable it
        # explicitly so MAIN_RC is always reported instead of aborting early.
        "set +e; main; echo MAIN_RC=$?"
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)


def _stub_calls(tmp_path: Path, name: str) -> str:
    calls = tmp_path / name
    return calls.read_text(encoding="utf-8") if calls.exists() else ""


class TestDeployServiceStartFailure:
    """A failed `compose up -d api worker bot` must stop deploy immediately.

    Regression cover for the production incident where a port-bind failure
    (address already in use on 8000) fell through into the full API
    readiness wait instead of failing fast into rollback.
    """

    @needs_bash
    def test_app_start_failure_never_reaches_readiness(self, tmp_path: Path) -> None:
        result = _service_start_failure_harness(tmp_path)
        assert result.returncode == 0, f"harness itself failed: {result.stderr}"
        assert "MAIN_RC=1" in result.stdout
        # The deploy never continued past the failed start.
        assert "waiting for API readiness" not in result.stdout
        # Neither the readiness probe nor any wait ran.
        assert _stub_calls(tmp_path, "python-calls.log") == ""
        assert _stub_calls(tmp_path, "sleep-calls.log") == ""

    @needs_bash
    def test_app_start_failure_selects_rollback_exactly_once(self, tmp_path: Path) -> None:
        result = _service_start_failure_harness(tmp_path)
        assert result.returncode == 0, f"harness itself failed: {result.stderr}"
        calls = _stub_calls(tmp_path, "docker-calls.log")
        # One failed start attempt plus exactly one rollback restart.
        assert calls.count("up -d api worker bot") == 2
        assert result.stdout.count("attempting application-image rollback") == 1
        assert result.stdout.count("ROLLBACK DONE") == 1
        env = tmp_path / "deploy" / "deploy.env"
        assert f"PLATFORM_IMAGE={_OLD_IMAGE}" in env.read_text(encoding="utf-8")

    @needs_bash
    def test_port_bind_error_is_reported_on_stderr(self, tmp_path: Path) -> None:
        result = _service_start_failure_harness(tmp_path)
        assert result.returncode == 0, f"harness itself failed: {result.stderr}"
        assert "address already in use" in result.stderr


_STABLE_STUB_DOCKER = """#!/bin/sh
# Fake docker for the stabilization tests: healthy infra, a succeeding
# migrate, and inspect answers driven by files the test prepares.
echo "$@" >> "$DOCKER_CALLS_LOG"
case " $* " in
    *" inspect "*)
        case "$*" in
            *"Health"*) echo healthy ;;
            *"RestartCount"*)
                n="$(cat "$INSPECT_COUNTER" 2>/dev/null || echo 0)"
                n=$((n + 1))
                printf '%s' "$n" > "$INSPECT_COUNTER"
                if [ "$CRASH_LOOP" = "1" ] && [ "$n" -ge 2 ]; then echo 2; else echo 0; fi
                ;;
            *) echo running ;;
        esac
        exit 0
        ;;
    *" ps -q "*)
        echo "cid123"
        exit 0
        ;;
    *" exec "*) echo "0034 (head)" ;;
esac
exit 0
"""

_STABLE_STUB_PYTHON = """#!/bin/sh
# Readiness probe stub: the API answers healthy.
echo '{"status": "ok"}'
exit 0
"""


def _stabilization_harness(tmp_path: Path, *, crash_loop: bool) -> subprocess.CompletedProcess[str]:
    """Run main() end to end with stubbed docker/sleep/python3.

    With ``crash_loop`` the bot restart count changes mid-deploy; otherwise
    every service stays stable and the deploy succeeds.
    """
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    (deploy_dir / "deploy.env").write_text(
        f"PLATFORM_IMAGE={_OLD_IMAGE}\nAPI_PORT=8000\n", encoding="utf-8"
    )
    (deploy_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    config = tmp_path / "configuration.toml"
    config.write_text("[app]\n", encoding="utf-8")
    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir()
    (stub_bin / "docker").write_text(_STABLE_STUB_DOCKER, encoding="utf-8")
    (stub_bin / "docker").chmod(0o755)
    (stub_bin / "sleep").write_text(_STUB_SLEEP, encoding="utf-8")
    (stub_bin / "sleep").chmod(0o755)
    (stub_bin / "python3").write_text(_STABLE_STUB_PYTHON, encoding="utf-8")
    (stub_bin / "python3").chmod(0o755)
    script = (
        f'export DEPLOY_PRODUCTION_SOURCED=1 PATH="{stub_bin.as_posix()}:$PATH" '
        f"DOCKER_CALLS_LOG='{(tmp_path / 'docker-calls.log').as_posix()}' "
        f"SLEEP_CALLS_LOG='{(tmp_path / 'sleep-calls.log').as_posix()}' "
        f"INSPECT_COUNTER='{(tmp_path / 'inspect-counter').as_posix()}' "
        f"CRASH_LOOP='{'1' if crash_loop else ''}' "
        f"DEPLOY_PATH='{deploy_dir.as_posix()}' PLATFORM_IMAGE_NEW='{_NEW_IMAGE}' "
        f"COMPOSE_FILE='{(deploy_dir / 'docker-compose.yml').as_posix()}' "
        f"ENV_FILE='{(deploy_dir / 'deploy.env').as_posix()}' "
        f"CONFIGURATION_PATH='{config.as_posix()}' EXPECTED_HEAD='0034' "
        f"STABILIZE_SECONDS=1 HEALTH_ATTEMPTS=3 HEALTH_INTERVAL=1; "
        f"source '{DEPLOY_SCRIPT.as_posix()}'; "
        "set +e; main; echo MAIN_RC=$?"
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)


class TestDeployStabilization:
    """Crash-looping worker/bot must fail the deploy despite looking running."""

    @needs_bash
    def test_stable_services_succeed(self, tmp_path: Path) -> None:
        result = _stabilization_harness(tmp_path, crash_loop=False)
        assert result.returncode == 0, f"harness itself failed: {result.stderr}"
        assert "MAIN_RC=0" in result.stdout
        assert "API readiness: ok" in result.stdout
        assert "stable (same containers" in result.stdout
        assert "DEPLOYMENT SUCCEEDED" in result.stdout
        env = tmp_path / "deploy" / "deploy.env"
        assert f"PLATFORM_IMAGE={_NEW_IMAGE}" in env.read_text(encoding="utf-8")

    @needs_bash
    def test_bot_crash_loop_fails_the_deploy(self, tmp_path: Path) -> None:
        result = _stabilization_harness(tmp_path, crash_loop=True)
        assert result.returncode == 0, f"harness itself failed: {result.stderr}"
        assert "MAIN_RC=1" in result.stdout
        # fail() reports to stderr while the rollback summary goes to stdout.
        assert "bot restarted during stabilization" in result.stderr
        assert "DEPLOYMENT SUCCEEDED" not in result.stdout
        assert result.stdout.count("ROLLBACK DONE") == 1
        env = tmp_path / "deploy" / "deploy.env"
        assert f"PLATFORM_IMAGE={_OLD_IMAGE}" in env.read_text(encoding="utf-8")


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


class TestProductionHealthchecks:
    """Only the API serves HTTP, so only the API keeps the image healthcheck.

    Regression cover for worker/bot showing unhealthy (the image
    HEALTHCHECK probes :8000) while actually processing jobs/polling.
    """

    def _compose(self) -> dict:
        return yaml.safe_load(PROD_COMPOSE.read_text(encoding="utf-8"))

    def test_api_inherits_the_image_http_healthcheck(self) -> None:
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        assert "/health/live" in dockerfile
        assert "HEALTHCHECK" in dockerfile
        api = self._compose()["services"]["api"]
        assert "healthcheck" not in api, "api must keep the image HTTP healthcheck"

    def test_non_http_services_disable_the_inherited_healthcheck(self) -> None:
        services = self._compose()["services"]
        for name in ("worker", "bot", "migrate", "backup"):
            healthcheck = services[name].get("healthcheck")
            assert healthcheck is not None and healthcheck.get("disable") is True, (
                f"{name} must not inherit the API HTTP healthcheck"
            )


class TestCiStillGatesDeploys:
    def test_ci_runs_on_prs_and_main_pushes(self) -> None:
        ci = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
        on = _on(ci)
        assert "pull_request" in on
        assert on["push"]["branches"] == ["main"]

    def test_ci_keeps_the_leaseweb_contract_gate(self) -> None:
        text = CI_WORKFLOW.read_text(encoding="utf-8")
        assert "scripts/gen_leaseweb_coverage.py --check" in text


def _docker_available() -> bool:
    """Whether this machine can build and run the production image."""
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


needs_docker = pytest.mark.skipif(not _docker_available(), reason="docker is not available")


def _git_index_mode(path: Path) -> str:
    result = subprocess.run(
        ["git", "ls-files", "-s", path.name],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=path.parent,
    )
    assert result.returncode == 0
    return result.stdout.strip().split()[0]


class TestEntrypointContract:
    """The production image must exec through its real ENTRYPOINT.

    Regression cover for the GHCR verify failure (`permission denied`,
    exit 126): `docker build` succeeding never proved the entrypoint was
    executable, so the contract is pinned at three levels — git mode,
    Dockerfile COPY mode, and a real container run.
    """

    def test_entrypoint_is_tracked_executable(self) -> None:
        assert ENTRYPOINT.is_file()
        assert _git_index_mode(ENTRYPOINT) == "100755"

    def test_dockerfile_enforces_executable_copy(self) -> None:
        text = DOCKERFILE.read_text(encoding="utf-8")
        assert (
            "COPY --chmod=755 --chown=app:app docker-entrypoint.sh /app/docker-entrypoint.sh"
            in text
        )

    def test_shell_scripts_are_lf_pinned(self) -> None:
        assert GITATTRIBUTES.is_file(), ".gitattributes must pin shell line endings"
        assert "*.sh text eol=lf" in GITATTRIBUTES.read_text(encoding="utf-8")
        raw = ENTRYPOINT.read_bytes()
        assert b"\r\n" not in raw, "docker-entrypoint.sh must use LF line endings"
        assert not raw.startswith(b"\xef\xbb\xbf"), "docker-entrypoint.sh must not have a BOM"

    @needs_docker
    def test_built_image_executes_through_the_real_entrypoint(self) -> None:
        tag = "cloud-server-platform:entrypoint-regression"
        build = subprocess.run(
            ["docker", "build", "-t", tag, "."],
            capture_output=True,
            text=True,
            timeout=600,
            cwd=REPO_ROOT,
        )
        assert build.returncode == 0, build.stderr[-2000:]
        try:
            # Through ENTRYPOINT ["/app/docker-entrypoint.sh"] -> exec "$@".
            dispatched = subprocess.run(
                ["docker", "run", "--rm", tag, "sh", "-c", "echo entrypoint-ok"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            assert dispatched.returncode == 0, dispatched.stderr[-2000:]
            assert dispatched.stdout.strip() == "entrypoint-ok"
            # The entrypoint file itself must be executable inside the image.
            mode = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--entrypoint",
                    "/bin/sh",
                    tag,
                    "-c",
                    "test -x /app/docker-entrypoint.sh",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            assert mode.returncode == 0, mode.stderr[-2000:]
        finally:
            subprocess.run(["docker", "rmi", tag], capture_output=True, timeout=120)
