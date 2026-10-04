"""Read-only, account-bound Hetzner diagnostics with official API envelopes."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import httpx
import pytest

import cloud_platform.cli as cli
from cloud_platform.core.config import HetznerAccountSettings, Settings
from cloud_platform.modules.offers.domain import PricingPolicy
from tests.unit.test_offers_readiness import _canonical_offer


class _Repo:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    async def list_all(self) -> list[Any]:
        return self.rows


def _envelope(key: str, values: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        key: values,
        "meta": {
            "pagination": {
                "page": 1,
                "next_page": None,
                "last_page": 1,
                "total_entries": len(values),
            }
        },
    }


def _patch_doctor(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[Any],
    *,
    image_architecture: str = "x86",
    catalog_status: int = 200,
    broken_inventory: bool = False,
    accounts: list[HetznerAccountSettings] | None = None,
    legacy_token: str = "",
) -> tuple[Settings, list[httpx.Request]]:
    settings = Settings(
        leaseweb_api_key="",
        leaseweb_accounts=[],
        _env_file=None,
        providers_enabled={"hetzner": True},
        provider_families={
            "hetzner": {
                "vps": {"billing_model": "prepaid_monthly_fixed"},
                "cloud": {"billing_model": "hourly"},
            }
        },
        hetzner_api_token=legacy_token,
        hetzner_accounts=accounts
        if accounts is not None
        else [
            HetznerAccountSettings(id="main", api_token="main-secret", server_limit=5),
            HetznerAccountSettings(id="next", api_token="next-secret", priority=200),
        ],
        hetzner_api_base_url="https://api.test/v1",
        fx_catalog_pricing_currency="USD",
    )
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET", "doctor must never mutate provider resources"
        token = request.headers["authorization"]
        assert token != "Bearer unused-legacy-secret"
        key = request.url.path.rsplit("/", 1)[-1]
        if key == "pricing":
            return httpx.Response(200, json={"pricing": {"currency": "EUR"}})
        if key == "servers":
            if broken_inventory:
                return httpx.Response(200, json={"servers": []})
            values = (
                [
                    {"id": 1, "status": "off", "labels": {}},
                    {"id": 2, "status": "running", "name": "manual", "labels": {}},
                ]
                if token == "Bearer main-secret"
                else [{"id": 3, "status": "off"}]
            )
        elif key == "locations":
            values = [{"id": 1, "name": "fsn1", "country": "DE", "city": "Falkenstein"}]
        elif key == "server_types":
            if catalog_status != 200:
                return httpx.Response(
                    catalog_status,
                    json={
                        "error": {
                            "code": "unauthorized" if catalog_status == 401 else "not_found",
                            "message": "never print this private diagnostic",
                        }
                    },
                )
            values = [
                {
                    "id": 1,
                    "name": "cpx22",
                    "architecture": "x86",
                    "cores": 2,
                    "memory": 4,
                    "disk": 40,
                    "storage_type": "local",
                    "locations": [{"name": "fsn1", "available": True}],
                    "prices": [
                        {
                            "location": "fsn1",
                            "price_hourly": {"gross": "0.0088"},
                            "price_monthly": {"gross": "5.49"},
                        }
                    ],
                }
            ]
        elif key == "images":
            values = [
                {
                    "id": 1,
                    "name": "ubuntu-24.04",
                    "type": "system",
                    "status": "available",
                    "architecture": image_architecture,
                }
            ]
        else:
            raise AssertionError(f"unexpected doctor endpoint: {request.url.path}")
        return httpx.Response(200, json=_envelope(key, values))

    client = httpx.AsyncClient
    monkeypatch.setattr(
        "cloud_platform.providers.hetzner.client.httpx.AsyncClient",
        lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(handle)),
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr("cloud_platform.core.config.get_settings", lambda: settings)
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
            "hetzner.hourly": PricingPolicy(
                mode="markup",
                markup_percent=25,
                auto_publish=True,
            )
        },
    )
    return settings, requests


@pytest.mark.asyncio
async def test_hourly_doctor_does_not_count_monthly_offer_as_hourly_sellable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monthly = replace(
        await _canonical_offer(provider_key="hetzner", product_id="cx22"),
        billing_model="prepaid_monthly_fixed",
    )
    _, requests = _patch_doctor(monkeypatch, [monthly])

    result = await cli.hetzner_cloud_doctor()

    assert not result.ok
    assert any("types with compatible images=1" in line for line in result.lines)
    assert "hourly offers stored: 0" in result.lines
    assert any("sellable=0" in line for line in result.lines)
    assert {request.headers["authorization"] for request in requests} == {
        "Bearer main-secret",
        "Bearer next-secret",
    }
    text = "\n".join(result.lines)
    assert "Project server usage: 2; operator-configured server ceiling: 5" in text
    assert "Project server usage: 1; operator-configured server ceiling: unknown" in text
    assert "secret" not in text


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
    _patch_doctor(monkeypatch, [hourly], image_architecture="arm64")

    result = await cli.hetzner_cloud_doctor()

    assert not result.ok
    assert any("types with compatible images=0" in line for line in result.lines)
    assert any("hourly sellable in USD: 1" in line for line in result.lines)


@pytest.mark.asyncio
@pytest.mark.parametrize("catalog_status", [401, 404])
async def test_hourly_doctor_reports_unknown_live_reads_without_retiring_offers(
    monkeypatch: pytest.MonkeyPatch,
    catalog_status: int,
) -> None:
    _patch_doctor(monkeypatch, [], catalog_status=catalog_status)

    result = await cli.hetzner_cloud_doctor()

    assert not result.ok
    text = "\n".join(result.lines)
    assert "private diagnostic" not in text
    assert "secret" not in text
    expected = "rejected the configured token" if catalog_status == 401 else "catalog unreadable"
    assert expected in text


@pytest.mark.asyncio
async def test_incomplete_project_inventory_is_unknown_not_zero_free_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_doctor(monkeypatch, [], broken_inventory=True)

    result = await cli.hetzner_cloud_doctor()

    assert not result.ok
    text = "\n".join(result.lines)
    assert "Project server usage: unknown" in text
    assert "Project server usage: 0" not in text
    assert "ceiling reached" not in text


@pytest.mark.asyncio
async def test_monthly_doctor_reads_draining_project_without_substituting_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, requests = _patch_doctor(
        monkeypatch,
        [],
        accounts=[
            HetznerAccountSettings(id="old-owner", api_token="next-secret", state="draining"),
        ],
    )

    result = await cli.hetzner_doctor()

    assert not result.ok
    assert requests
    assert all(request.headers["authorization"] == "Bearer next-secret" for request in requests)
    text = "\n".join(result.lines)
    assert "account old-owner: state=draining" in text
    assert "existing ownership remains manageable" in text
    assert "no active credential account" in text
    assert "secret" not in text


@pytest.mark.asyncio
async def test_explicit_empty_accounts_do_not_probe_legacy_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, requests = _patch_doctor(
        monkeypatch,
        [],
        accounts=[],
        legacy_token="unused-legacy-secret",
    )

    result = await cli.hetzner_cloud_doctor()

    assert not result.ok
    assert not requests
    assert any("no managed Hetzner credentials" in line for line in result.lines)


@pytest.mark.asyncio
async def test_account_only_sync_publishes_one_offer_through_project_with_headroom(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cloud_platform.providers.hetzner import sync

    settings, requests = _patch_doctor(monkeypatch, [])
    settings.hetzner_accounts[0].server_limit = 2
    published: dict[tuple[str, str, str], Any] = {}
    observed_routes: list[Any] = []

    class _Offers:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def upsert_from_provider(
            self,
            *,
            provider_key: str,
            product_id: str,
            location_id: str,
            update: Any,
            **kwargs: Any,
        ) -> None:
            published[(provider_key, product_id, location_id)] = update

        async def mark_unavailable(self, *args: Any, **kwargs: Any) -> int:
            return 0

    class _Routes:
        def __init__(self, *args: Any) -> None:
            pass

        async def list_for_provider(self, provider_key: str) -> list[Any]:
            return []

        async def upsert_observations(
            self,
            *,
            provider_key: str,
            observations: Any,
            **kwargs: Any,
        ) -> None:
            observed_routes.extend(observations)

    async def readiness(provider_key: str) -> None:
        pass

    monkeypatch.setattr(sync, "SqlAlchemySellableOfferRepository", _Offers)
    monkeypatch.setattr(
        "cloud_platform.modules.provider_routes.repository.SqlAlchemyProviderRouteRepository",
        _Routes,
    )
    monkeypatch.setattr(cli, "_print_storefront_readiness", readiness)

    assert await cli.hetzner_sync_offers() == 0

    assert len(published) == 1
    assert next(iter(published.values())).provider_account_id == "next"
    assert {route.credential_account_id for route in observed_routes} == {"main", "next"}
    assert {request.headers["authorization"] for request in requests} == {
        "Bearer main-secret",
        "Bearer next-secret",
    }
    assert "secret" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_catalog_status_recognizes_accounts_without_legacy_token(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, requests = _patch_doctor(monkeypatch, [])

    assert await cli.catalog_auto_sync_doctor() == 0

    text = capsys.readouterr().out
    section = text.split("hetzner:\n", 1)[1].split("\n\n", 1)[0]
    assert "credential configured: yes" in section
    assert not requests
    assert "secret" not in text
