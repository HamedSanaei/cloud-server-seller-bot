"""Execute deployment shell guards without touching any remote service."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"
BASH = (
    "C:/Program Files/Git/bin/bash.exe"
    if os.name == "nt" and Path("C:/Program Files/Git/bin/bash.exe").is_file()
    else shutil.which("bash")
)


def _workflow(name: str = "deploy-staging.yml") -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _render(command: str, values: dict[str, str]) -> str:
    return re.sub(r"\$\{\{\s*(.*?)\s*\}\}", lambda match: values[match[1]], command)


def test_shared_runtime_reaps_children_without_weakening_probes_or_single_bot() -> None:
    compose = yaml.safe_load((ROOT / "deploy/production/docker-compose.yml").read_text())
    for name in ("api", "worker", "bot", "migrate"):
        assert compose["services"][name]["init"] is True
        assert compose["services"][name]["pids_limit"] == 256
    assert compose["services"]["bot"]["deploy"]["replicas"] == 1
    assert compose["services"]["worker"]["healthcheck"]["test"] == [
        "CMD",
        "python",
        "-m",
        "cloud_platform.worker.health",
    ]
    assert compose["services"]["api"]["networks"]["platform-bridge"]["aliases"] == [
        "cloud-platform-api",
    ]


@pytest.mark.skipif(BASH is None, reason="POSIX shell unavailable")
@pytest.mark.parametrize(
    "path, success",
    [
        ("/etc/platform/configuration.toml", True),
        ("/etc/platform/configurationXtoml", False),
        ("/etc/platform/configuration.toml;echo invalid", False),
    ],
)
def test_staging_identity_gate_validates_literal_toml_suffix_before_publishing(
    path: str,
    success: bool,
) -> None:
    workflow = _workflow()
    command = workflow["jobs"]["deployment-access"]["steps"][0]["run"]
    result = subprocess.run(
        [BASH, "-c", command],
        capture_output=True,
        timeout=10,
        env={
            **os.environ,
            "DEPLOY_HOST": "fixture",
            "DEPLOY_USER": "fixture",
            "SSH_KEY": "fixture-non-secret",
            "KNOWN_HOSTS": "fixture-non-secret",
            "DEPLOY_PATH": "/opt/platform",
            "CONFIGURATION_PATH": path,
        },
    )
    assert (result.returncode == 0) is success
    assert "deployment-access" in workflow["jobs"]["build"]["needs"]


@pytest.mark.skipif(BASH is None, reason="POSIX shell unavailable")
@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/heads/feature", "refs/tags/staging"])
def test_staging_resolver_rejects_other_branches_before_resolving_an_image(
    ref: str,
    tmp_path: Path,
) -> None:
    resolve = next(
        step for step in _workflow()["jobs"]["build"]["steps"] if step.get("id") == "resolve"
    )
    command = _render(
        resolve["run"],
        {
            "github.ref": ref,
            "github.sha": "a" * 40,
            "github.repository": "example/platform",
            "env.REGISTRY": "ghcr.io",
        },
    )
    output = tmp_path / "outputs"
    result = subprocess.run(
        [BASH, "-c", command],
        env={**os.environ, "GITHUB_OUTPUT": output.as_posix()},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert not output.exists()


@pytest.mark.skipif(BASH is None, reason="POSIX shell unavailable")
@pytest.mark.parametrize("sha", ["main", "a" * 39, "a" * 41, "A" * 40, "$(touch injected)"])
def test_production_sha_input_is_data_and_must_be_a_full_hex_commit(
    sha: str,
    tmp_path: Path,
) -> None:
    resolve = next(
        step
        for step in _workflow("deploy-production.yml")["jobs"]["build"]["steps"]
        if step.get("id") == "resolve"
    )
    command = _render(
        resolve["run"],
        {
            "github.repository": "example/platform",
            "env.REGISTRY": "ghcr.io",
        },
    )
    result = subprocess.run(
        [BASH, "-c", command],
        cwd=tmp_path,
        env={**os.environ, "REQUESTED_SHA": sha, "GITHUB_OUTPUT": "outputs"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert not (tmp_path / "outputs").exists()
    assert not (tmp_path / "injected").exists()


@pytest.mark.skipif(BASH is None, reason="POSIX shell unavailable")
def test_production_resolver_emits_only_the_validated_sha_image(tmp_path: Path) -> None:
    resolve = next(
        step
        for step in _workflow("deploy-production.yml")["jobs"]["build"]["steps"]
        if step.get("id") == "resolve"
    )
    command = _render(
        resolve["run"],
        {
            "github.repository": "Example/Platform",
            "env.REGISTRY": "ghcr.io",
        },
    )
    result = subprocess.run(
        [BASH, "-c", command],
        cwd=tmp_path,
        env={**os.environ, "REQUESTED_SHA": "a" * 40, "GITHUB_OUTPUT": "outputs"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "outputs").read_text().splitlines() == [
        "sha=" + "a" * 40,
        "image=ghcr.io/example/platform",
    ]


@pytest.mark.skipif(BASH is None, reason="POSIX shell unavailable")
@pytest.mark.parametrize("name", ["ci.yml", "deploy-staging.yml", "deploy-production.yml"])
def test_all_workflow_shell_commands_parse_after_expression_interpolation(name: str) -> None:
    for job in _workflow(name)["jobs"].values():
        for step in job.get("steps", []):
            if "run" not in step:
                continue
            # Only parse; no Docker, database, SSH or GitHub calls are executed.
            command = re.sub(r"\$\{\{.*?\}\}", "placeholder", step["run"])
            result = subprocess.run(
                [BASH, "-n"],
                input=command,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 0, (name, step.get("name"), result.stderr)


@pytest.mark.skipif(BASH is None, reason="POSIX shell unavailable")
@pytest.mark.parametrize("name", ["deploy-staging.yml", "deploy-production.yml"])
@pytest.mark.parametrize(
    "status,error,expected",
    [
        (0, "", "present=true"),
        (1, "manifest unknown", "present=false"),
        (1, "no such manifest: ghcr.io/example/platform:sha", "present=false"),
        (1, "unauthorized: authentication required", None),
        (1, "Get https://ghcr.io/v2/: context deadline exceeded", None),
    ],
)
def test_registry_errors_never_authorize_overwriting_an_existing_sha_tag(
    name: str,
    status: int,
    error: str,
    expected: str | None,
    tmp_path: Path,
) -> None:
    inspect = next(
        step for step in _workflow(name)["jobs"]["build"]["steps"] if step.get("id") == "exists"
    )
    command = _render(
        inspect["run"],
        {
            "steps.resolve.outputs.image": "ghcr.io/example/platform",
            "steps.resolve.outputs.sha": "a" * 40,
        },
    )
    result = subprocess.run(
        [
            BASH,
            "-c",
            (
                'docker() { printf "%s\\n" "$REGISTRY_ERROR" >&2; return "$REGISTRY_STATUS"; }\n'
                + command
            ),
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "RUNNER_TEMP": ".",
            "GITHUB_OUTPUT": "outputs",
            "REGISTRY_ERROR": error,
            "REGISTRY_STATUS": str(status),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    if expected is None:
        assert result.returncode != 0
        assert not (tmp_path / "outputs").exists()
        assert error in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "outputs").read_text().strip() == expected
