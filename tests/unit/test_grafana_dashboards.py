"""Tests for the Grafana golden-signals dashboard (M11-005).

Acceptance: golden signals visible.

The dashboard is a static artifact; the tests keep it honest by (1) checking
it is valid Grafana dashboard JSON, (2) cross-checking that every metric
referenced in a panel query is a family the platform actually emits
(extracted from the metrics module by AST, so a rename breaks this test
instead of silently producing an empty panel), and (3) requiring at least
one panel per golden signal (latency / traffic / errors / saturation).
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = REPO_ROOT / "deploy" / "grafana" / "dashboards" / "cloud-platform-golden-signals.json"
METRICS = REPO_ROOT / "src" / "cloud_platform" / "observability" / "metrics.py"

_METRIC_RE = re.compile(r"cloud_platform_[a-z_0-9]+")


def _emitted_metric_families() -> set[str]:
    """The metric names constructed in the metrics module (AST - no import
    side effects, works without the prometheus_client registry)."""
    tree = ast.parse(METRICS.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in ("Counter", "Gauge", "Histogram", "Summary") and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    names.add(first.value)
    return names


def _load_dashboard() -> dict:
    assert DASHBOARD.is_file(), f"missing dashboard: {DASHBOARD}"
    return json.loads(DASHBOARD.read_text(encoding="utf-8"))


def _all_exprs(dashboard: dict) -> list[str]:
    exprs: list[str] = []
    for panel in dashboard.get("panels", []):
        for target in panel.get("targets", []):
            expr = target.get("expr")
            if expr:
                exprs.append(expr)
    return exprs


class TestDashboardShape:
    def test_is_valid_grafana_dashboard(self) -> None:
        d = _load_dashboard()
        assert isinstance(d.get("title"), str) and d["title"]
        assert isinstance(d.get("uid"), str) and d["uid"]
        assert isinstance(d.get("schemaVersion"), int)
        panels = d.get("panels")
        assert isinstance(panels, list) and len(panels) >= 10
        for panel in panels:
            assert isinstance(panel.get("title"), str), "every panel needs a title"
            assert isinstance(panel.get("id"), int), "every panel needs a unique id"
        ids = [p["id"] for p in panels]
        assert len(ids) == len(set(ids)), "panel ids must be unique"

    def test_time_panel_targets_reference_prometheus_datasource(self) -> None:
        d = _load_dashboard()
        assert "__inputs" in d and d["__inputs"][0]["pluginId"] == "prometheus"
        for panel in d.get("panels", []):
            if panel.get("type") == "row":
                continue
            for target in panel.get("targets", []):
                assert target.get("expr"), f"panel {panel['id']} target without expr"


def _normalize(name: str) -> str:
    """PromQL series names carry _bucket/_sum/_count suffixes; the family
    (what Counter/Histogram declare) does not."""
    return re.sub(r"_(bucket|sum|count)$", "", name)


class TestMetricsMatchPlatform:
    def test_every_referenced_metric_is_emitted(self) -> None:
        """A panel may only query families the platform actually exposes."""
        emitted = _emitted_metric_families()
        referenced = set()
        for expr in _all_exprs(_load_dashboard()):
            referenced.update(_normalize(m) for m in _METRIC_RE.findall(expr))
        unknown = referenced - emitted
        assert not unknown, (
            f"dashboard references metrics the platform never emits: {sorted(unknown)}"
        )

    def test_core_golden_signal_metrics_are_referenced(self) -> None:
        """The four signals' key families must appear in queries."""
        emitted = _emitted_metric_families()
        referenced = set()
        for expr in _all_exprs(_load_dashboard()):
            referenced.update(_normalize(m) for m in _METRIC_RE.findall(expr))
        for family in (
            "cloud_platform_api_request_duration_seconds",
            "cloud_platform_api_requests_total",
            "cloud_platform_provider_calls_total",
            "cloud_platform_operation_queue_age_seconds",
        ):
            assert family in emitted
            assert family in referenced, f"golden-signal metric {family} has no panel"


class TestGoldenSignalsVisible:
    def test_all_four_signal_rows_present(self) -> None:
        d = _load_dashboard()
        row_titles = [p["title"].upper() for p in d["panels"] if p.get("type") == "row"]
        for signal in ("LATENCY", "TRAFFIC", "ERRORS", "SATURATION"):
            assert any(signal in t for t in row_titles), f"no row for the {signal} signal"

    def test_latency_uses_quantiles_on_histograms(self) -> None:
        exprs = [e for e in _all_exprs(_load_dashboard()) if "duration_seconds" in e]
        assert any("histogram_quantile(0.95" in e for e in exprs)
        assert any("histogram_quantile(0.99" in e for e in exprs)

    def test_errors_row_has_5xx_and_provider_error_panels(self) -> None:
        exprs = _all_exprs(_load_dashboard())
        assert any('status=~"5.."' in e for e in exprs), "no API 5xx panel"
        assert any('outcome!="success"' in e for e in exprs), "no provider error panel"

    def test_saturation_row_has_queue_age_and_cost_panels(self) -> None:
        exprs = _all_exprs(_load_dashboard())
        assert any("cloud_platform_operation_queue_age_seconds" in e for e in exprs)
        assert any("cloud_platform_daily_global_provider_cost_minor" in e for e in exprs)


class TestProvisioningFiles:
    def test_datasource_provisioning_parses(self) -> None:
        import yaml

        path = REPO_ROOT / "deploy" / "grafana" / "provisioning" / "datasources" / "prometheus.yaml"
        assert path.is_file()
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert doc["apiVersion"] == 1
        ds = doc["datasources"][0]
        assert ds["type"] == "prometheus"
        assert ds["url"]

    def test_dashboard_provider_points_at_dashboard_dir(self) -> None:
        import yaml

        path = REPO_ROOT / "deploy" / "grafana" / "provisioning" / "dashboards" / "default.yaml"
        assert path.is_file()
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        provider = doc["providers"][0]
        assert provider["type"] == "file"
        assert "dashboards" in str(provider["options"]["path"])
