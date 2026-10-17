"""Tests for the production CD pipeline (GHCR build + SSH deploy).

Acceptance: production requires explicit approval and successful exact-SHA main
CI; staging updates the same production target. Images are immutable. Writers drain
before encrypted backup and migration; health and single-bot gates determine
activation. Rollback never downgrades a database or starts an incompatible epoch.

These are static/functional tests only — no test here performs a real
deployment, contacts GHCR, or opens an SSH session.
"""

from __future__ import annotations

import hashlib
import os
import selectors
import shlex
import shutil
import subprocess
import sys
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
BASH = (
    "C:/Program Files/Git/bin/bash.exe"
    if os.name == "nt" and Path("C:/Program Files/Git/bin/bash.exe").is_file()
    else shutil.which("bash") or "bash"
)


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _on(doc: dict) -> dict:
    # PyYAML parses the `on:` key as boolean True (YAML 1.1).
    return doc[True]


def _bash_available() -> bool:
    """Whether this machine can execute the deploy script's shell functions."""
    try:
        probe = subprocess.run(
            [BASH, "-c", "exit 0"],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if probe.returncode != 0:
        return False
    check = subprocess.run(
        [BASH, "-c", f"export DEPLOY_PRODUCTION_SOURCED=1; source '{DEPLOY_SCRIPT.as_posix()}'"],
        capture_output=True,
        timeout=30,
    )
    return check.returncode == 0


needs_bash = pytest.mark.skipif(not _bash_available(), reason="no POSIX bash available")


@pytest.fixture(autouse=True)
def isolated_shared_host_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep lock semantics without requiring/writing the host's real /var/lock.

    Only this module's test copy changes. Production never receives a path
    override that could bypass its global lock. The test copy shortens only
    the bounded cross-project lock wait so a real contention test fails fast.
    """
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    isolated = tmp_path / "deploy-test.sh"
    isolated.write_text(
        source.replace(
            "/var/lock/servercloud-platform-release.lock",
            (tmp_path / "shared-release.lock").as_posix(),
        ).replace("flock -w 600 8", "flock -w 1 8"),
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.setattr(sys.modules[__name__], "DEPLOY_SCRIPT", isolated)


@pytest.mark.skipif(sys.platform != "linux", reason="real flock requires Linux")
@pytest.mark.parametrize("different_target", [False, True])
@pytest.mark.parametrize("first_exit", [0, 1])
def test_real_flock_excludes_parallel_release_and_unlocks_after_exit(
    tmp_path: Path, different_target: bool, first_exit: int
) -> None:
    assert shutil.which("flock"), "Linux release validation requires real flock"
    first_target = tmp_path / "first"
    first_target.mkdir()
    second_target = tmp_path / "second" if different_target else first_target
    second_target.mkdir(exist_ok=True)
    first_marker, next_marker = tmp_path / "first-entered", tmp_path / "next-entered"

    def command(target: Path, marker: Path, *, hold: bool, status: int) -> str:
        return f"""
            export DEPLOY_PRODUCTION_SOURCED=1
            export DEPLOY_PATH={shlex.quote(target.as_posix())}
            export PLATFORM_IMAGE_NEW=ghcr.io/example/platform:{"a" * 40}
            export CANDIDATE_COMPOSE_FILE={shlex.quote((target / "candidate.yml").as_posix())}
            export EXPECTED_COMPOSE_SHA256={"a" * 64}
            source {shlex.quote(DEPLOY_SCRIPT.as_posix())}
            deploy() {{
                printf entered > {shlex.quote(marker.as_posix())}
                printf 'entered\\n'
                {"IFS= read -r release" if hold else ":"}
                return {status}
            }}
            main
        """

    first = subprocess.Popen(
        [BASH, "-c", command(first_target, first_marker, hold=True, status=first_exit)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert first.stdout is not None
        with selectors.DefaultSelector() as ready:
            ready.register(first.stdout, selectors.EVENT_READ)
            assert ready.select(timeout=15), "first release never acquired its locks"
        assert first.stdout.readline().strip() == "entered"
        blocked = subprocess.run(
            [BASH, "-c", command(second_target, next_marker, hold=False, status=0)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert blocked.returncode != 0, blocked.stdout + blocked.stderr
        assert not next_marker.exists(), "a concurrent release entered its mutation phase"
        first.communicate(input="release\n", timeout=10)
        assert first.returncode == first_exit
        resumed = subprocess.run(
            [BASH, "-c", command(second_target, next_marker, hold=False, status=0)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert resumed.returncode == 0, resumed.stdout + resumed.stderr
        assert next_marker.read_text() == "entered"
    finally:
        if first.poll() is None:
            first.kill()
            first.communicate(timeout=10)


def _bash_functions(body: str) -> str:
    """Run `body` with the deploy script's functions loaded (no deployment)."""
    result = subprocess.run(
        [
            BASH,
            "-c",
            f"export DEPLOY_PRODUCTION_SOURCED=1; source '{DEPLOY_SCRIPT.as_posix()}'; {body}",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"bash failed: {result.stderr}"
    return result.stdout.strip()


def _application_starts(calls: str | list[str], *, canonical: bool | None = None) -> list[str]:
    """Classify executed service starts by release target, not optional CLI flags."""
    starts = []
    for call in calls.splitlines() if isinstance(calls, str) else calls:
        args = shlex.split(call)
        if args[-3:] != ["api", "worker", "bot"] or "up" not in args or "-d" not in args:
            continue
        if canonical is not None:
            compose = Path(args[args.index("-f") + 1])
            if (compose.name == "docker-compose.yml") is not canonical:
                continue
        starts.append(call)
    return starts


class TestDeployScriptStatic:
    def test_shell_syntax_is_valid(self) -> None:
        if not _bash_available():
            pytest.skip("no POSIX bash available")
        result = subprocess.run([BASH, "-n", str(DEPLOY_SCRIPT)], capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr.decode()

    @needs_bash
    def test_rejected_configuration_fails_before_mutation(self, tmp_path: Path) -> None:
        """An enabled gateway with an unusable callback URL must not deploy."""
        result, deploy_dir, canonical, _, env_before, config_before = _release_harness(
            tmp_path, fail_config_check=True
        )
        assert result.returncode == 0, f"harness itself failed: {result.stderr}"
        assert "MAIN_RC=1" in result.stdout
        assert "server configuration rejected" in (result.stdout + result.stderr)
        # ``fail`` reports on stderr; the summary line lands on stdout.
        assert "deployment aborted before anything was mutated" in (result.stdout + result.stderr)
        assert "deployment failed before anything was mutated" in result.stdout
        calls = _stub_calls(tmp_path, "docker-calls.log")
        # The preflight itself pulled the image and ran the Settings loader...
        assert "get_settings" in calls
        # ...and nothing past it ran: no migration, no service replacement.
        migrate_lines = [
            line
            for line in calls.splitlines()
            if "run --rm --no-deps migrate" in line and "get_settings" not in line
        ]
        assert migrate_lines == []
        assert not _application_starts(calls)
        # Nothing was mutated and nothing needed rolling back.
        assert (deploy_dir / "deploy.env").read_bytes() == env_before
        assert canonical.read_bytes() == b"# canonical release contract\nservices: {}\n"
        assert (tmp_path / "configuration.toml").read_bytes() == config_before
        assert "no rollback needed" in result.stdout
        assert "ROLLBACK DONE" not in result.stdout
        assert "release compose promoted" not in result.stdout


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
    *"io.servercloud.platform-contract"*)
        printf '1\n'
        exit 0
        ;;
    *" image inspect "*)
        printf '%s\n' "${PLATFORM_IMAGE_NEW##*:}"
        exit 0
        ;;
    *" pull "*)
        # The configuration preflight pulls the new image first; the deploy's
        # own compose pull is the second. Counting pulls keeps the
        # restore-on-pull-failure path under test.
        n="$(cat "$PULL_COUNTER" 2>/dev/null || echo 0)"
        n=$((n + 1))
        printf '%s' "$n" > "$PULL_COUNTER"
        if [ "$STUB_FAIL_PULL" = "1" ] && [ "$n" -ge 2 ]; then exit 1; fi
        ;;
    *" alembic heads"*)
        printf '%s\n' "${ALEMBIC_IMAGE_HEAD:-0034 (head)}"
        exit 0
        ;;
    *" alembic current"*)
        printf '%s\n' "${ALEMBIC_DB_HEAD:-0034 (head)}"
        exit 0
        ;;
