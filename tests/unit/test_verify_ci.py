"""Keep the explicitly different staging and release validation contracts."""

from __future__ import annotations

import pytest

from scripts import verify_ci


def test_staging_runs_only_fast_gates_after_explicit_tests(monkeypatch):
    calls = []
    monkeypatch.setattr(verify_ci, "_run", lambda command: calls.append(command) or 0)
    assert verify_ci.main(["--staging", "--test", "tests/unit/test_frankfurter_fx.py"]) == 0
    assert calls[0] == [
        "uv",
        "run",
        "pytest",
        "-q",
        "--no-cov",
        "-p",
        "no:cacheprovider",
        "tests/unit/test_frankfurter_fx.py",
    ]
    assert calls[1:-2] == verify_ci.STAGING_STEPS
    assert ["uv", "run", "python", "-m", "compileall", "-q", "src"] in calls
    assert "import cloud_platform.api.app" in calls[-3][-1]
    assert not any(word in str(calls) for word in ("mypy", "pre-commit", "--cov=", "--push-ready"))


def test_release_keeps_full_suite_types_secrets_and_exact_coverage(monkeypatch):
    calls = []
    monkeypatch.setattr(verify_ci, "_run", lambda command: calls.append(command) or 0)
    assert verify_ci.main(["--push-ready"]) == 0
    assert calls[:-2] == verify_ci.PUSH_READY_STEPS
    for gate in ("mypy", "pre-commit", "--cov-fail-under=88", "scripts/check_coverage.py"):
        assert gate in str(calls)


def test_staging_stops_immediately_on_targeted_regression_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(verify_ci, "_run", lambda command: calls.append(command) or 7)
    assert verify_ci.main(["--staging", "--test", "failing-test"]) == 7
    assert len(calls) == 1


def test_targeted_option_is_not_a_release_suite_replacement():
    with pytest.raises(SystemExit, match="2"):
        verify_ci.main(["--push-ready", "--test", "test"])
