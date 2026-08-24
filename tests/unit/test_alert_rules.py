"""Tests for the alert rules (M11-006).

Acceptance: alerts for spend, 429, provisioning failure, queue age and
reconciliation drift.

This module pins the alert-rules file to the code: the file must parse,
cover all five topics, wire the queue-age feeds for both operation types,
and reference only metric names that the PlatformMetrics registry actually
exports - so the rules cannot drift from the metrics.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from prometheus_client import CollectorRegistry

from cloud_platform.observability.metrics import PlatformMetrics

REPO_ROOT = Path(__file__).resolve().parents[2]
RULES_PATH = REPO_ROOT / "deploy" / "prometheus" / "alert-rules.yaml"


@pytest.fixture(scope="module")
def rules() -> dict:
    assert RULES_PATH.is_file(), f"missing alert rules file: {RULES_PATH}"
    data = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and "groups" in data
    return data


def _all_rules(rules: dict) -> list[dict]:
    out: list[dict] = []
    for group in rules["groups"]:
        for rule in group.get("rules", []):
            out.append(rule)
    return out


def _exported_metric_names() -> set[str]:
    """Every metric name a PlatformMetrics registry would expose."""
    m = PlatformMetrics(registry=CollectorRegistry())
    rendered = m.render().decode()
    names: set[str] = set()
    for line in rendered.splitlines():
        if line.startswith("# HELP"):
            # "# HELP cloud_platform_x <help>"
            names.add(line.split()[2])
    return names


class TestFileStructure:
    def test_file_exists_and_parses(self, rules: dict) -> None:
        assert rules["groups"], "no alert groups"
        names = [g["name"] for g in rules["groups"]]
        assert len(names) == len(set(names)), "group names must be unique"
        for group in rules["groups"]:
            assert group.get("rules"), f"group {group['name']} has no rules"

    def test_every_rule_is_well_formed(self, rules: dict) -> None:
        for rule in _all_rules(rules):
            assert rule.get("alert"), rule
            assert rule.get("expr", "").strip(), f"{rule.get('alert')} has no expr"
            assert "for" in rule, f"{rule['alert']} lacks a for-duration"
            assert rule.get("labels", {}).get("severity") in ("page", "warn", "info")
            assert rule.get("labels", {}).get("topic"), f"{rule['alert']} lacks a topic"
            annotations = rule.get("annotations", {})
            assert annotations.get("summary"), f"{rule['alert']} lacks a summary"
            assert annotations.get("description"), f"{rule['alert']} lacks a description"

    def test_alert_names_are_unique(self, rules: dict) -> None:
        names = [r["alert"] for r in _all_rules(rules)]
        assert len(names) == len(set(names)), f"duplicate alert names: {names}"


class TestTopics:
    """The acceptance: all five topics have at least one alert."""

    @pytest.mark.parametrize(
        "topic",
        ["spend", "provider-429", "provisioning-failure", "queue-age", "reconciliation-drift"],
    )
    def test_topic_is_covered(self, rules: dict, topic: str) -> None:
        topics = {r.get("labels", {}).get("topic") for r in _all_rules(rules)}
        assert topic in topics, f"no alert with topic {topic!r}"

    def test_429_alert_targets_rate_limited_outcome(self, rules: dict) -> None:
        for rule in _all_rules(rules):
            if rule.get("labels", {}).get("topic") == "provider-429" and "429" in rule["alert"]:
                assert "rate_limited" in rule["expr"]


class TestQueueAgeFeeds:
    """The queue-age alerts must have the underlying feeds wired for BOTH
    operation types (server_create and server_delete)."""

    def test_queue_age_alerts_reference_both_operation_types(self, rules: dict) -> None:
        exprs = [
            r["expr"] for r in _all_rules(rules) if r.get("labels", {}).get("topic") == "queue-age"
        ]
        assert exprs, "no queue-age alerts"
        combined = "\n".join(exprs)
        assert 'operation_type="server_delete"' in combined
        assert 'operation_type="server_create"' in combined

    def test_operation_type_values_match_the_gauge_labels(self) -> None:
        """The label values the rules use are the OperationType enum values
        (the worker jobs label the gauge with exactly these)."""
        from cloud_platform.modules.operations.domain import OperationType

        assert OperationType.SERVER_DELETE.value == "server_delete"
        assert OperationType.SERVER_CREATE.value == "server_create"


class TestMetricsExistence:
    """Every metric referenced by a rule must exist in the registry."""

    METRIC_RE = re.compile(r"cloud_platform_[a-z0-9_]+")

    def test_all_referenced_metrics_are_exported(self, rules: dict) -> None:
        exported = _exported_metric_names()
        referenced: set[str] = set()
        for rule in _all_rules(rules):
            referenced |= set(self.METRIC_RE.findall(rule["expr"]))
        missing = referenced - exported
        assert not missing, f"alert rules reference unknown metrics: {sorted(missing)}"

    def test_exported_metrics_are_rendered(self) -> None:
        exported = _exported_metric_names()
        for expected in (
            "cloud_platform_api_requests_total",
            "cloud_platform_job_runs_total",
            "cloud_platform_provider_calls_total",
            "cloud_platform_billing_events_total",
            "cloud_platform_daily_global_provider_cost_minor",
            "cloud_platform_daily_provider_cost_minor",
            "cloud_platform_provisioning_failures_total",
            "cloud_platform_reconciliation_outcomes_total",
            "cloud_platform_operation_queue_age_seconds",
        ):
            assert expected in exported, f"{expected} missing from the metrics registry"