esac
exit 0
"""


def _deploy_path(path: Path) -> str:
    """The deploy script guards ``DEPLOY_PATH`` with a POSIX absolute regex.

    Windows checkouts hand it ``C:/...``; MSYS bash accepts the ``/c/...`` form
    for both the guard and the script's own ``cd``, so the end-to-end harnesses
    stay runnable outside Linux (CI is unaffected: the path already starts
    with ``/`` there).
    """
    raw = path.as_posix()
    if raw.startswith("/"):
        return raw
    drive, _, rest = raw.partition(":/")
    return f"/{drive.lower()}/{rest}"


def _write_compose_files(
    deploy_dir: Path, *, missing_current: bool = False, missing_candidate: bool = False
) -> tuple[str, str, str]:
    """Write canonical + candidate compose files.

    Returns (current path, candidate path, candidate SHA-256). The contents
    differ deliberately so tests can prove which contract was used where.
    """
    current = deploy_dir / "docker-compose.yml"
    candidate = deploy_dir / ".docker-compose.abc123.candidate.yml"
    # newline="\n": these fixtures are byte-compared after the deploy script
    # rewrites them, so Windows text-mode translation must not creep in.
    if not missing_current:
        current.write_text(
            "# canonical release contract\nservices: {}\n", encoding="utf-8", newline="\n"
        )
    if not missing_candidate:
        candidate.write_text(
            "# release candidate contract\nservices: {}\n", encoding="utf-8", newline="\n"
        )
        sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
    else:
        sha = "0" * 64
    return current.as_posix(), candidate.as_posix(), sha


def _fail_closed_harness(tmp_path: Path, *, missing: str, fail_pull: bool = False) -> str:
    """Run deploy() with a stub docker and return its stdout plus RC marker.

    `missing` is one of "configuration", "compose", "env", or "nothing"
    ("compose" means the release candidate is missing; the canonical file
    may legitimately be absent on a first deploy).
    The invocation mirrors production (`if deploy; then ... else ... fi`),
    i.e. the errexit-suppressed context where the original bug continued.
    """
    assert missing in ("configuration", "compose", "env", "nothing")
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    if missing != "env":
        (deploy_dir / "deploy.env").write_text(
            f"PLATFORM_IMAGE={_OLD_IMAGE}\nAPI_PORT=8000\n", encoding="utf-8", newline="\n"
        )
    current, candidate, candidate_sha = _write_compose_files(
        deploy_dir,
        missing_candidate=(missing == "compose"),
    )
    config = tmp_path / "configuration.toml"
    if missing != "configuration":
        config.write_text("[app]\n", encoding="utf-8")
    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir()
    # Unit harness mocks host primitives; Linux CI also tests real flock.
    (stub_bin / "flock").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (stub_bin / "flock").chmod(0o755)
    (stub_bin / "docker").write_text(_STUB_DOCKER, encoding="utf-8")
    (stub_bin / "docker").chmod(0o755)
    calls = tmp_path / "docker-calls.log"
    script = (
        f'export DEPLOY_PRODUCTION_SOURCED=1 PATH="{_deploy_path(stub_bin)}:$PATH" '
        f"DOCKER_CALLS_LOG='{calls.as_posix()}' STUB_FAIL_PULL={'1' if fail_pull else '0'} "
        f"PULL_COUNTER='{(tmp_path / 'pull-counter').as_posix()}' "
        f"DEPLOY_PATH='{_deploy_path(deploy_dir)}' PLATFORM_IMAGE_NEW='{_NEW_IMAGE}' "
        f"CURRENT_COMPOSE_FILE='{current}' "
        f"CANDIDATE_COMPOSE_FILE='{candidate}' "
        f"EXPECTED_COMPOSE_SHA256='{candidate_sha}' "
        f"ENV_FILE='{(deploy_dir / 'deploy.env').as_posix()}' "
        f"CONFIGURATION_PATH='{config.as_posix()}' EXPECTED_HEAD='' "
        f"HEALTH_ATTEMPTS=3 HEALTH_INTERVAL=1; "
        f"source '{DEPLOY_SCRIPT.as_posix()}'; "
        "if deploy; then echo HARNESS_RC=0; else echo HARNESS_RC=$?; fi"
    )
    result = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=120)
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
        # Before a failed pull only READ-ONLY, database-free probes may run:
        # the ephemeral configuration validator and the alembic head probe
        # (which reads the head out of the image's own migrations directory).
        # A MIGRATION must never have run.
        migrate_runs = [
            line
            for line in calls.splitlines()
            if "run --rm --no-deps migrate" in line
            and "get_settings" not in line
            and "alembic heads" not in line
        ]
        assert migrate_runs == []
        assert "alembic upgrade" not in calls
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
            *"io.servercloud.platform-contract"*) echo 1 ;;
            *"org.opencontainers.image.revision"*) echo "${PLATFORM_IMAGE_NEW##*:}" ;;
            *"Health"*) echo healthy ;;
            *) echo running ;;
        esac
        exit 0
        ;;
    *" ps -q "*)
        echo "cid123"
        exit 0
        ;;
    *" alembic heads"*)
        # The head must come from the RELEASE image, never from the image still
        # recorded in deploy.env during the preflight.
        [ "$PLATFORM_IMAGE" = "$PLATFORM_IMAGE_NEW" ] || {
            echo "PROBE_USED_THE_WRONG_IMAGE: $PLATFORM_IMAGE" >&2
            exit 3
        }
        printf '%s\n' "${ALEMBIC_IMAGE_HEAD:-0034 (head)}"
        exit 0
        ;;
    *" alembic current"*)
        printf '%s\n' "${ALEMBIC_DB_HEAD:-0034 (head)}"
        exit 0
        ;;
    *" up -d api worker bot "*|*" up -d --no-deps api worker bot "*)
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
    current, candidate, candidate_sha = _write_compose_files(deploy_dir)
    config = tmp_path / "configuration.toml"
    config.write_text("[app]\n", encoding="utf-8")
    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir()
    # Unit harness mocks host primitives; Linux CI also tests real flock.
    (stub_bin / "flock").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (stub_bin / "flock").chmod(0o755)
    (stub_bin / "docker").write_text(_SERVICE_UP_STUB_DOCKER, encoding="utf-8")
    (stub_bin / "docker").chmod(0o755)
    (stub_bin / "sleep").write_text(_STUB_SLEEP, encoding="utf-8")
    (stub_bin / "sleep").chmod(0o755)
    (stub_bin / "python3").write_text(_STUB_PYTHON, encoding="utf-8")
    (stub_bin / "python3").chmod(0o755)
    script = (
        f'export DEPLOY_PRODUCTION_SOURCED=1 PATH="{_deploy_path(stub_bin)}:$PATH" '
        f"DOCKER_CALLS_LOG='{(tmp_path / 'docker-calls.log').as_posix()}' "
        f"SLEEP_CALLS_LOG='{(tmp_path / 'sleep-calls.log').as_posix()}' "
        f"PYTHON_CALLS_LOG='{(tmp_path / 'python-calls.log').as_posix()}' "
        f"UP_FAIL_COUNTER='{(tmp_path / 'up-counter').as_posix()}' "
        f"DEPLOY_PATH='{_deploy_path(deploy_dir)}' PLATFORM_IMAGE_NEW='{_NEW_IMAGE}' "
        f"CURRENT_COMPOSE_FILE='{current}' "
        f"CANDIDATE_COMPOSE_FILE='{candidate}' "
        f"EXPECTED_COMPOSE_SHA256='{candidate_sha}' "
        f"ENV_FILE='{(deploy_dir / 'deploy.env').as_posix()}' "
        f"CONFIGURATION_PATH='{config.as_posix()}' EXPECTED_HEAD='' "
        f"HEALTH_ATTEMPTS=36 HEALTH_INTERVAL=5; "
        f"source '{DEPLOY_SCRIPT.as_posix()}'; "
        # Sourcing the script leaks its `set -e` into this shell; disable it
        # explicitly so MAIN_RC is always reported instead of aborting early.
        "set +e; main; echo MAIN_RC=$?"
    )
    return subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=120)


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
        up_lines = [
            line
            for line in calls.splitlines()
            if "up -d api worker bot" in line.replace("--no-deps ", "")
        ]
        assert len(up_lines) == 2
        # The failed attempt uses the release candidate; the rollback
        # restarts with the previous canonical contract, never the candidate.
        assert "candidate" in up_lines[0]
        assert "candidate" not in up_lines[1]
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
            *"io.servercloud.platform-contract"*) echo 1 ;;
            *"org.opencontainers.image.revision"*) echo "${PLATFORM_IMAGE_NEW##*:}" ;;
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
    *" alembic heads"*)
        printf '%s\n' "${ALEMBIC_IMAGE_HEAD:-0034 (head)}"
        exit 0
        ;;
    *" alembic current"*)
        printf '%s\n' "${ALEMBIC_DB_HEAD:-0034 (head)}"
        exit 0
        ;;
    *" exec "*) printf '%s\n' "${ALEMBIC_IMAGE_HEAD:-0034 (head)}" ;;
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
        f"PLATFORM_IMAGE={_OLD_IMAGE}\nAPI_PORT=8000\n", encoding="utf-8", newline="\n"
    )
    current, candidate, candidate_sha = _write_compose_files(deploy_dir)
    config = tmp_path / "configuration.toml"
    config.write_text("[app]\n", encoding="utf-8", newline="\n")
    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir()
    # Unit harness mocks host primitives; Linux CI also tests real flock.
    (stub_bin / "flock").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (stub_bin / "flock").chmod(0o755)
    (stub_bin / "docker").write_text(_STABLE_STUB_DOCKER, encoding="utf-8")
    (stub_bin / "docker").chmod(0o755)
    (stub_bin / "sleep").write_text(_STUB_SLEEP, encoding="utf-8")
    (stub_bin / "sleep").chmod(0o755)
    (stub_bin / "python3").write_text(_STABLE_STUB_PYTHON, encoding="utf-8")
    (stub_bin / "python3").chmod(0o755)
    script = (
        f'export DEPLOY_PRODUCTION_SOURCED=1 PATH="{_deploy_path(stub_bin)}:$PATH" '
        f"DOCKER_CALLS_LOG='{(tmp_path / 'docker-calls.log').as_posix()}' "
        f"SLEEP_CALLS_LOG='{(tmp_path / 'sleep-calls.log').as_posix()}' "
        f"INSPECT_COUNTER='{(tmp_path / 'inspect-counter').as_posix()}' "
        f"CRASH_LOOP='{'1' if crash_loop else ''}' "
        f"DEPLOY_PATH='{_deploy_path(deploy_dir)}' PLATFORM_IMAGE_NEW='{_NEW_IMAGE}' "
        f"CURRENT_COMPOSE_FILE='{current}' "
        f"CANDIDATE_COMPOSE_FILE='{candidate}' "
        f"EXPECTED_COMPOSE_SHA256='{candidate_sha}' "
        f"ENV_FILE='{(deploy_dir / 'deploy.env').as_posix()}' "
        f"CONFIGURATION_PATH='{config.as_posix()}' EXPECTED_HEAD='0034' "
        f"STABILIZE_SECONDS=1 HEALTH_ATTEMPTS=3 HEALTH_INTERVAL=1; "
        f"source '{DEPLOY_SCRIPT.as_posix()}'; "
        "set +e; main; echo MAIN_RC=$?"
    )
    return subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=120)


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
        assert "release compose promoted" not in result.stdout
        assert result.stdout.count("ROLLBACK DONE") == 1
        env = tmp_path / "deploy" / "deploy.env"
        assert f"PLATFORM_IMAGE={_OLD_IMAGE}" in env.read_text(encoding="utf-8")


_RELEASE_STUB_DOCKER = """#!/bin/sh
# Fake docker for the release-compose tests: every operation succeeds unless
# a FAIL_* flag (or the crash-loop / fail-up-once markers) says otherwise.
echo "$@" >> "$DOCKER_CALLS_LOG"
case " $* " in
    *".candidate.yml "*)
        [ "$PLATFORM_IMAGE" = "$PLATFORM_IMAGE_NEW" ] || {
            echo "FORWARD_IMAGE_DRIFT" >&2
            exit 3
        }
        ;;
    *" up -d --no-deps api worker bot "*)
        [ "$PLATFORM_IMAGE" = "$ROLLBACK_IMAGE" ] || {
            echo "ROLLBACK_IMAGE_DRIFT" >&2
            exit 3
        }
        ;;
