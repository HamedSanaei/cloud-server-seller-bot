"""Configuration privacy and bounded unauthenticated metric cardinality."""

from __future__ import annotations

import importlib
import traceback
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from cloud_platform.core.config import Settings, load_settings
from cloud_platform.observability.metrics import PlatformMetrics


@pytest.mark.parametrize("environment", ["production", " Production ", "staging", "unknown"])
def test_header_impersonation_is_rejected_outside_explicit_development(environment):
    with pytest.raises(ValidationError, match="development/test"):
        Settings(
            app_env=environment,
            telegram_sessions_backend="redis",
            api_allow_header_identity=True,
        )


@pytest.mark.parametrize(
    "section,field",
    [("telegram", "bot_token"), ("security", "provider_credential_encryption_key")],
)
def test_invalid_secret_inputs_never_reach_rendered_configuration_tracebacks(
    tmp_path, section, field
):
    sentinel = "private-example-credential"
    config = tmp_path / "invalid.toml"
    config.write_text(f'[{section}]\n{field} = ["{sentinel}"]\n', encoding="utf-8")
    with pytest.raises(ValidationError) as failure:
        load_settings(config)
    rendered = "".join(traceback.format_exception(failure.value))
    assert sentinel not in rendered
    assert "input_value" not in rendered


def _request_samples(registry):
    return [
        sample
        for family in registry.collect()
        for sample in family.samples
        if sample.name == "cloud_platform_api_requests_total"
    ]


def test_unknown_paths_and_methods_cannot_allocate_unbounded_metric_series(monkeypatch):
    module = importlib.import_module("cloud_platform.api.app")
    metrics = PlatformMetrics()
    monkeypatch.setattr(module, "metrics", metrics)
    app = module.create_app()
    with TestClient(app) as client:
        for index in range(32):
            assert client.get(f"/unknown/{uuid4()}").status_code == 404
            assert client.request(f"CUSTOM{index}", f"/missing/{uuid4()}").status_code == 404
    samples = _request_samples(metrics.registry)
    assert {(sample.labels["method"], sample.labels["path"]) for sample in samples} == {
        ("GET", "<unmatched>"),
        ("OTHER", "<unmatched>"),
    }
    assert {sample.value for sample in samples} == {32}


def test_matched_resources_share_the_route_template_not_customer_identifiers(monkeypatch):
    module = importlib.import_module("cloud_platform.api.app")
    metrics = PlatformMetrics()
    monkeypatch.setattr(module, "metrics", metrics)
    app = module.create_app()

    @app.get("/audit/resources/{resource_id}")
    async def resource(resource_id: str):
        return {"id": resource_id}

    identities = [str(uuid4()), str(uuid4())]
    with TestClient(app) as client:
        for identity in identities:
            assert client.get(f"/audit/resources/{identity}").json() == {"id": identity}
    samples = _request_samples(metrics.registry)
    assert len(samples) == 1
    assert samples[0].labels == {
        "method": "GET",
        "path": "/audit/resources/{resource_id}",
        "status": "200",
    }
    assert samples[0].value == 2
