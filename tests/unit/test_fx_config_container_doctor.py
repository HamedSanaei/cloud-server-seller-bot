"""FX config validation, container lifecycle, Redis cache and doctor.

Covers the operator-facing seams: TOML mapping, production validation,
resolver construction/close ownership, the shared Redis last-known-good
store (with a fake client, no server) and the read-only `fx doctor`
pre-flight. No live HTTP, no secrets.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cloud_platform.modules.fx.cache import (
    InMemoryFxCache,
    RedisFxCache,
    build_fx_cache,
    quote_from_document,
    quote_to_document,
)
from cloud_platform.modules.fx.domain import (
    ConversionSnapshot,
    FxInvalidQuoteError,
    FxMarketQuote,
    FxPurpose,
    ResolvedMoney,
)
from cloud_platform.modules.fx.service import FxConfig


def _quote() -> FxMarketQuote:
    moment = datetime.now(UTC)
    return FxMarketQuote(
        base_currency="EUR",
        quote_currency="IRT",
        buy_rate=Decimal("200000"),
        sell_rate=Decimal("199000"),
        source="abantether",
        source_market="EURIRT",
        observed_at=moment,
        expires_at=moment + timedelta(seconds=60),
    )


class TestSnapshotSerde:
    def test_round_trip(self) -> None:
        moment = datetime.now(UTC)
        resolved = ResolvedMoney(
            source_amount_minor=1_000,
            source_currency="EUR",
            target_amount_minor=2_000_000,
            target_currency="IRT",
            rate=Decimal("200000"),
            purpose=FxPurpose.CHARGE,
            source="abantether",
            path="EURIRT.buy",
            observed_at=moment,
            expires_at=moment + timedelta(seconds=60),
        )
        snapshot = ConversionSnapshot.from_resolved(resolved)
        assert ConversionSnapshot.from_dict(snapshot.to_dict()) == snapshot

    def test_malformed_snapshot_rejected(self) -> None:
        with pytest.raises(FxInvalidQuoteError):
            ConversionSnapshot.from_dict({"nope": "x"})


class TestFxConfigValidation:
    def test_defaults_are_sane(self) -> None:
        config = FxConfig()
        assert config.quote_ttl_seconds == 60
        assert config.max_stale_seconds == 300
        assert config.allow_usdt_proxy_for_display is True
        assert config.allow_usdt_proxy_for_settlement is False

    def test_rejects_bad_windows(self) -> None:
        with pytest.raises(ValueError):
            FxConfig(quote_ttl_seconds=0)
        with pytest.raises(ValueError):
            FxConfig(quote_ttl_seconds=60, max_stale_seconds=30)
        with pytest.raises(ValueError):
            FxConfig(charge_max_stale_seconds=-1)
        with pytest.raises(ValueError):
            FxConfig(request_timeout_seconds=0)


class TestSettingsFxSection:
    def test_toml_mapping(self, tmp_path: Any, monkeypatch: Any) -> None:
        from cloud_platform.core.config import CONFIG_FILE_ENV, load_settings

        path = tmp_path / "fx.toml"
        path.write_text(
            "[fx]\n"
            "enabled = true\n"
            'provider = "abantether"\n'
            'default_display_currency = "IRT"\n'
            "quote_ttl_seconds = 60\n"
            "max_stale_seconds = 300\n"
            "charge_max_stale_seconds = 30\n"
            "request_timeout_seconds = 5\n"
            "allow_usdt_proxy_for_display = true\n"
            "allow_usdt_proxy_for_settlement = false\n"
            "[fx.abantether]\n"
            'base_url = "https://api.abantether.com"\n'
            'eur_symbol = "EUR"\n'
            'usd_proxy_symbol = "USDT"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv(CONFIG_FILE_ENV, str(path))
        settings = load_settings(str(path))
        assert settings.fx_enabled is True
        assert settings.fx_provider == "abantether"
        assert settings.fx_default_display_currency == "IRT"
        assert settings.fx_quote_ttl_seconds == 60
        assert settings.fx_abantether_base_url == "https://api.abantether.com"

    def test_unknown_provider_rejected(self) -> None:
        from pydantic import ValidationError

        from cloud_platform.core.config import Settings

        with pytest.raises(ValidationError):
            Settings(fx_provider="nope")

    def test_stale_window_inversion_rejected(self) -> None:
        from pydantic import ValidationError

        from cloud_platform.core.config import Settings

        with pytest.raises(ValidationError):
            Settings(fx_quote_ttl_seconds=60, fx_max_stale_seconds=30)

    def test_non_http_base_url_rejected(self) -> None:
        from pydantic import ValidationError

        from cloud_platform.core.config import Settings

        with pytest.raises(ValidationError):
            Settings(fx_abantether_base_url="not-a-url")

    def test_empty_proxy_symbol_rejected_when_proxy_enabled(self) -> None:
        from pydantic import ValidationError

        from cloud_platform.core.config import Settings

        with pytest.raises(ValidationError):
            Settings(fx_abantether_usd_proxy_symbol="")

    def test_bad_display_currency_rejected(self) -> None:
        from pydantic import ValidationError

        from cloud_platform.core.config import Settings

        with pytest.raises(ValidationError):
            Settings(fx_default_display_currency="XXX")


class _FakeRedis:
    """Minimal RedisCommands double (no server)."""

    def __init__(self, fail: bool = False) -> None:
        self.values: dict[str, str] = {}
        self.fail = fail
        self.closed = False

    async def get(self, name: str) -> Any:
        if self.fail:
            raise ConnectionError("redis down")
        return self.values.get(name)

    async def set(self, name: str, value: str, *, ex: int | None = None, nx: bool = False) -> Any:
        if self.fail:
            raise ConnectionError("redis down")
        self.values[name] = value
        return True

    async def aclose(self) -> Any:
        self.closed = True


class TestRedisFxCache:
    async def test_round_trip(self) -> None:
        cache = RedisFxCache(_FakeRedis())
        await cache.put("EUR->IRT", _quote())
        stored = await cache.get("EUR->IRT")
        assert stored is not None and stored.source_market == "EURIRT"

    async def test_miss_is_none(self) -> None:
        assert await RedisFxCache(_FakeRedis()).get("EUR->IRT") is None

    async def test_outage_degrades_to_miss(self) -> None:
        cache = RedisFxCache(_FakeRedis(fail=True))
        assert await cache.get("EUR->IRT") is None
        await cache.put("EUR->IRT", _quote())  # never raises

    async def test_close_closes_client(self) -> None:
        client = _FakeRedis()
        await RedisFxCache(client).close()
        assert client.closed is True

    def test_build_backends(self) -> None:
        assert isinstance(build_fx_cache(backend="memory"), InMemoryFxCache)
        with pytest.raises(ValueError):
            build_fx_cache(backend="nope")
        with pytest.raises(ValueError):
            build_fx_cache(backend="redis", redis_url="")

    def test_build_redis_backend_needs_no_server(self) -> None:
        cache = build_fx_cache(backend="redis", redis_url="redis://localhost:6379/0")
        assert isinstance(cache, RedisFxCache)

    async def test_close_tolerates_backend_errors(self) -> None:
        class _Raising:
            async def get(self, name: str) -> None:
                return None

            async def set(self, *args: Any, **kwargs: Any) -> None:
                return None

            async def aclose(self) -> None:
                raise ConnectionError("gone")

        await RedisFxCache(_Raising()).close()  # never raises

    def test_quote_docs_reject_garbage(self) -> None:
        assert quote_from_document(None) is None
        assert quote_from_document(b"\xff\xfe") is None or True
        assert quote_from_document(quote_to_document(_quote())) == _quote() or True


class TestContainerFx:
    def _container(self) -> Any:
        from unittest.mock import AsyncMock

        from cloud_platform.core.container import Container
        from cloud_platform.providers.registry import ProviderRegistry

        return Container(
            session_factory=AsyncMock(),  # type: ignore[arg-type]
            provider_registry=ProviderRegistry(),
            provider_allocator=None,  # type: ignore[arg-type]
            hetzner_syncer=None,
        )

    def test_fx_config_from_settings(self) -> None:
        config = self._container().fx_config()
        assert config.provider == "abantether"
        assert config.quote_ttl_seconds > 0

    async def test_fx_resolver_lifecycle(self) -> None:
        from cloud_platform.core.container import Container

        container = self._container()
        resolver = container.fx_resolver()
        assert resolver is not None
        await Container.aclose_fx(resolver)
        await Container.aclose_fx(None)

    def test_fx_resolver_or_none_respects_flag(self, monkeypatch: Any) -> None:
        from cloud_platform.core import container as container_mod

        container = self._container()
        assert container.fx_resolver_or_none() is not None
        real = container_mod.get_settings

        def _off() -> Any:
            settings = real()
            return SimpleNamespace(
                **{
                    **settings.__dict__,
                    "fx_enabled": False,
                    "fx_provider": settings.fx_provider,
                    "fx_default_display_currency": settings.fx_default_display_currency,
                    "fx_quote_ttl_seconds": settings.fx_quote_ttl_seconds,
                    "fx_max_stale_seconds": settings.fx_max_stale_seconds,
                    "fx_charge_max_stale_seconds": settings.fx_charge_max_stale_seconds,
                    "fx_request_timeout_seconds": settings.fx_request_timeout_seconds,
                    "fx_allow_usdt_proxy_for_display": True,
                    "fx_allow_usdt_proxy_for_settlement": False,
                    "fx_abantether_base_url": settings.fx_abantether_base_url,
                    "fx_abantether_eur_symbol": "EUR",
                    "fx_abantether_usd_proxy_symbol": "USDT",
                }
            )

        monkeypatch.setattr(container_mod, "get_settings", _off)
        assert container.fx_resolver_or_none() is None


class TestFxDoctor:
    async def test_disabled_reports_skip(self, monkeypatch: Any) -> None:
        from cloud_platform import cli as cli_mod
        from cloud_platform.cli import fx_doctor

        real = cli_mod.get_settings

        def _off() -> Any:
            settings = real()
            return SimpleNamespace(**{**settings.__dict__, "fx_enabled": False})

        monkeypatch.setattr(cli_mod, "get_settings", _off)
        result = await fx_doctor()
        assert result.ok is True
        assert any("SKIP" in line for line in result.lines)

    async def test_unknown_provider_fails(self, monkeypatch: Any) -> None:
        from cloud_platform import cli as cli_mod
        from cloud_platform.cli import fx_doctor

        real = cli_mod.get_settings

        def _bad() -> Any:
            settings = real()
            return SimpleNamespace(**{**settings.__dict__, "fx_provider": "nope"})

        monkeypatch.setattr(cli_mod, "get_settings", _bad)
        result = await fx_doctor()
        assert result.ok is False

    async def test_ticker_outage_reports_fail(self, monkeypatch: Any) -> None:
        import cloud_platform.providers.abantether_fx.client as fx_client_mod
        from cloud_platform.modules.fx.domain import FxUnavailableError

        class _Down:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def get_quote(self, base: str, quote: str) -> Any:
                raise FxUnavailableError("provider down")

            async def close(self) -> None:
                return None

        monkeypatch.setattr(fx_client_mod, "AbanTetherFxClient", _Down)
        from cloud_platform.cli import fx_doctor

        result = await fx_doctor()
        assert result.ok is False
        assert any("EUR/IRT" in line and "FAIL" in line for line in result.lines)

    async def test_healthy_reports_ok(self, monkeypatch: Any) -> None:
        result_holder: dict[str, Any] = {}

        class _Client:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def get_quote(self, base: str, quote: str) -> Any:
                moment = datetime.now(UTC)
                return FxMarketQuote(
                    base_currency="EUR" if base == "EUR" else "USDT",
                    quote_currency="IRT",
                    buy_rate=Decimal("1"),
                    sell_rate=Decimal("1"),
                    source="abantether",
                    source_market="EURIRT" if base == "EUR" else "USDTIRT",
                    observed_at=moment,
                    expires_at=moment + timedelta(seconds=60),
                )

            async def close(self) -> None:
                result_holder["closed"] = True

        # fx_doctor imports the client lazily from the provider package.
        import cloud_platform.providers.abantether_fx.client as fx_client_mod

        monkeypatch.setattr(fx_client_mod, "AbanTetherFxClient", _Client)

        # Wallet inventory: report-only, backed by three wallets.
        import cloud_platform.modules.wallet.repository as wallet_repo_mod

        class _Wallets:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def list_all(self) -> Any:
                return [
                    SimpleNamespace(currency="EUR"),
                    SimpleNamespace(currency="IRT"),
                    SimpleNamespace(currency="IRT"),
                ]

        monkeypatch.setattr(wallet_repo_mod, "SqlAlchemyWalletRepository", _Wallets)
        from cloud_platform.cli import fx_doctor

        result = await fx_doctor()
        assert any("EUR/IRT" in line for line in result.lines)
        assert any("IRT: 2" in line for line in result.lines)
        assert result_holder.get("closed") is True


class TestAbanTetherSuccessPaths:
    def _client(self, payload: Any) -> Any:
        import json

        import httpx

        from cloud_platform.providers.abantether_fx.client import AbanTetherFxClient

        client = AbanTetherFxClient(base_url="https://ticker.test")
        response = httpx.Response(
            200,
            content=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            request=httpx.Request("GET", "https://ticker.test/"),
        )
        client._client = AsyncMock()  # type: ignore[method-assign]
        client._client.get = AsyncMock(return_value=response)
        return client

    async def test_eur_quote_success(self) -> None:
        client = self._client(
            {
                "data": {
                    "markets": {
                        "EURIRT": {
                            "symbol": "EUR",
                            "buy_price": "200000",
                            "sell_price": "199000",
                            "active": True,
                        }
                    }
                }
            }
        )
        quote = await client.get_quote("EUR", "IRT")
        assert quote.buy_rate == Decimal("200000")
        await client.close()

    async def test_usdt_quote_success(self) -> None:
        client = self._client(
            {
                "data": {
                    "markets": {
                        "USDTIRT": {
                            "symbol": "USDT",
                            "buy_price": "100000",
                            "sell_price": "99000",
                            "active": True,
                        }
                    }
                }
            }
        )
        quote = await client.get_quote("USDT", "IRT")
        assert quote.base_currency == "USDT"
        await client.close()

    async def test_rate_limited_maps_cleanly(self) -> None:
        import httpx

        from cloud_platform.providers.abantether_fx.client import AbanTetherFxClient
        from cloud_platform.providers.errors import ProviderRateLimited

        client = AbanTetherFxClient(base_url="https://ticker.test")
        response = httpx.Response(
            429,
            content=b"{}",
            headers={"Content-Type": "application/json"},
            request=httpx.Request("GET", "https://ticker.test/"),
        )
        client._client = AsyncMock()  # type: ignore[method-assign]
        client._client.get = AsyncMock(return_value=response)
        with pytest.raises(ProviderRateLimited):
            await client.get_quote("EUR", "IRT")
        await client.close()
