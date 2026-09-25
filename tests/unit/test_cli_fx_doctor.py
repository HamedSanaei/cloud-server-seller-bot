"""Operator-surface CLI tests: fx doctor/rates + leaseweb cloud catalog/preview.

Hermetic by construction: repository classes are patched with lambdas
returning AsyncMock repos, the hourly provider is patched via
``cli._hourly_cloud_provider_or_error``, settings come from in-memory
namespaces (conftest already isolates ``configuration.toml``). No database,
no network, no redis.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import cloud_platform.cli as cli


def _fake_repo_class(repo: Any) -> Any:
    return lambda *a, **k: repo


def _fx_settings(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "fx_enabled": True,
        "fx_domestic_enabled": True,
        "fx_global_enabled": True,
        "fx_provider": "abantether",
        "fx_domestic_provider": None,
        "fx_abantether_base_url": "https://ticker.test",
        "fx_request_timeout_seconds": 5.0,
        "fx_quote_ttl_seconds": 60,
        "fx_abantether_eur_symbol": "EUR",
        "fx_abantether_usd_proxy_symbol": "USDT",
        "fx_allow_usdt_proxy_for_display": False,
        "fx_allow_usdt_proxy_for_settlement": False,
        "fx_catalog_pricing_currency": "USD",
        "fx_global_fiat_provider": "frankfurter",
        "app_env": "test",
        "redis_url": "redis://localhost:6379/0",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _market_quote(base: str = "EUR") -> SimpleNamespace:
    moment = datetime.now(UTC)
    return SimpleNamespace(
        base_currency=base,
        quote_currency="IRT",
        source_market=f"{base}IRT",
        observed_at=moment,
        expires_at=moment,
    )


def _reference_resolution(base: str, target: str, rate: str = "1.17") -> SimpleNamespace:
    return SimpleNamespace(
        rate=Decimal(rate),
        quote=SimpleNamespace(provider_date=datetime.now(UTC).date()),
        source="frankfurter",
        stale=False,
    )


@pytest.fixture(autouse=True)
def _quiet(capsys: pytest.CaptureFixture[str]) -> Any:
    yield
    capsys.readouterr()


class TestFxDoctor:
    async def test_without_configuration_reports_skips(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            cli,
            "get_settings",
            lambda: _fx_settings(
                fx_enabled=False, fx_domestic_enabled=False, fx_global_enabled=False
            ),
        )
        monkeypatch.setattr(
            "cloud_platform.modules.fx.cache.build_fx_cache",
            lambda **k: __import__(
                "cloud_platform.modules.fx.cache", fromlist=["InMemoryFxCache"]
            ).InMemoryFxCache(),
        )
        repo = AsyncMock(list_all=AsyncMock(return_value=[]))
        monkeypatch.setattr(
            "cloud_platform.modules.wallet.repository.SqlAlchemyWalletRepository",
            _fake_repo_class(repo),
        )
        result = await cli.fx_doctor()
        assert result.ok is True
        assert any("SKIP" in line for line in result.lines)
        assert any("FX cache" in line for line in result.lines)

    async def test_healthy_reports_ok_and_closes_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "get_settings", lambda: _fx_settings())
        closed: dict[str, bool] = {}

        class _Client:
            def __init__(self, *a: Any, **k: Any) -> None:
                pass

            async def get_quote(self, base: str, quote: str) -> Any:
                return _market_quote(base)

            async def close(self) -> None:
                closed["ok"] = True

        monkeypatch.setattr(
            "cloud_platform.providers.abantether_fx.client.AbanTetherFxClient",
            _Client,
        )
        fake_resolver = AsyncMock()
        fake_resolver.get_rate = AsyncMock(side_effect=lambda b, t: _reference_resolution(b, t))
        container = MagicMock(
            global_fx_resolver_or_none=MagicMock(return_value=fake_resolver),
            close=AsyncMock(),
        )
        monkeypatch.setattr("cloud_platform.core.container.create_container", lambda: container)
        monkeypatch.setattr("cloud_platform.core.container.Container.aclose_fx", AsyncMock())
        repo = AsyncMock(
            list_all=AsyncMock(
                return_value=[
                    SimpleNamespace(currency="USD"),
                    SimpleNamespace(currency="IRT"),
                ]
            )
        )
        monkeypatch.setattr(
            "cloud_platform.modules.wallet.repository.SqlAlchemyWalletRepository",
            _fake_repo_class(repo),
        )
        result = await cli.fx_doctor()
        assert any("EUR/IRT" in line and "OK" in line for line in result.lines)
        assert any("Wallet currencies" in line for line in result.lines)
        assert closed.get("ok") is True

    async def test_eur_outage_reports_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "get_settings", lambda: _fx_settings())

        class _Down:
            def __init__(self, *a: Any, **k: Any) -> None:
                pass

            async def get_quote(self, base: str, quote: str) -> Any:
                raise RuntimeError("ticker down")

            async def close(self) -> None:
                return None

        monkeypatch.setattr(
            "cloud_platform.providers.abantether_fx.client.AbanTetherFxClient",
            _Down,
        )
        repo = AsyncMock(list_all=AsyncMock(return_value=[]))
        monkeypatch.setattr(
            "cloud_platform.modules.wallet.repository.SqlAlchemyWalletRepository",
            _fake_repo_class(repo),
        )
        result = await cli.fx_doctor()
        assert result.ok is False
        assert any("EUR/IRT" in line and "FAIL" in line for line in result.lines)


class TestFxRates:
    async def test_disabled_returns_one(self, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
        monkeypatch.setattr(cli, "get_settings", lambda: _fx_settings(fx_enabled=False))
        assert await cli.fx_rates(None) == 1
        assert "disabled" in capsys.readouterr().out

    async def test_target_mismatch_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        monkeypatch.setattr(cli, "get_settings", lambda: _fx_settings())
        assert await cli.fx_rates("EUR") == 2
        assert "refused" in capsys.readouterr().out

    async def test_table_rendering_success(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        monkeypatch.setattr(cli, "get_settings", lambda: _fx_settings())
        resolver = AsyncMock()
        resolver.get_rate = AsyncMock(side_effect=lambda b, t: _reference_resolution(b, t))
        container = MagicMock(
            global_fx_resolver_or_none=MagicMock(return_value=resolver),
            close=AsyncMock(),
        )
        monkeypatch.setattr("cloud_platform.core.container.create_container", lambda: container)
        monkeypatch.setattr("cloud_platform.core.container.Container.aclose_fx", AsyncMock())
        assert await cli.fx_rates(None) == 0
        out = capsys.readouterr().out
        assert "EUR/USD: rate=1.17" in out
        assert "source=frankfurter" in out

    async def test_per_base_failure_returns_one(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        monkeypatch.setattr(cli, "get_settings", lambda: _fx_settings())

        async def _flaky(base: str, target: str) -> Any:
            if base == "EUR":
                raise RuntimeError("frankfurter 500")
            return _reference_resolution(base, target)

        resolver = AsyncMock()
        resolver.get_rate = AsyncMock(side_effect=_flaky)
        container = MagicMock(
            global_fx_resolver_or_none=MagicMock(return_value=resolver),
            close=AsyncMock(),
        )
        monkeypatch.setattr("cloud_platform.core.container.create_container", lambda: container)
        monkeypatch.setattr("cloud_platform.core.container.Container.aclose_fx", AsyncMock())
        assert await cli.fx_rates(None) == 1
        assert "EUR/USD: error" in capsys.readouterr().out


class TestLeasewebCloudCatalog:
    def _provider(self, regions: list[Any]) -> Any:
        provider = SimpleNamespace(
            list_regions=AsyncMock(return_value=list(regions)),
            list_instance_types=AsyncMock(return_value=[]),
            list_images=AsyncMock(return_value=[]),
            close=AsyncMock(),
        )
        return provider

    async def test_region_filter_only_prints_wanted(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        regions = [
            SimpleNamespace(id="eu-central-1", country_code="DE", name="Frankfurt"),
            SimpleNamespace(id="eu-west-1", country_code="NL", name="Amsterdam"),
        ]
        monkeypatch.setattr(
            cli, "_hourly_cloud_provider_or_error", AsyncMock(return_value=self._provider(regions))
        )
        repo = AsyncMock(list_all=AsyncMock(return_value=[]))
        monkeypatch.setattr(
            "cloud_platform.modules.offers.repository.SqlAlchemySellableOfferRepository",
            _fake_repo_class(repo),
        )
        assert await cli.leaseweb_cloud_catalog("eu-central-1") == 0
        out = capsys.readouterr().out
        assert "eu-central-1" in out
        assert "eu-west-1" not in out

    async def test_unknown_region_returns_one(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        regions = [SimpleNamespace(id="eu-central-1", country_code="DE", name="X")]
        monkeypatch.setattr(
            cli, "_hourly_cloud_provider_or_error", AsyncMock(return_value=self._provider(regions))
        )
        assert await cli.leaseweb_cloud_catalog("no-such-region") == 1
        assert "unknown region" in capsys.readouterr().out


class TestLeasewebCloudCreatePreview:
    def _provider(self, *, types: list[Any], images: list[Any]) -> Any:
        return SimpleNamespace(
            list_instance_types=AsyncMock(return_value=list(types)),
            list_images=AsyncMock(return_value=list(images)),
            close=AsyncMock(),
        )

    def _facts(self) -> tuple[Any, Any]:
        from cloud_platform.providers.leaseweb.cloud import CloudImage, CloudInstanceType

        return (
            CloudInstanceType(
                id="lsw.m3.large",
                name="lsw.m3.large",
                region="eu-central-1",
                family_key="general",
                family_name="General Purpose",
                vcpu=2,
                ram_gb=7,
                disk_gb=5,
                traffic=None,
                hourly_cost_minor=4,
                currency="EUR",
                storage_type="CENTRAL",
                storage_types=("CENTRAL", "LOCAL"),
                hourly_rate_exact="0.0442",
            ),
            CloudImage(
                id="UBUNTU_26_04_64BIT",
                label="Ubuntu 26.04 LTS (x86_64)",
                os_family="linux",
                architecture="x86_64",
                min_disk_size_gb=5,
                storage_types=("LOCAL", "CENTRAL"),
            ),
        )

    async def test_body_printed_and_no_post(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        post_calls: list[tuple[Any, Any]] = []

        async def _no_post(self: Any, *a: Any, **k: Any) -> Any:
            post_calls.append((a, k))
            raise AssertionError("preview must not POST")

        monkeypatch.setattr(
            "cloud_platform.providers.leaseweb.transport.LeasewebTransport.request",
            _no_post,
        )
        monkeypatch.setattr(
            "cloud_platform.providers.leaseweb.transport.LeasewebTransport.request_raw",
            _no_post,
        )
        match, image = self._facts()
        monkeypatch.setattr(
            cli,
            "_hourly_cloud_provider_or_error",
            AsyncMock(return_value=self._provider(types=[match], images=[image])),
        )
        rc = await cli.leaseweb_cloud_create_preview(
            "eu-central-1", "lsw.m3.large", "UBUNTU_26_04_64BIT", "preview-only"
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "POST /publicCloud/v1/instances (NOT SENT" in out
        assert "no provider mutation occurred" in out
        assert post_calls == []
        # The launch root disk is derived from the live provider facts and both
        # required fields appear; no undocumented field is sent.
        import json

        body = json.loads(out.split("preview only):", 1)[1].split("no provider mutation")[0])
        assert body == {
            "type": "lsw.m3.large",
            "imageId": "UBUNTU_26_04_64BIT",
            "region": "eu-central-1",
            "reference": "preview-only",
            "contractType": "HOURLY",
            "rootDiskSize": 5,
            "rootDiskStorageType": "CENTRAL",
        }
        assert "labels" not in body

    async def test_an_explicit_root_disk_is_honoured(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        match, image = self._facts()
        monkeypatch.setattr(
            cli,
            "_hourly_cloud_provider_or_error",
            AsyncMock(return_value=self._provider(types=[match], images=[image])),
        )
        rc = await cli.leaseweb_cloud_create_preview(
            "eu-central-1", "lsw.m3.large", "UBUNTU_26_04_64BIT", "ref", 50, "local"
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert '"rootDiskSize": 50' in out
        assert '"rootDiskStorageType": "LOCAL"' in out

    async def test_an_unknown_type_or_image_returns_one(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        match, image = self._facts()
        monkeypatch.setattr(
            cli,
            "_hourly_cloud_provider_or_error",
            AsyncMock(return_value=self._provider(types=[match], images=[image])),
        )
        assert await cli.leaseweb_cloud_create_preview("eu-central-1", "lsw.nope", "x", "r") == 1
        assert "not offered" in capsys.readouterr().out
        assert (
            await cli.leaseweb_cloud_create_preview(
                "eu-central-1", "lsw.m3.large", "NO_SUCH_IMAGE", "r"
            )
            == 1
        )
        assert "not offered" in capsys.readouterr().out
