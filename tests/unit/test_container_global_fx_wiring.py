"""Container wiring for the GLOBAL (Frankfurter) FX path.

The storefront must be able to price a foreign catalog from a process-owned
resolver, keep its outage memo for the process lifetime, degrade to "no FX"
instead of crashing when the resolver cannot be built, and never close a
resolver the container still owns.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cloud_platform.core import container as container_mod
from cloud_platform.core.container import Container
from cloud_platform.providers.registry import ProviderRegistry


def _container() -> Container:
    return Container(
        session_factory=AsyncMock(),  # type: ignore[arg-type]
        provider_registry=ProviderRegistry(),
        provider_allocator=None,  # type: ignore[arg-type]
        hetzner_syncer=None,
    )


def _with_settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    real = container_mod.get_settings

    def _settings() -> Any:
        base = real()
        return SimpleNamespace(**{**base.__dict__, **overrides})

    monkeypatch.setattr(container_mod, "get_settings", _settings)


class TestGlobalResolverWiring:
    async def test_global_resolver_is_process_owned_and_memoized(self) -> None:
        container = _container()

        first = container.global_fx_resolver()
        second = container.global_fx_resolver()

        assert first is second
        assert first._container_owned is True
        assert first.source.source_name == "frankfurter"
        assert first.catalog_currency == "USD"
        assert first.catalog_stale_limit >= first.config.quote_ttl_seconds
        # Test hygiene: don't leak the owned HTTP transport past this test.
        await first.source.close()

    def test_global_config_is_read_from_server_owned_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_settings(
            monkeypatch,
            fx_enabled=True,
            fx_global_enabled=True,
            fx_catalog_pricing_currency="USD",
            fx_frankfurter_quote_ttl_seconds=600,
            fx_frankfurter_max_stale_seconds=7200,
            fx_frankfurter_catalog_max_stale_seconds=3600,
        )
        config = _container().global_fx_config()

        assert config.catalog_currency == "USD"
        assert config.quote_ttl_seconds == 600
        assert config.max_stale_seconds == 7200
        assert config.catalog_max_stale_seconds == 3600
        assert config.catalog_max_stale_seconds is not None
        assert config.catalog_max_stale_seconds <= config.max_stale_seconds

    @pytest.mark.parametrize(
        ("fx_enabled", "fx_global_enabled"), [(False, True), (True, False), (False, False)]
    )
    def test_disabled_global_fx_yields_no_resolver(
        self, monkeypatch: pytest.MonkeyPatch, fx_enabled: bool, fx_global_enabled: bool
    ) -> None:
        _with_settings(monkeypatch, fx_enabled=fx_enabled, fx_global_enabled=fx_global_enabled)

        assert _container().global_fx_resolver_or_none() is None

    def test_unbuildable_resolver_degrades_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Boom:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                raise RuntimeError("no client for you")

        _with_settings(monkeypatch, fx_enabled=True, fx_global_enabled=True)
        monkeypatch.setattr(
            "cloud_platform.providers.frankfurter_fx.client.FrankfurterFxClient", _Boom
        )

        # Catalog sync must keep running without FX rather than crash the worker.
        assert _container().global_fx_resolver_or_none() is None


class TestResolverLifecycleWiring:
    async def test_aclose_fx_keeps_container_owned_resolvers_open(self) -> None:
        container = _container()
        resolver = container.global_fx_resolver()

        await Container.aclose_fx(resolver)

        # Still usable and still the same process-owned instance, with its
        # Frankfurter transport still open for later pricing calls.
        assert container.global_fx_resolver() is resolver
        assert resolver.source._closed is False
        await resolver.source.close()

    async def test_aclose_fx_closes_unowned_resolvers(self) -> None:
        closed: list[bool] = []

        class _Foreign:
            async def close(self) -> None:
                closed.append(True)

        await Container.aclose_fx(_Foreign())
        await Container.aclose_fx(None)

        assert closed == [True]

    async def test_aclose_fx_never_raises_but_does_not_reach_owned_resources(self) -> None:
        class _Angry:
            async def close(self) -> None:
                raise ConnectionError("gone")

        await Container.aclose_fx(_Angry())

        owned = _container().global_fx_resolver()
        await Container.aclose_fx(owned)

        assert owned.source._closed is False
        await owned.source.close()

    @pytest.mark.parametrize(
        ("app_env", "expected_backend"), [("production", "redis"), ("test", "memory")]
    )
    def test_cache_backend_follows_the_deployment_environment(
        self, monkeypatch: pytest.MonkeyPatch, app_env: str, expected_backend: str
    ) -> None:
        _with_settings(monkeypatch, app_env=app_env, redis_url="redis://cache.test:6379/0")

        backend, redis_url = Container._fx_cache_backend()

        assert backend == expected_backend
        assert redis_url == "redis://cache.test:6379/0"