esac
case " $* " in
    *" inspect "*)
        case "$*" in
            *"io.servercloud.platform-contract"*)
                echo "${STUB_PLATFORM_CONTRACT:-1}"
                ;;
            *"org.opencontainers.image.revision"*)
                echo "${STUB_IMAGE_REVISION:-${PLATFORM_IMAGE_NEW##*:}}"
                ;;
            *"Health"*)
                case " $* " in
                    *" cid-${STUB_UNHEALTHY_SERVICE:-none} "*) echo unhealthy ;;
                    *) echo healthy ;;
                esac
                ;;
            *"RestartCount"*)
                [ "${STUB_FAIL_RESTART_PROBE:-0}" = "1" ] && exit 1
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
        case " $* " in
            *" ps -q bot "*)
                n="$(cat "$BOT_PS_COUNTER" 2>/dev/null || echo 0)"
                n=$((n + 1))
                printf '%s' "$n" > "$BOT_PS_COUNTER"
                if [ "${STUB_TWO_BOTS:-0}" = "1" ] || {
                    [ "${STUB_LATE_TWO_BOTS:-0}" = "1" ] && [ "$n" -ge 3 ]
                }; then
                    printf 'cid-bot\ncid-bot-extra\n'
                else
                    echo "cid-bot"
                fi
                ;;
            *)
                for service in "$@"; do :; done
                echo "cid-${service}"
                ;;
        esac
        exit 0
        ;;
    *"label=com.docker.compose.project.working_dir="*)
        [ -z "${STUB_FOREIGN_PROJECT:-}" ] || printf '%s\n' "$STUB_FOREIGN_PROJECT"
        exit 0
        ;;
    *" ps "*)
        [ "${STUB_FAIL_STATUS:-0}" = "1" ] && exit 1
        exit 0
        ;;
    *" config "*)
        [ "$FAIL_CONFIG" = "1" ] && exit 1
        exit 0
        ;;
    *" pull "*)
        case " $* " in
            *" compose "*) [ "${STUB_FAIL_COMPOSE_PULL:-0}" = "1" ] && exit 1 ;;
        esac
        exit 0
        ;;
    *" get_settings"*)
        # Configuration preflight: the release image refusing the server-owned
        # configuration must fail the deploy before anything is mutated.
        [ "$FAIL_CONFIG_CHECK" = "1" ] && exit 1
        exit 0
        ;;
    *"cloud_platform.db.schema_parity"*)
        # Physical schema gate. It must run with the RELEASE image (deploy.env
        # still holds the previous one at this point) and it fails the deploy
        # when the database lacks objects the release queries.
        case "$PLATFORM_IMAGE" in
            "$PLATFORM_IMAGE_NEW") ;;
            "$ROLLBACK_IMAGE")
                [ "$FAIL_ROLLBACK_SCHEMA" = "1" ] && exit 1
                exit 0
                ;;
            *)
                echo "PROBE_USED_THE_WRONG_IMAGE: $PLATFORM_IMAGE" >&2
                exit 3
                ;;
        esac
        if [ "$FAIL_SCHEMA_PARITY" = "1" ]; then
            echo "[FAIL] the release's code queries schema this database does not have:"
            echo "  missing table:   provider_routes"
            exit 1
        fi
        exit 0
        ;;
    *" alembic heads"*)
        # The head SHIPPED IN THE IMAGE (read from the artifact itself), and
        # read from the RELEASE image — deploy.env still holds the previous one
        # during the preflight. (`exec` against the running api container is not
        # image-pinned by the script: that container IS the release candidate.)
        [ "$FAIL_IMAGE_HEAD" = "1" ] && exit 1
        case " $* " in
            *" run "*)
                [ "$PLATFORM_IMAGE" = "$PLATFORM_IMAGE_NEW" ] || {
                    echo "PROBE_USED_THE_WRONG_IMAGE: $PLATFORM_IMAGE" >&2
                    exit 3
                }
                ;;
        esac
        printf '%s\n' "${ALEMBIC_IMAGE_HEAD:-0034 (head)}"
        exit 0
        ;;
    *" alembic current"*)
        # The revision the DATABASE reports, read with the release image.
        [ "$FAIL_DB_HEAD" = "1" ] && exit 1
        case " $* " in
            *" run "*)
                [ "$PLATFORM_IMAGE" = "$PLATFORM_IMAGE_NEW" ] || {
                    echo "PROBE_USED_THE_WRONG_IMAGE: $PLATFORM_IMAGE" >&2
                    exit 3
                }
                ;;
        esac
        if [ ! -f "$MIGRATION_MARKER" ] && [ -n "${ALEMBIC_DB_HEAD_BEFORE+x}" ]; then
            printf '%s\n' "$ALEMBIC_DB_HEAD_BEFORE"
            exit 0
        fi
        printf '%s\n' "${ALEMBIC_DB_HEAD:-0034 (head)}"
        exit 0
        ;;
    *"catalog auto-sync run"*)
        # Bounded provider-fact refresh release transition (one-shot container).
        if [ "$FAIL_CATALOG_REFRESH" = "1" ]; then
            echo "running one complete catalog refresh (timeout 600s)"
            echo "  leaseweb: ok=False discovered=0 persisted=0 prices=0 published=0 errors=1"
            echo "catalog refresh completed with provider errors: leaseweb"
            exit 1
        fi
        echo "running one complete catalog refresh (timeout 600s)"
        echo "  leaseweb: ok=True discovered=507 persisted=507 prices=456 published=456"
        echo "catalog refresh completed"
        exit 0
        ;;
    *"normalize-selling-currency"*)
        # Catalog canonicalization release transition (one-shot container).
        if [ "$FAIL_NORMALIZE" = "1" ]; then
            echo "  FAIL leaseweb/lsw.c3.2xlarge/ap-northeast-1: pricing failed (OfferPricingError)"
            echo "normalized 0 offer(s) to USD; intentionally skipped: 1; failed: 1"
            exit 1
        fi
        echo "normalized 456 offer(s) to USD; intentionally skipped: 1; failed: 0"
        exit 0
        ;;
    *" run --rm backup"*)
        [ "${FAIL_BACKUP:-0}" = "1" ] && exit 1
        exit 0
        ;;
    *" run "*)
        : > "$MIGRATION_MARKER"
        if [ -n "${STUB_MIGRATION_SIGNAL:-}" ]; then
            kill -"$STUB_MIGRATION_SIGNAL" "$DEPLOY_SHELL_PID"
        fi
        [ "$FAIL_MIGRATE" = "1" ] && exit 1
        exit 0
        ;;
    *" up -d api worker bot "*|*" up -d --no-deps api worker bot "*)
        n="$(cat "$UP_FAIL_COUNTER" 2>/dev/null || echo 0)"
        n=$((n + 1))
        printf '%s' "$n" > "$UP_FAIL_COUNTER"
        if [ "$FAIL_UP_API" = "1" ] && [ "$n" = "1" ]; then
            echo "failed to bind host port 0.0.0.0:8000/tcp: address already in use" >&2
            exit 1
        fi
        exit 0
        ;;
    *"offers readiness"*)
        if [ "$FAIL_STOREFRONT_READINESS" = "1" ]; then
            echo "[FAIL] leaseweb: 507 stored offer(s) but ZERO sellable in USD"
            echo "storefront readiness: FAIL"
            exit 1
        fi
        echo "[OK  ] leaseweb: 12 sellable of 507 stored"
        echo "storefront readiness: OK"
        exit 0
        ;;
    *" exec "*)
        case "$*" in
            *"post_deploy_smoke.py"*)
                if [ "${STUB_TAMPER_CANDIDATE:-0}" = "1" ]; then
                    printf '# changed during deployment\n' >> "$CANDIDATE_COMPOSE_FILE"
                fi
                [ "${FAIL_SMOKE:-0}" != "1" ]
                exit $?
                ;;
        esac
        printf '%s\n' "${ALEMBIC_IMAGE_HEAD:-0034 (head)}"
        exit 0
        ;;
