"""Tests for the generated-artifact hygiene gate (CI hygiene).

The gate fails when generated CI/test reports are tracked by Git. These
tests exercise the pure ``find_violations`` classifier (no git subprocess).
"""

from __future__ import annotations

from scripts.check_generated_artifacts import find_violations


class TestDeniedFiles:
    def test_junit_and_coverage_reports_flagged(self) -> None:
        assert find_violations(["pytest-results.xml"]) == ["pytest-results.xml"]
        assert find_violations(["junit.xml"]) == ["junit.xml"]
        assert find_violations(["coverage.xml"]) == ["coverage.xml"]
        assert find_violations([".coverage"]) == [".coverage"]

    def test_coverage_shards_flagged(self) -> None:
        assert find_violations([".coverage.localhost.123"]) == [".coverage.localhost.123"]

    def test_nested_reports_flagged(self) -> None:
        assert find_violations(["reports/pytest-results.xml"]) == ["reports/pytest-results.xml"]


class TestDeniedDirectories:
    def test_ci_artifacts_prefix_flagged(self) -> None:
        violations = find_violations(
            [".ci-artifacts/pytest-results.xml", ".ci-artifacts/coverage.xml"]
        )
        assert violations == [
            ".ci-artifacts/coverage.xml",
            ".ci-artifacts/pytest-results.xml",
        ]

    def test_test_results_and_htmlcov_flagged(self) -> None:
        assert find_violations(["test-results/junit.xml"]) == ["test-results/junit.xml"]
        assert find_violations(["htmlcov/index.html"]) == ["htmlcov/index.html"]


class TestCleanTree:
    def test_source_files_pass(self) -> None:
        assert (
            find_violations(
                [
                    "src/cloud_platform/cli.py",
                    "tests/unit/test_bot_ui.py",
                    "docs/roadmap/TASKS.yaml",
                    "configuration.example.toml",
                ]
            )
            == []
        )

    def test_xml_fixtures_are_not_reports(self) -> None:
        # Narrow denylist: a legitimate *.xml fixture must not trip the gate.
        assert find_violations(["tests/fixtures/sample.xml"]) == []
