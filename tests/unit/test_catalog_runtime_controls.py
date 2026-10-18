"""Operator-only invalidation, rollback flags and worker-local exporter contracts."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cloud_platform.core.config import Settings, toml_to_settings


def test_runtime_flags_are_mapped_and_have_bounded_safe_defaults():
    values = toml_to_settings(
        {
            "storefront": {
                "catalog_sync": {
                    "batch_enabled": False,
                    "probe_cache_enabled": False,
                    "probe_cache_ttl_seconds": 3600,
                }
            },
            "worker": {"metrics_enabled": False, "metrics_port": 9102},
        }
    )
    settings = Settings(**values)
    assert not settings.catalog_batch_enabled and not settings.catalog_probe_cache_enabled
    assert settings.catalog_probe_cache_ttl_seconds == 3600
    assert not settings.worker_metrics_enabled and settings.worker_metrics_port == 9102
    with pytest.raises(ValueError):
        Settings(catalog_probe_cache_ttl_seconds=86401)


async def test_cli_cache_invalidation_is_account_region_scoped(monkeypatch, capsys):
    import cloud_platform.cli as cli
    from cloud_platform.providers.leaseweb.discovery_cache import RedisProbeCache

    redis = SimpleNamespace(aclose=AsyncMock())
    invalidate = AsyncMock(return_value=3)
    monkeypatch.setattr("redis.asyncio.Redis.from_url", lambda *a, **kw: redis)
    monkeypatch.setattr(RedisProbeCache, "invalidate", invalidate)
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(redis_url="redis://synthetic"))
    args = cli._parser().parse_args(
        [
            "catalog",
            "probe-cache",
            "invalidate",
            "--account",
            "account-a",
            "--region",
            "AMS-01",
        ]
    )
    assert await cli._dispatch(args) == 0
    invalidate.assert_awaited_once_with("account-a", "AMS-01")
    redis.aclose.assert_awaited_once()
    assert "entries=3" in capsys.readouterr().out
    args.account = "*"
    assert await cli._dispatch(args) == 2
    assert invalidate.await_count == 1


@pytest.mark.parametrize("enabled", [True, False])
async def test_exporter_uses_worker_registry_and_is_closed(monkeypatch, tmp_path, enabled):
    import cloud_platform.worker.settings as ws
    from cloud_platform.observability.metrics import metrics

    container = SimpleNamespace(reconcile_superadmins=AsyncMock())
    server = MagicMock()
    start_server = MagicMock(return_value=(server, MagicMock()))
    monkeypatch.setattr("prometheus_client.start_http_server", start_server)
    monkeypatch.setattr(
        "cloud_platform.core.container.get_container", AsyncMock(return_value=container)
    )
    monkeypatch.setattr("cloud_platform.core.container.close_container", AsyncMock())
    monkeypatch.setattr("cloud_platform.worker.health.HEARTBEAT_PATH", tmp_path / "heartbeat")
    monkeypatch.setattr(ws, "reconcile_capacity_evidence_once", AsyncMock())
    monkeypatch.setattr("cloud_platform.observability.tracing.setup_tracing", lambda *a, **kw: None)
    settings = SimpleNamespace(
        worker_metrics_enabled=enabled,
        worker_metrics_port=9101,
        otel_exporter_endpoint="",
        otel_sample_ratio=0,
    )
    monkeypatch.setattr("cloud_platform.core.config.get_settings", lambda: settings)
    ctx = {}
    await ws.startup(ctx)
    if enabled:
        start_server.assert_called_once_with(9101, addr="0.0.0.0", registry=metrics.registry)
    else:
        start_server.assert_not_called()
    await ws.shutdown(ctx)
    assert not ctx
    assert server.shutdown.call_count == int(enabled)
    assert server.server_close.call_count == int(enabled)
