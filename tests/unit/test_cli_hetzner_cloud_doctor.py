"""Read-only Hetzner hourly diagnostics keep monthly VPS inventory separate."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

import cloud_platform.cli as cli
from cloud_platform.modules.offers.domain import PricingPolicy
from cloud_platform.providers.errors import ProviderAuthError
from tests.unit.test_offers_readiness import _canonical_offer


class _Repo:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    async def list_all(self) -> list[Any]:
        return self.rows


class _HourlyProvider:
    reads: ClassVar[list[str]] = []

    def __init__(self, *, token: str, base_url: str) -> None:
        assert token and base_url

    async def read_locations(self) -> tuple[Any, ...]:
        self.reads.append("GET /locations")
        return (SimpleNamespace(id="fsn1"),)

    async def read_instance_types(self, location: str) -> Any:
        self.reads.append("GET /server_types")
        return SimpleNamespace(
            plans=(SimpleNamespace(plan_id="cpx22", architecture="x86"),),
            rejected=(SimpleNamespace(reason="unavailable-at-location"),),
        )

    async def list_images(self, location: str) -> list[Any]:
        self.reads.append("GET /images")
        return [SimpleNamespace(id="1", architecture="x86")]

    async def aclose(self) -> None:
        self.reads.append("close")


def _patch_doctor(
    monkeypatch: pytest.MonkeyPatch, provider: type[_HourlyProvider], rows: list[Any]
) -> None:
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(
            providers_enabled={"hetzner": True},
            provider_families={
                "hetzner": {
                    "vps": {"billing_model": "prepaid_monthly_fixed"},
                    "cloud": {"billing_model": "hourly"},
                }
            },
            hetzner_api_token="not-a-real-token",
            hetzner_api_base_url="https://api.hetzner.cloud/v1",
            fx_catalog_pricing_currency="USD",
        ),
    )
    monkeypatch.setattr(
        "cloud_platform.providers.hetzner.hourly.HetznerHourlyCloudProvider", provider
    )
    monkeypatch.setattr(
        "cloud_platform.modules.offers.repository.SqlAlchemySellableOfferRepository",
        lambda *args: _Repo(rows),
    )
    monkeypatch.setattr(
        "cloud_platform.modules.offers.repository.SqlAlchemyCatalogSyncStateRepository",
        lambda *args: _Repo([]),
    )
    monkeypatch.setattr(
        "cloud_platform.modules.offers.auto_sync.pricing_policies_from_settings",
        lambda settings: {
            "hetzner.hourly": PricingPolicy(mode="markup", markup_percent=25, auto_publish=True)
        },
    )


@pytest.mark.asyncio
async def test_hourly_doctor_does_not_count_monthly_offer_as_hourly_sellable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monthly = replace(
        await _canonical_offer(provider_key="hetzner", product_id="cx22"),
        billing_model="prepaid_monthly_fixed",
    )
    _HourlyProvider.reads = []
    _patch_doctor(monkeypatch, _HourlyProvider, [monthly])

    result = await cli.hetzner_cloud_doctor()

    assert not result.ok
    assert "hourly offers stored: 0" in result.lines
    assert any("sellable=0" in line for line in result.lines)
    assert _HourlyProvider.reads == [
        "GET /locations",
        "GET /server_types",
        "GET /images",
        "close",
    ]


@pytest.mark.asyncio
async def test_hourly_doctor_requires_compatible_image_even_with_priced_offer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hourly = await _canonical_offer(
        provider_key="hetzner",
        product_id="cpx22",
        billing_model="hourly",
        billing_parameters={"provider_hourly_rate": "4.49"},
    )

    class _Incompatible(_HourlyProvider):
        async def list_images(self, location: str) -> list[Any]:
            self.reads.append("GET /images")
            return [SimpleNamespace(id="2", architecture="arm64")]

    _Incompatible.reads = []
    _patch_doctor(monkeypatch, _Incompatible, [hourly])

    result = await cli.hetzner_cloud_doctor()

    assert not result.ok
    assert any("types with compatible images=0" in line for line in result.lines)
    assert any("hourly sellable in USD: 1" in line for line in result.lines)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (
            RuntimeError("never print this private diagnostic"),
            "catalog unreadable (RuntimeError)",
        ),
        (
            ProviderAuthError("never print this private diagnostic"),
            "rejected the configured token",
        ),
    ],
)
async def test_hourly_doctor_reports_unknown_live_reads_without_retiring_offers(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, expected: str
) -> None:
    class _Unavailable(_HourlyProvider):
        async def read_instance_types(self, location: str) -> Any:
            raise failure

    _Unavailable.reads = []
    _patch_doctor(monkeypatch, _Unavailable, [])

    result = await cli.hetzner_cloud_doctor()

    assert not result.ok
    assert any(expected in line for line in result.lines)
    assert "private diagnostic" not in "\n".join(result.lines)
    assert _Unavailable.reads == ["GET /locations", "close"]
