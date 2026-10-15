"""Exercise delivery safety commands locally without a registry or SSH host."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts import post_deploy_smoke as smoke

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"
SCRIPT = ROOT / "scripts/deploy-production.sh"
BASH = (
    "C:/Program Files/Git/bin/bash.exe"
    if os.name == "nt" and Path("C:/Program Files/Git/bin/bash.exe").is_file()
    else shutil.which("bash")
)


def workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _required_result(results: dict, event: str) -> subprocess.CompletedProcess:
    command = workflow("ci.yml")["jobs"]["required"]["steps"][0]["run"]
    program = command.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    return subprocess.run(
        [sys.executable, "-c", program],
        env={**os.environ, "RESULTS": json.dumps(results), "EVENT": event},
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.mark.parametrize("failure", ["failure", "cancelled", "skipped"])
def test_required_aggregate_rejects_each_failed_or_skipped_safety_job(failure: str) -> None:
    jobs = workflow("ci.yml")["jobs"]
    required = jobs["required"]
    assert set(required["needs"]) == set(jobs) - {"required"}
    for failed_job in required["needs"]:
        results = {name: {"result": "success"} for name in required["needs"]}
        results[failed_job]["result"] = failure
        result = _required_result(results, "pull_request")
        assert result.returncode != 0, failed_job
        assert failed_job in result.stderr


@pytest.mark.parametrize("event", ["push", "pull_request", "workflow_dispatch"])
def test_required_aggregate_dependency_skip_is_only_allowed_on_push(event: str) -> None:
    required = workflow("ci.yml")["jobs"]["required"]
    results = {name: {"result": "success"} for name in required["needs"]}
    assert _required_result(results, event).returncode == 0
    results["dependency-review"]["result"] = "skipped"
    assert (_required_result(results, event).returncode == 0) is (event == "push")


_RELEASE_SHA = "a" * 40
_GH_RELEASE_STUB = r"""
import json
import os
import re
import sys

endpoint = sys.argv[2]
query = sys.argv[4]
if "/git/ref/heads/main" in endpoint:
    assert query == ".object.sha"
    print(os.environ["TEST_MAIN_SHA"])
elif "/actions/workflows/ci.yml/runs?" in endpoint:
    assert "head_sha=" + os.environ["RELEASE_SHA"] in endpoint
    assert "&branch=main&event=push&per_page=1" in endpoint
    print("123")
else:
    assert endpoint.endswith("/actions/runs/123")
    run = json.loads(os.environ["TEST_CI_RUN"])
    # Interpret the shipped query's equality conjunctions without requiring
    # jq on Windows. Every term must parse; dropped terms also fail below.
    seen = set()
    trusted = True
    for term in query.strip("()").split(" and "):
        match = re.fullmatch(r'\.([a-z_.]+) == "([^"]+)"', term)
        assert match, term
        field, expected = match.groups()
        seen.add(field)
        value = run
        for part in field.split("."):
            value = value.get(part, {})
        trusted = trusted and value == expected
    assert seen == {
        "path", "status", "conclusion", "event", "head_branch", "head_sha",
        "repository.full_name", "head_repository.full_name",
    }
    print(str(trusted).lower())