esac
exit 0
"""


def _release_harness(
    tmp_path: Path,
    *,
    missing_candidate: bool = False,
    bad_sha: bool = False,
    fail_config: bool = False,
    fail_config_check: bool = False,
    fail_migrate: bool = False,
    fail_ready: bool = False,
    fail_up_api: bool = False,
    fail_schema_parity: bool = False,
    fail_catalog_refresh: bool = False,
    fail_normalize: bool = False,
    fail_storefront_readiness: bool = False,
    fail_rollback_schema: bool = False,
    crash_loop: bool = False,
    deploy_profile: str = "production",
    alembic_image_head: str = "0034 (head)",
    alembic_db_head: str = "0034 (head)",
    expected_head: str = "0034",
    previous_image: str = _OLD_IMAGE,
    env_overrides: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path, bytes, bytes]:
    """Run main() against the release-candidate flow with stubbed externals.

    ``alembic_image_head`` is the head the release IMAGE ships (read from the
    artifact); ``alembic_db_head`` is what the DATABASE reports. They must
    match before any schema-dependent service starts. ``fail_schema_parity``
    is the production shape where they DO match while the physical schema is
    still missing objects.

    Returns (process, deploy_dir, canonical, candidate, env_before, config_before).
    """
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    env_file = deploy_dir / "deploy.env"
    env_before = (
        "# deployment values (server-owned)\n"
        f"PLATFORM_IMAGE={previous_image}\n"
        "POSTGRES_PASSWORD=s3cret-server-value\n"
        "API_PORT=8000\n"
    )
    env_file.write_text(env_before, encoding="utf-8", newline="\n")
    current, candidate, candidate_sha = _write_compose_files(deploy_dir)
    if missing_candidate:
        (deploy_dir / ".docker-compose.abc123.candidate.yml").unlink()
    config = tmp_path / "configuration.toml"
    config.write_text("[app]\n", encoding="utf-8", newline="\n")
    config_before = config.read_bytes()
    canonical = deploy_dir / "docker-compose.yml"
    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir()
    # Unit harness mocks host primitives; Linux CI also tests real flock.
    (stub_bin / "flock").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (stub_bin / "flock").chmod(0o755)
    (stub_bin / "docker").write_text(_RELEASE_STUB_DOCKER, encoding="utf-8")
    (stub_bin / "docker").chmod(0o755)
    (stub_bin / "sleep").write_text(_STUB_SLEEP, encoding="utf-8")
    (stub_bin / "sleep").chmod(0o755)
    if fail_ready:
        (stub_bin / "python3").write_text(_STUB_PYTHON, encoding="utf-8")
    else:
        (stub_bin / "python3").write_text(_STABLE_STUB_PYTHON, encoding="utf-8")
    (stub_bin / "python3").chmod(0o755)
    script = (
        f'export DEPLOY_PRODUCTION_SOURCED=1 PATH="{_deploy_path(stub_bin)}:$PATH" '
        f"DOCKER_CALLS_LOG='{(tmp_path / 'docker-calls.log').as_posix()}' "
        f"SLEEP_CALLS_LOG='{(tmp_path / 'sleep-calls.log').as_posix()}' "
        f"INSPECT_COUNTER='{(tmp_path / 'inspect-counter').as_posix()}' "
        f"UP_FAIL_COUNTER='{(tmp_path / 'up-counter').as_posix()}' "
        f"MIGRATION_MARKER='{(tmp_path / 'migration-started').as_posix()}' "
        f"BOT_PS_COUNTER='{(tmp_path / 'bot-ps-counter').as_posix()}' "
        f"CRASH_LOOP='{'1' if crash_loop else ''}' "
        f"FAIL_CONFIG='{'1' if fail_config else ''}' "
        f"FAIL_CONFIG_CHECK='{'1' if fail_config_check else ''}' "
        f"FAIL_MIGRATE='{'1' if fail_migrate else ''}' "
        f"FAIL_UP_API='{'1' if fail_up_api else ''}' "
        f"FAIL_SCHEMA_PARITY='{'1' if fail_schema_parity else ''}' "
        f"FAIL_CATALOG_REFRESH='{'1' if fail_catalog_refresh else ''}' "
        f"FAIL_NORMALIZE='{'1' if fail_normalize else ''}' "
        f"FAIL_STOREFRONT_READINESS='{'1' if fail_storefront_readiness else ''}' "
        f"FAIL_ROLLBACK_SCHEMA='{'1' if fail_rollback_schema else ''}' "
        f"ROLLBACK_IMAGE='{_OLD_IMAGE}' "
        f"ALEMBIC_IMAGE_HEAD='{alembic_image_head}' "
        f"ALEMBIC_DB_HEAD='{alembic_db_head}' "
        f"DEPLOY_PATH='{_deploy_path(deploy_dir)}' PLATFORM_IMAGE_NEW='{_NEW_IMAGE}' "
        f"CURRENT_COMPOSE_FILE='{current}' "
        f"CANDIDATE_COMPOSE_FILE='{candidate}' "
        f"EXPECTED_COMPOSE_SHA256='{'f' * 64 if bad_sha else candidate_sha}' "
        f"ENV_FILE='{env_file.as_posix()}' "
        f"CONFIGURATION_PATH='{config.as_posix()}' EXPECTED_HEAD='{expected_head}' "
        f"DEPLOY_PROFILE='{deploy_profile}' "
        f"STABILIZE_SECONDS=1 HEALTH_ATTEMPTS=3 HEALTH_INTERVAL=1; "
        + "".join(
            f"export {key}={shlex.quote(value)}; " for key, value in (env_overrides or {}).items()
        )
        + f"source '{DEPLOY_SCRIPT.as_posix()}'; "
        + "export DEPLOY_SHELL_PID=$$; set +e; main; echo MAIN_RC=$?"
    )
    result = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=120)
    return result, deploy_dir, canonical, Path(candidate), env_before.encode(), config_before


def _assert_no_mutation(
    tmp_path: Path,
    result: subprocess.CompletedProcess[str],
    deploy_dir: Path,
    canonical: Path,
    env_before: bytes,
    config_before: bytes,
) -> None:
    """A preflight failure must change nothing and trigger no fake rollback."""
    assert result.returncode == 0, f"harness itself failed: {result.stderr}"
    assert "MAIN_RC=1" in result.stdout
    calls = _stub_calls(tmp_path, "docker-calls.log")
    assert "pull" not in calls
    assert " up " not in f" {calls} "
    assert " run " not in f" {calls} "
    assert (deploy_dir / "deploy.env").read_bytes() == env_before
    assert canonical.read_bytes() == b"# canonical release contract\nservices: {}\n"
    assert (tmp_path / "configuration.toml").read_bytes() == config_before
    assert "no rollback needed" in result.stdout
    assert "ROLLBACK DONE" not in result.stdout
    assert "release compose promoted" not in result.stdout


class TestReleaseComposeContract:
    """The release compose ships with the release and promotes only on success."""

    @needs_bash
    def test_missing_candidate_fails_before_mutation(self, tmp_path: Path) -> None:
        result, deploy_dir, canonical, _, env_before, config_before = _release_harness(
            tmp_path, missing_candidate=True
        )
        _assert_no_mutation(tmp_path, result, deploy_dir, canonical, env_before, config_before)

    @needs_bash
    def test_invalid_candidate_config_fails_before_mutation(self, tmp_path: Path) -> None:
        result, deploy_dir, canonical, _, env_before, config_before = _release_harness(
            tmp_path, fail_config=True
        )
        _assert_no_mutation(tmp_path, result, deploy_dir, canonical, env_before, config_before)

    @needs_bash
    def test_sha_mismatch_fails_before_mutation(self, tmp_path: Path) -> None:
        result, deploy_dir, canonical, _, env_before, config_before = _release_harness(
            tmp_path, bad_sha=True
        )
        assert "SHA-256 mismatch" in result.stdout or "SHA-256 mismatch" in result.stderr
        _assert_no_mutation(tmp_path, result, deploy_dir, canonical, env_before, config_before)

    @needs_bash
    def test_success_promotes_candidate_to_canonical(self, tmp_path: Path) -> None:
        result, deploy_dir, canonical, candidate, env_before, config_before = _release_harness(
            tmp_path
        )
        assert result.returncode == 0, f"harness itself failed: {result.stderr}"
        assert "MAIN_RC=0" in result.stdout
        assert "release compose promoted:" in result.stdout
        assert "DEPLOYMENT SUCCEEDED" in result.stdout
        # Canonical is now byte-for-byte the release candidate; the
        # candidate no longer remains as an active artifact.
        assert canonical.read_bytes() == b"# release candidate contract\nservices: {}\n"
        assert not candidate.exists()
        # deploy.env stays server-owned: only PLATFORM_IMAGE was rewritten.
        assert (deploy_dir / "deploy.env").read_bytes() == env_before.replace(
            _OLD_IMAGE.encode(), _NEW_IMAGE.encode()
        )
        assert (tmp_path / "configuration.toml").read_bytes() == config_before
        calls = _stub_calls(tmp_path, "docker-calls.log").splitlines()
        stop = next(
            i for i, call in enumerate(calls) if " stop --timeout 45 api worker bot" in call
        )
        migration = next(
            i for i, call in enumerate(calls) if call.endswith(" run --rm --no-deps migrate")
        )
        schema = next(
            i for i, call in enumerate(calls) if "cloud_platform.db.schema_parity" in call
        )
        start = calls.index(_application_starts(calls, canonical=False)[0])
        smoke = next(i for i, call in enumerate(calls) if "post_deploy_smoke.py" in call)
        assert stop < migration < schema < start < smoke
        assert all(
            "--project-name cloud-platform-production" in call
            for call in calls
            if call.startswith("compose ") and call != "compose version"
        )

    @needs_bash
    def test_runtime_failure_rolls_back_with_canonical(self, tmp_path: Path) -> None:
        result, deploy_dir, canonical, _, env_before, config_before = _release_harness(
            tmp_path, fail_up_api=True
        )
        assert result.returncode == 0, f"harness itself failed: {result.stderr}"
        assert "MAIN_RC=1" in result.stdout
        assert "release compose promoted" not in result.stdout
        assert result.stdout.count("ROLLBACK DONE") == 1
        calls = _stub_calls(tmp_path, "docker-calls.log")
        up_lines = [
            line
            for line in calls.splitlines()
            if "up -d api worker bot" in line.replace("--no-deps ", "")
        ]
        assert len(up_lines) == 2
        assert "candidate" in up_lines[0]
        assert "candidate" not in up_lines[1]
        assert (deploy_dir / "deploy.env").read_bytes() == env_before
        assert canonical.read_bytes() == b"# canonical release contract\nservices: {}\n"
        assert (tmp_path / "configuration.toml").read_bytes() == config_before

    @needs_bash
    def test_failed_migration_does_not_promote(self, tmp_path: Path) -> None:
        result, deploy_dir, canonical, _, env_before, config_before = _release_harness(
            tmp_path, fail_migrate=True
        )
        assert result.returncode == 0, f"harness itself failed: {result.stderr}"
        assert "MAIN_RC=1" in result.stdout
        assert "release compose promoted" not in result.stdout
        assert result.stdout.count("ROLLBACK DONE") == 1
        assert (deploy_dir / "deploy.env").read_bytes() == env_before
        assert canonical.read_bytes() == b"# canonical release contract\nservices: {}\n"
        assert (tmp_path / "configuration.toml").read_bytes() == config_before

    @needs_bash
    def test_database_behind_image_head_never_starts_services(self, tmp_path: Path) -> None:
        """Regression: code that queries a table the database lacks reached prod.

        The database must be AT the head the release image ships BEFORE any
        schema-dependent service starts. This is the exact failure mode of the
        `relation "provider_routes" does not exist` incident: the image shipped
        migration 0035, the database stayed at 0033, and api/worker/bot started
        anyway.
        """
        result, deploy_dir, canonical, _, env_before, _config_before = _release_harness(
            tmp_path, alembic_db_head="0033 (head)"
        )
        assert result.returncode == 0, f"harness itself failed: {result.stderr}"
        assert "MAIN_RC=1" in result.stdout
        assert "database is NOT at the image alembic head" in result.stderr
        calls = _stub_calls(tmp_path, "docker-calls.log")
        up_lines = [
            line
            for line in calls.splitlines()
            if "up -d api worker bot" in line.replace("--no-deps ", "")
        ]
        # Even an unstamped migration may have changed financial data. No old
        # or candidate writers may start after this attempted transition.
        assert up_lines == [], calls
        assert "schema transition attempted" in result.stderr
        assert "release compose promoted" not in result.stdout
        assert canonical.read_bytes() == b"# canonical release contract\nservices: {}\n"
        assert (deploy_dir / "deploy.env").read_bytes() == env_before.replace(
            _OLD_IMAGE.encode(), _NEW_IMAGE.encode()
        )

    @needs_bash
    def test_revision_match_with_missing_physical_schema_never_starts_services(
        self, tmp_path: Path
    ) -> None:
        """Regression: the CONFIRMED production shape must be rejected.

        Reproduces it exactly: ``alembic_version`` == 0037, migration 0036's FX
        columns and 0037's ``sellable_offers.provider_account_id`` present,
        while migration 0035's objects (``provider_routes``,
        ``provider_orders.credential_account_id``,
        ``servers.credential_account_id``) are MISSING. The revision matches the
        image, so only the physical-schema gate can catch it - and it must do so
        before any schema-dependent service is replaced.
        """
        result, deploy_dir, canonical, candidate, env_before, _config_before = _release_harness(
            tmp_path,
            alembic_image_head="0037 (head)",
            alembic_db_head="0037 (head)",
            expected_head="0037",
            fail_schema_parity=True,
        )
        assert result.returncode == 0, f"harness itself failed: {result.stderr}"
        assert "MAIN_RC=1" in result.stdout
        assert "physical schema does not support the release" in result.stderr
        calls = _stub_calls(tmp_path, "docker-calls.log")
        gate_lines = [line for line in calls.splitlines() if "schema_parity" in line]
        assert gate_lines, calls
        # Read with the RELEASE image: deploy.env still holds the previous one.
        assert "PROBE_USED_THE_WRONG_IMAGE" not in result.stderr
        # No CANDIDATE service start at all: the drift is caught before api/
        # worker/bot are replaced, so the running release keeps serving.
        up_lines = [
            line
            for line in calls.splitlines()
            if "up -d api worker bot" in line.replace("--no-deps ", "")
        ]
        assert all("candidate" not in line for line in up_lines), up_lines
        assert "release compose promoted" not in result.stdout
        # The recorded image reference goes back to the release still running,
        # and the failed release leaves no stale candidate behind.
        assert result.stdout.count("ROLLBACK DONE") == 1
        assert not candidate.exists()
        assert (deploy_dir / "deploy.env").read_bytes() == env_before
        assert canonical.read_bytes() == b"# canonical release contract\nservices: {}\n"

    @needs_bash
    def test_image_with_multiple_heads_fails_before_mutation(self, tmp_path: Path) -> None:
        """Two open heads mean `upgrade head` is ambiguous: refuse to deploy."""
        result, deploy_dir, canonical, _, env_before, _config_before = _release_harness(
            tmp_path, alembic_image_head="0033 (head)\n0034 (head)"
        )
        assert result.returncode == 0, f"harness itself failed: {result.stderr}"
        assert "MAIN_RC=1" in result.stdout
        assert "ships MULTIPLE alembic heads" in result.stderr
        assert "no rollback needed" in result.stdout
        calls = _stub_calls(tmp_path, "docker-calls.log")
        assert "alembic upgrade" not in calls
        assert not _application_starts(calls)
        assert (deploy_dir / "deploy.env").read_bytes() == env_before
        assert canonical.read_bytes() == b"# canonical release contract\nservices: {}\n"

    @needs_bash
    def test_pipeline_head_drift_fails_before_mutation(self, tmp_path: Path) -> None:
        """EXPECTED_HEAD is cross-checked against the head in the artifact.

        A CI variable can drift from the built image; the artifact cannot drift
        from itself. Detecting the drift pre-flight keeps a deploy from applying
        the WRONG schema while believing it applied the expected one.
        """
        result, deploy_dir, canonical, candidate, env_before, _config_before = _release_harness(
            tmp_path, expected_head="0033"
        )
        assert result.returncode == 0, f"harness itself failed: {result.stderr}"
        assert "MAIN_RC=1" in result.stdout
        assert "does not match the head shipped in the image" in result.stderr
        assert "no rollback needed" in result.stdout
        calls = _stub_calls(tmp_path, "docker-calls.log")
        assert "alembic upgrade" not in calls
        assert not _application_starts(calls)
        assert candidate.exists()
        assert (deploy_dir / "deploy.env").read_bytes() == env_before
        assert canonical.read_bytes() == b"# canonical release contract\nservices: {}\n"

    @needs_bash
    @pytest.mark.parametrize("attempts", ["0", "00", "08", "99999999999999999999", "not-a-number"])
    def test_invalid_health_attempts_cannot_bypass_readiness(
        self, tmp_path: Path, attempts: str
    ) -> None:
        result, deploy_dir, canonical, _, env_before, config_before = _release_harness(
            tmp_path, env_overrides={"HEALTH_ATTEMPTS": attempts}
        )
        _assert_no_mutation(tmp_path, result, deploy_dir, canonical, env_before, config_before)

    @needs_bash
    def test_image_tag_cannot_claim_a_different_commit(self, tmp_path: Path) -> None:
        result, deploy_dir, canonical, _, env_before, _ = _release_harness(
            tmp_path, env_overrides={"STUB_IMAGE_REVISION": _OLD_SHA}
        )
        assert "MAIN_RC=1" in result.stdout
        assert "revision does not match" in result.stderr
        assert (deploy_dir / "deploy.env").read_bytes() == env_before
        assert canonical.read_bytes() == b"# canonical release contract\nservices: {}\n"
        calls = _stub_calls(tmp_path, "docker-calls.log")
        assert " run " not in calls
        assert " up " not in calls

    @needs_bash
    def test_branched_database_cannot_hide_behind_its_first_revision(self, tmp_path: Path) -> None:
        result, deploy_dir, _, _, env_before, _ = _release_harness(
            tmp_path, alembic_db_head="0034 (head)\n0033 (head)"
        )
        assert "MAIN_RC=1" in result.stdout
        assert "multiple migration revisions" in result.stderr
        calls = _stub_calls(tmp_path, "docker-calls.log").splitlines()
        assert not any(call.endswith(" run --rm --no-deps migrate") for call in calls)
        assert not any(" stop " in call for call in calls)
        assert not _application_starts(calls)
        assert (deploy_dir / "deploy.env").read_bytes() == env_before

    @needs_bash
    @pytest.mark.parametrize("observed", ["0033 (head)", "0034 (head)"])
    def test_attempted_schema_transition_never_restarts_an_old_financial_epoch(
        self, tmp_path: Path, observed: str
    ) -> None:
        result, deploy_dir, canonical, _, env_before, _ = _release_harness(
            tmp_path,
            fail_migrate=True,
            alembic_db_head=observed,
            env_overrides={
                "ALEMBIC_DB_HEAD_BEFORE": "0033 (head)",
                "ALLOW_FORWARD_SCHEMA_ROLLBACK": "1",
            },
        )
        assert "MAIN_RC=1" in result.stdout
        assert "schema transition attempted" in result.stderr
        calls = _stub_calls(tmp_path, "docker-calls.log").splitlines()
        assert any("candidate.yml stop --timeout 45 api worker bot" in call for call in calls)
        assert not _application_starts(calls)
        assert (deploy_dir / "deploy.env").read_bytes() == env_before.replace(
            _OLD_IMAGE.encode(), _NEW_IMAGE.encode()
        )
        assert canonical.read_bytes() == b"# canonical release contract\nservices: {}\n"

    @needs_bash
    def test_failed_readiness_after_forward_migration_leaves_zero_pollers(
        self, tmp_path: Path
    ) -> None:
        result, _, canonical, _, _, _ = _release_harness(
            tmp_path,
            fail_ready=True,
            env_overrides={"ALEMBIC_DB_HEAD_BEFORE": "0033 (head)"},
        )
        assert "MAIN_RC=1" in result.stdout
        calls = _stub_calls(tmp_path, "docker-calls.log").splitlines()
        starts = _application_starts(calls, canonical=False)
        assert len(starts) == 1 and "candidate" in starts[0]
        assert calls.index(starts[0]) < next(
            i
            for i, call in enumerate(calls)
            if "candidate.yml stop --timeout 45 api worker bot" in call
        )
        assert not _application_starts(calls, canonical=True)
        assert canonical.read_bytes() == b"# canonical release contract\nservices: {}\n"

    @needs_bash
    def test_first_deploy_failure_stops_its_partially_started_bot(self, tmp_path: Path) -> None:
        result, _, _, _, _, _ = _release_harness(tmp_path, previous_image="", fail_up_api=True)
        assert "MAIN_RC=1" in result.stdout
        calls = _stub_calls(tmp_path, "docker-calls.log").splitlines()
        start = calls.index(_application_starts(calls, canonical=False)[0])
        stop = next(
            i
            for i, call in enumerate(calls)
            if "candidate.yml stop --timeout 45 api worker bot" in call
        )
        assert start < stop
        assert not _application_starts(calls, canonical=True)

    @needs_bash
    @pytest.mark.parametrize("service", ["worker", "bot"])
    def test_running_but_unhealthy_service_cannot_promote(
        self, tmp_path: Path, service: str
    ) -> None:
        result, _, canonical, _, _, _ = _release_harness(
            tmp_path, env_overrides={"STUB_UNHEALTHY_SERVICE": service}
        )
        assert "MAIN_RC=1" in result.stdout
        assert f"{service} did not establish" in result.stderr
        assert canonical.read_bytes() == b"# canonical release contract\nservices: {}\n"

    @needs_bash
    def test_failed_financial_smoke_cannot_promote(self, tmp_path: Path) -> None:
        result, _, canonical, _, _, _ = _release_harness(
            tmp_path, env_overrides={"FAIL_SMOKE": "1"}
        )
        assert "MAIN_RC=1" in result.stdout
        assert canonical.read_bytes() == b"# canonical release contract\nservices: {}\n"

    @needs_bash
    @pytest.mark.parametrize("failure", ["STUB_FAIL_STATUS", "STUB_TAMPER_CANDIDATE"])
    def test_final_gate_failure_still_has_the_previous_canonical_contract(
        self, tmp_path: Path, failure: str
    ) -> None:
        result, deploy_dir, canonical, _, env_before, _ = _release_harness(
            tmp_path, env_overrides={failure: "1"}
        )
        assert "MAIN_RC=1" in result.stdout
        assert result.stdout.count("ROLLBACK DONE") == 1
        assert canonical.read_bytes() == b"# canonical release contract\nservices: {}\n"
        assert (deploy_dir / "deploy.env").read_bytes() == env_before

    @needs_bash
    def test_inherited_image_variable_cannot_override_the_release(self, tmp_path: Path) -> None:
        result, deploy_dir, _, _, env_before, _ = _release_harness(
            tmp_path, env_overrides={"PLATFORM_IMAGE": _OLD_IMAGE}
        )
        assert "MAIN_RC=0" in result.stdout, result.stderr
        assert "IMAGE_DRIFT" not in result.stderr
        assert (deploy_dir / "deploy.env").read_bytes() == env_before.replace(
            _OLD_IMAGE.encode(), _NEW_IMAGE.encode()
        )

    @needs_bash
    def test_pull_failure_does_not_stop_or_restart_the_current_poller(self, tmp_path: Path) -> None:
        result, deploy_dir, _, _, env_before, _ = _release_harness(
            tmp_path, env_overrides={"STUB_FAIL_COMPOSE_PULL": "1"}
        )
        assert "MAIN_RC=1" in result.stdout
        calls = _stub_calls(tmp_path, "docker-calls.log").splitlines()
        assert not any(" stop " in call or " up " in call for call in calls)
        assert (deploy_dir / "deploy.env").read_bytes() == env_before

    @needs_bash
    def test_duplicate_bot_rollback_stops_instead_of_reporting_success(
        self, tmp_path: Path
    ) -> None:
        result, _, canonical, _, _, _ = _release_harness(
            tmp_path, env_overrides={"STUB_TWO_BOTS": "1"}
        )
        assert "MAIN_RC=1" in result.stdout
        assert "ROLLBACK DONE" not in result.stdout
        calls = _stub_calls(tmp_path, "docker-calls.log").splitlines()
        assert calls[-1].endswith("docker-compose.yml stop --timeout 45 api worker bot")
        assert canonical.read_bytes() == b"# canonical release contract\nservices: {}\n"

    @needs_bash
    def test_old_image_schema_rejection_never_starts_its_writers(self, tmp_path: Path) -> None:
        result, _, _, _, _, _ = _release_harness(
            tmp_path, fail_ready=True, fail_rollback_schema=True
        )
        assert "MAIN_RC=1" in result.stdout
        calls = _stub_calls(tmp_path, "docker-calls.log")
        assert not _application_starts(calls, canonical=True)

    @needs_bash
    @pytest.mark.parametrize("signal", ["HUP", "INT", "TERM"])
    def test_disconnect_signal_does_not_interrupt_the_migration_window(
        self, tmp_path: Path, signal: str
    ) -> None:
        result, _, canonical, _, _, _ = _release_harness(
            tmp_path, env_overrides={"STUB_MIGRATION_SIGNAL": signal}
        )
        assert "MAIN_RC=0" in result.stdout, result.stderr
        assert canonical.read_bytes() == b"# release candidate contract\nservices: {}\n"
        calls = _stub_calls(tmp_path, "docker-calls.log").splitlines()
        assert len(_application_starts(calls, canonical=False)) == 1

    @needs_bash
    def test_second_bot_appearing_during_stabilization_fails_closed(self, tmp_path: Path) -> None:
        result, _, canonical, _, _, _ = _release_harness(
            tmp_path, env_overrides={"STUB_LATE_TWO_BOTS": "1"}
        )
        assert "MAIN_RC=1" in result.stdout
        assert "bot container changed during stabilization" in result.stderr
        assert "ROLLBACK DONE" not in result.stdout
        calls = _stub_calls(tmp_path, "docker-calls.log").splitlines()
        assert calls[-1].endswith("docker-compose.yml stop --timeout 45 api worker bot")
        assert canonical.read_bytes() == b"# canonical release contract\nservices: {}\n"

    @needs_bash
    def test_unavailable_restart_evidence_is_not_stability(self, tmp_path: Path) -> None:
        result, _, canonical, _, _, _ = _release_harness(
            tmp_path, env_overrides={"STUB_FAIL_RESTART_PROBE": "1"}
        )
        assert "MAIN_RC=1" in result.stdout
        assert "cannot inspect bot restart count" in result.stderr
        assert canonical.read_bytes() == b"# canonical release contract\nservices: {}\n"

    @needs_bash
    @pytest.mark.skipif(
        os.name == "nt" or shutil.which("flock") is None,
        reason="real POSIX host-lock contract",
    )
    def test_main_refuses_a_second_host_mutator_before_deploy(self, tmp_path: Path) -> None:
        import fcntl

        command = (
            f"export DEPLOY_PRODUCTION_SOURCED=1 DEPLOY_PATH={shlex.quote(tmp_path.as_posix())} "
            f"PLATFORM_IMAGE_NEW={_NEW_IMAGE} CANDIDATE_COMPOSE_FILE=unused "
            f"EXPECTED_COMPOSE_SHA256={'0' * 64}; "
            f"source {shlex.quote(DEPLOY_SCRIPT.as_posix())}; "
            "deploy() { echo UNSAFE_MUTATION; }; main"
        )
        with (tmp_path / ".deployment.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run(
                [BASH, "-c", command], capture_output=True, text=True, timeout=10
            )
        assert result.returncode != 0
        assert "UNSAFE_MUTATION" not in result.stdout

    @needs_bash
    def test_failed_readiness_does_not_promote(self, tmp_path: Path) -> None:
        result, deploy_dir, canonical, _, env_before, config_before = _release_harness(
            tmp_path, fail_ready=True
        )
        assert result.returncode == 0, f"harness itself failed: {result.stderr}"
        assert "MAIN_RC=1" in result.stdout
        assert "release compose promoted" not in result.stdout
        assert (deploy_dir / "deploy.env").read_bytes() == env_before
        assert canonical.read_bytes() == b"# canonical release contract\nservices: {}\n"
        assert (tmp_path / "configuration.toml").read_bytes() == config_before

    @needs_bash
    def test_stabilization_failure_does_not_promote(self, tmp_path: Path) -> None:
        result, deploy_dir, canonical, _, env_before, config_before = _release_harness(
            tmp_path, crash_loop=True
        )
        assert result.returncode == 0, f"harness itself failed: {result.stderr}"
        assert "MAIN_RC=1" in result.stdout
        assert "release compose promoted" not in result.stdout
        assert (deploy_dir / "deploy.env").read_bytes() == env_before
        assert canonical.read_bytes() == b"# canonical release contract\nservices: {}\n"
        assert (tmp_path / "configuration.toml").read_bytes() == config_before


class TestDeployProfiles:
    """`DEPLOY_PROFILE` selects the lane's WORK, never its safety.

    Both lanes run this one engine. `staging` exists to make the loop fast, so
    it skips exactly one expensive step - the one-shot provider catalog refresh
    that the worker's scheduled coordinator already owns. Every other gate
    (configuration preflight, migrations + head + physical schema, catalog
    canonicalization, single bot, health, storefront readiness, rollback) is
    identical, and `test_staging_profile_still_fails_on_a_broken_storefront`
    proves it on the failure path.
    """

    @needs_bash
    def test_production_profile_runs_the_catalog_refresh(self, tmp_path: Path) -> None:
        result, _, _, _, _, _ = _release_harness(tmp_path)
        assert result.returncode == 0, f"harness itself failed: {result.stderr}"
        assert "MAIN_RC=0" in result.stdout
        assert "deploy profile      : production" in result.stdout
        assert "refreshing provider catalog facts" in result.stdout
        assert "catalog auto-sync run" in _stub_calls(tmp_path, "docker-calls.log")

    @needs_bash
    def test_staging_profile_skips_only_the_catalog_refresh(self, tmp_path: Path) -> None:
        result, _, _, _, _, _ = _release_harness(tmp_path, deploy_profile="staging")
        assert result.returncode == 0, f"harness itself failed: {result.stderr}"
        assert "MAIN_RC=0" in result.stdout
        assert "deploy profile      : staging" in result.stdout
        assert "skipping the one-shot catalog refresh" in result.stdout
        calls = _stub_calls(tmp_path, "docker-calls.log")
        assert "catalog auto-sync run" not in calls
        # Every other release transition and gate still runs.
        assert "run --rm --no-deps migrate" in calls
        assert "normalize-selling-currency --execute" in calls
        assert "storefront readiness: ok" in result.stdout
        assert "DEPLOYMENT SUCCEEDED" in result.stdout

    @needs_bash
    def test_staging_profile_still_fails_on_a_broken_storefront(self, tmp_path: Path) -> None:
        """The staging lane is faster, never blinder."""
        result, deploy_dir, canonical, _, env_before, _config_before = _release_harness(
            tmp_path, deploy_profile="staging", fail_storefront_readiness=True
        )
        assert result.returncode == 0, f"harness itself failed: {result.stderr}"
        assert "MAIN_RC=1" in result.stdout
        assert "storefront readiness failed" in result.stderr
        assert "release compose promoted" not in result.stdout
        assert result.stdout.count("ROLLBACK DONE") == 1
        assert (deploy_dir / "deploy.env").read_bytes() == env_before
        assert canonical.read_bytes() == b"# canonical release contract\nservices: {}\n"

    @needs_bash
    def test_unknown_profile_fails_before_any_mutation(self, tmp_path: Path) -> None:
        result, deploy_dir, canonical, _, env_before, config_before = _release_harness(
            tmp_path, deploy_profile="canary"
        )
        assert "DEPLOY_PROFILE must be" in result.stderr
        _assert_no_mutation(tmp_path, result, deploy_dir, canonical, env_before, config_before)


class TestStorefrontReadinessGate:
    """A green deploy must never leave the customer catalog EMPTY.

    Regression cover for the global-USD release: migrations green, services
    green, API ready — and 507 stored offers with ZERO sellable because every
    row still carried a legacy currency/provenance. The release therefore
    canonicalizes the catalog itself and then asserts that an enabled,
    credentialed, auto-priced provider actually has something on sale, all
    BEFORE the compose contract is promoted.
    """

    @needs_bash
    def test_refresh_failure_is_reported_and_never_fatal(self, tmp_path: Path) -> None:
        """A provider/API outage must not block an unrelated release.

        It is never silent either: the counters are printed, the step is logged
        as a WARN, and the storefront readiness gate stays authoritative.
        """
        result, _, _, _, _, _ = _release_harness(tmp_path, fail_catalog_refresh=True)
        assert result.returncode == 0, f"harness itself failed: {result.stderr}"
        assert "MAIN_RC=0" in result.stdout
        assert "[WARN] catalog refresh reported provider errors" in result.stdout
        calls = _stub_calls(tmp_path, "docker-calls.log")
        assert "catalog auto-sync run" in calls
        # The release still canonicalizes and still gates on readiness.
        assert "normalize-selling-currency --execute" in calls
        assert "storefront readiness: ok" in result.stdout

    @needs_bash
    def test_normalization_is_idempotent_and_never_fatal(self, tmp_path: Path) -> None:
        """An FX outage must not block an unrelated release, but is never silent."""
        result, _, _, _, _, _ = _release_harness(tmp_path, fail_normalize=True)
        assert result.returncode == 0, f"harness itself failed: {result.stderr}"
        assert "MAIN_RC=0" in result.stdout
        assert "[WARN] catalog normalization reported failures" in result.stdout
        calls = _stub_calls(tmp_path, "docker-calls.log")
        assert "normalize-selling-currency --execute" in calls

    @needs_bash
    def test_empty_storefront_for_an_enabled_provider_fails_the_release(
        self, tmp_path: Path
    ) -> None:
        result, deploy_dir, canonical, candidate, env_before, config_before = _release_harness(
            tmp_path, fail_storefront_readiness=True
        )
        assert result.returncode == 0, f"harness itself failed: {result.stderr}"
        assert "MAIN_RC=1" in result.stdout
        assert "storefront readiness failed" in result.stderr
        assert "nothing on sale" in result.stderr.lower() or "NOTHING on sale" in result.stderr
        # The release is NOT promoted and the previous image is restored.
        assert "release compose promoted" not in result.stdout
        assert "DEPLOYMENT SUCCEEDED" not in result.stdout
        assert result.stdout.count("ROLLBACK DONE") == 1
        assert (deploy_dir / "deploy.env").read_bytes() == env_before
        assert canonical.read_bytes() == b"# canonical release contract\nservices: {}\n"
        assert (tmp_path / "configuration.toml").read_bytes() == config_before
        assert not candidate.exists(), "a failed release must not leave a stale candidate"

    @needs_bash
    def test_open_storefront_lets_the_release_promote(self, tmp_path: Path) -> None:
        result, _, _, _, _, _ = _release_harness(tmp_path)
        assert result.returncode == 0, f"harness itself failed: {result.stderr}"
        assert "MAIN_RC=0" in result.stdout
        assert "storefront readiness: ok" in result.stdout
        assert "DEPLOYMENT SUCCEEDED" in result.stdout


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