"""


@pytest.mark.skipif(BASH is None, reason="POSIX shell unavailable")
@pytest.mark.parametrize("job", ["build", "deploy"])
@pytest.mark.parametrize(
    "override",
    [
        {},
        {"conclusion": "failure"},
        {"conclusion": "cancelled"},
        {"status": "in_progress"},
        {"event": "pull_request"},
        {"head_branch": "staging"},
        {"path": ".github/workflows/untrusted.yml"},
        {"head_sha": "b" * 40},
        {"repository": {"full_name": "attacker/platform"}},
        {"head_repository": {"full_name": "attacker/platform"}},
        {"stale_main": True},
    ],
)
def test_release_guards_reject_untrusted_failed_pr_and_stale_runs(
    job: str, override: dict, tmp_path: Path
) -> None:
    """Execute both shipped API guards, not a duplicated acceptance predicate."""
    run = {
        "path": ".github/workflows/ci.yml",
        "status": "completed",
        "conclusion": "success",
        "event": "push",
        "head_branch": "main",
        "head_sha": _RELEASE_SHA,
        "repository": {"full_name": "example/platform"},
        "head_repository": {"full_name": "example/platform"},
        **override,
    }
    steps = workflow("deploy-production.yml")["jobs"][job]["steps"]
    guard = next(step for step in steps if step.get("id") == "ci") if job == "build" else steps[0]
    command = 'gh() { "$TEST_PYTHON" -c "$GH_RELEASE_STUB" "$@"; }\n' + guard["run"]
    result = subprocess.run(
        [BASH, "-c", command],
        cwd=tmp_path,
        env={
            **os.environ,
            "TEST_PYTHON": Path(sys.executable).as_posix(),
            "GH_RELEASE_STUB": _GH_RELEASE_STUB,
            "TEST_CI_RUN": json.dumps(run),
            "TEST_MAIN_SHA": "b" * 40 if override.get("stale_main") else _RELEASE_SHA,
            "GITHUB_REPOSITORY": "example/platform",
            "RELEASE_SHA": _RELEASE_SHA,
            "SOURCE_RUN_ID": "123",
            "CI_RUN_ID": "123",
            "GITHUB_OUTPUT": "outputs",
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert (result.returncode == 0) is (not override), result.stderr
    if job == "build":
        assert (tmp_path / "outputs").exists() is (not override)


@pytest.mark.skipif(BASH is None, reason="POSIX shell unavailable")
def test_manual_release_finds_exact_main_push_ci_without_bypassing_guards(tmp_path: Path) -> None:
    run = {
        "path": ".github/workflows/ci.yml",
        "status": "completed",
        "conclusion": "success",
        "event": "push",
        "head_branch": "main",
        "head_sha": _RELEASE_SHA,
        "repository": {"full_name": "example/platform"},
        "head_repository": {"full_name": "example/platform"},
    }
    guard = next(
        step
        for step in workflow("deploy-production.yml")["jobs"]["build"]["steps"]
        if step.get("id") == "ci"
    )
    result = subprocess.run(
        [
            BASH,
            "-c",
            'gh() { "$TEST_PYTHON" -c "$GH_RELEASE_STUB" "$@"; }\n' + guard["run"],
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "TEST_PYTHON": Path(sys.executable).as_posix(),
            "GH_RELEASE_STUB": _GH_RELEASE_STUB,
            "TEST_CI_RUN": json.dumps(run),
            "TEST_MAIN_SHA": _RELEASE_SHA,
            "GITHUB_REPOSITORY": "example/platform",
            "RELEASE_SHA": _RELEASE_SHA,
            "SOURCE_RUN_ID": "",
            "GITHUB_OUTPUT": "outputs",
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "outputs").read_text().strip() == "ci_run_id=123"


@pytest.mark.skipif(BASH is None, reason="POSIX shell unavailable")
@pytest.mark.parametrize("name", ["deploy-staging.yml", "deploy-production.yml"])
@pytest.mark.parametrize("login_exit,deploy_exit", [(0, 0), (23, 0), (0, 29)])
def test_remote_registry_credentials_are_private_and_cleaned_on_every_exit(
    name: str,
    login_exit: int,
    deploy_exit: int,
    tmp_path: Path,
) -> None:
    """Execute the shipped SSH wrapper with local shell stand-ins, not a source copy."""
    doc = workflow(name)
    deploy = doc["jobs"]["deploy"]
    step = next(step for step in deploy["steps"] if "GHCR_TOKEN" in step.get("env", {}))
    replacements = {
        "github.actor": "test-runner",
        "vars.PROD_PORT || 22": "22",
        "vars.PROD_USER": "deploy",
        "vars.PROD_HOST": "localhost",
        "vars.PROD_DEPLOY_PATH": "/srv/platform",
        "needs.build.outputs.image": "ghcr.io/test/platform",
        "needs.build.outputs.sha": "a" * 40,
        "needs.build.outputs.head": "0054",
        "steps.compose.outputs.candidate": ".candidate.yml",
        "steps.compose.outputs.sha256": "b" * 64,
    }
    command = re.sub(r"\$\{\{\s*(.*?)\s*\}\}", lambda m: replacements[m[1]], step["run"])
    (tmp_path / "scripts").mkdir()
    (tmp_path / "probe").mkdir()
    (tmp_path / "scripts/deploy-production.sh").write_text(
        "set -eu\n"
        '[ "${GHCR_TOKEN+x}" != x ]\n'
        '[ "$PLATFORM_IMAGE_NEW" = "ghcr.io/test/platform:' + "a" * 40 + '" ]\n'
        '[ "$EXPECTED_HEAD" = 0054 ]\n'
        '[ -f "$DOCKER_CONFIG/config.json" ]\n'
        'printf "%s" "${DEPLOY_PROFILE:-production}" > probe/deployed\n'
        'exit "$DEPLOY_EXIT"\n',
        encoding="utf-8",
    )
    # The SSH stand-in executes the real quoted remote command in a new shell;
    # docker consumes the actual token pipe and writes a real auth file.
    harness = (
        r"""
        ssh() { bash -c "${!#}"; }
        docker() {
            [ "$*" = "login ghcr.io -u test-runner --password-stdin" ] || return 91
            [ "$(cat)" = "$TEST_TOKEN" ] || return 92
            if [ "$CHECK_POSIX_MODE" = 1 ]; then
                [ "$(stat -c %a "$DOCKER_CONFIG")" = 700 ] || return 93
            fi
            printf '%s' "$DOCKER_CONFIG" > probe/auth-path
            printf 'private-auth' > "$DOCKER_CONFIG/config.json"
            return "$LOGIN_EXIT"
        }
        export -f ssh docker
        set +e
    """
        + "\n(\n"
        + command
        + r"""
        )
        result=$?
        auth_path="$(cat probe/auth-path)"
        [ ! -e "$auth_path" ] || exit 94
        exit "$result"
    """
    )
    result = subprocess.run(
        [BASH, "-c", harness],
        cwd=tmp_path,
        env={
            **os.environ,
            "GHCR_TOKEN": "test-ephemeral-token",
            "TEST_TOKEN": "test-ephemeral-token",
            "CHECK_POSIX_MODE": "0" if os.name == "nt" else "1",
            "LOGIN_EXIT": str(login_exit),
            "DEPLOY_EXIT": str(deploy_exit),
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == (login_exit or deploy_exit), result.stderr
    assert "test-ephemeral-token" not in result.stdout + result.stderr
    marker = tmp_path / "probe/deployed"
    assert marker.exists() is (login_exit == 0)
    if marker.exists():
        assert marker.read_text() == ("staging" if name == "deploy-staging.yml" else "production")


@pytest.mark.skipif(BASH is None, reason="POSIX shell unavailable")
def test_schema_advanced_rollback_cannot_be_overridden() -> None:
    command = f"""
        export DEPLOY_PRODUCTION_SOURCED=1
        source '{SCRIPT.as_posix()}'
        PREV_IMAGE=ghcr.io/test/repo:old
        MIGRATION_STARTED=yes
        DB_HEAD_BEFORE=0053
        EXPECTED_HEAD=0054
        ALLOW_FORWARD_SCHEMA_ROLLBACK=1
        db_head() {{ echo 0054; }}
        compose_candidate() {{ return 0; }}
        compose_current() {{ return 0; }}
        set_platform_image() {{ echo UNSAFE_IMAGE_RESTART; }}
        set +e
        rollback
        echo ROLLBACK_RC=$?
    """
    result = subprocess.run([BASH, "-c", command], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "ROLLBACK_RC=1" in result.stdout
    assert "UNSAFE_IMAGE_RESTART" not in result.stdout


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("flock") is None,
    reason="real Linux host-lock contract (runs on GitHub-hosted Linux)",
)
def test_real_flock_refuses_second_deployer(tmp_path: Path) -> None:
    import fcntl

    with (tmp_path / ".deployment.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            ["flock", "-n", str(tmp_path / ".deployment.lock"), "true"],
            capture_output=True,
            timeout=10,
        )
    assert result.returncode != 0


def test_smoke_revision_parser_handles_merge_parents_and_rejects_duplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    versions = tmp_path / "alembic/versions"
    versions.mkdir(parents=True)
    (versions / "a.py").write_text("revision = 'a'\ndown_revision = None\n")
    (versions / "b.py").write_text("revision = 'b'\ndown_revision = 'a'\n")
    (versions / "c.py").write_text("revision = 'c'\ndown_revision = ('a', 'b')\n")
    monkeypatch.setattr(smoke, "REPO_ROOT", tmp_path)
    assert smoke.repo_head_revision() == "c"
    (versions / "duplicate.py").write_text("revision = 'a'\ndown_revision = None\n")
    with pytest.raises(ValueError, match="duplicate migration revision"):
        smoke.repo_head_revision()
