"""Tests for the READ-ONLY Leaseweb operator diagnostics (CLI surface).

These commands are the only way an operator touches the provider by hand, so
the suite proves three things:

1. every command performs documented READ-ONLY calls (``respx`` mocks the HTTP
   boundary — nothing can reach Leaseweb);
2. the API key comes from configuration, never from the code, and never
   appears in the printed output;
3. no command exposes a billable or destructive operation (ordering a VPS,
   reinstall, password reset, credential/snapshot mutation, power actions).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

import cloud_platform.cli as cli
from cloud_platform.providers.leaseweb import diagnostics
from cloud_platform.providers.leaseweb.errors import LeasewebValidationError

KEY = "test-leaseweb-key"
BASE = "https://api.test"
VPS = "VPS02_1"
ORDER_ID = "LS-ORD-1"
SNAPSHOT_ID = "3a042956-0689-45dc-8322-8b8325464182"


def _settings(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "leaseweb_api_key": KEY,
        "leaseweb_api_base_url": BASE,
        "leaseweb_locations": "AMS-01, FRA-01",
        "leaseweb_timeout_seconds": 5.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    resolved = _settings()
    monkeypatch.setattr(diagnostics, "_settings", lambda: resolved)
    return resolved


IP_PAYLOAD = {
    "ip": "1.2.3.4",
    "prefixLength": "32",
    "version": 4,
    "nullRouted": False,
    "reverseLookup": "host.example.com",
    "mainIp": True,
    "networkType": "PUBLIC",
}

VPS_DETAIL = {
    "id": VPS,
    "pack": "Leaseweb VPS 2",
    "region": "eu-west-3",
    "datacenter": "AMS-01",
    "reference": "srv-1",
    "image": {
        "id": "UBUNTU_22_04_64BIT",
        "name": "Ubuntu 22.04 LTS",
        "family": "linux",
        "flavour": "ubuntu",
        "custom": False,
    },
    "state": "RUNNING",
    "hasPublicIpV4": True,
    "rootDiskSize": 50,
    "startedAt": "2023-11-30T16:31:28+00:00",
    "ips": [IP_PAYLOAD],
    "iso": {"id": "GRML", "name": "GRML Rescue"},
    "resources": {
        "cpu": {"value": 2, "unit": "vCPU"},
        "memory": {"value": 4, "unit": "GiB"},
        "publicNetworkSpeed": {"value": 10, "unit": "Gbps"},
    },
    "contract": {
        "id": "41228459000100",
        "type": "MONTHLY",
        "state": "ACTIVE",
        "term": 12,
        "billingFrequency": 1,
        "sla": "Basic",
        "inModification": False,
    },
}

PRODUCT_DETAIL = {
    "id": "VPS02_1",
    "name": "VPS 2.1",
    "vCpu": "2",
    "vRam": "4",
    "nvmeStorage": "100 GB",
    "traffic": "20 TB",
    "location": ["AMS-01"],
    "price": {
        "currency": "EUR",
        "basePrice": "9.99",
        "setupFee": "0.00",
        "total": "9.99",
        "contractTerm": "1_MONTH",
        "billingCycle": "1_MONTH",
        "contractTerms": [{"key": "1_MONTH", "total": "9.99"}],
        "billingCycles": [{"key": "1_MONTH", "total": "9.99"}],
    },
    "configurationOptions": {
        "diskUpgrade": [],
        "operatingSystem": [
            {"name": "Ubuntu 22.04", "selected": True, "price": "0.00", "currency": "EUR"}
        ],
        "controlPanel": [{"name": "Plesk", "selected": False, "price": "7.50", "currency": "EUR"}],
        "serviceLevelAgreement": [],
    },
}

METRICS_PAYLOAD = {
    "metrics": {"downPublic": {"values": [{"value": 1024}], "unit": "B"}},
    "_metadata": {
        "from": "2024-01-01T00:00:00Z",
        "to": "2024-01-02T00:00:00Z",
        "granularity": "DAY",
        "aggregation": "SUM",
        "unit": "B",
        "summary": {"downPublic": {"average": 512, "expected": 1024, "total": 1024}},
    },
}


class TestTransportFromConfiguration:
    """The API key lives in configuration; nothing here hard-codes a secret."""

    def test_transport_uses_configured_values(self) -> None:
        transport = diagnostics.build_transport(_settings())
        assert transport.base_url == BASE
        assert transport.client.headers["X-LSW-Auth"] == KEY

    def test_missing_key_is_refused_before_any_request(self) -> None:
        for value in ("", "   ", "CHANGE_ME", None):
            with pytest.raises(LeasewebValidationError):
                diagnostics.build_transport(_settings(leaseweb_api_key=value))

    def test_api_builders_share_the_configuration_source(self) -> None:
        assert diagnostics.build_ordering_api(_settings()).transport.base_url == BASE
        assert diagnostics.build_orders_api(_settings()).transport.base_url == BASE
        assert diagnostics.build_vps_api(_settings()).transport.base_url == BASE


class TestAuthCheck:
    """``leaseweb auth-check`` — one read-only catalogue call."""

    @respx.mock
    async def test_success_reports_the_catalogue_call(self, settings: Any) -> None:
        route = respx.get(f"{BASE}/ordering/v1/products/vps").mock(
            return_value=httpx.Response(
                200,
                json={
                    "vpss": [{"id": "VPS02_1", "price": {"total": "9.99", "currency": "EUR"}}],
                    "_metadata": {"totalCount": 1, "offset": 0, "limit": 1},
                },
            )
        )
        lines = await diagnostics.auth_check_lines()
        assert route.calls.last.request.method == "GET"
        assert "OK" in lines[-2]
        assert "1 product row(s)" in lines[-2]

    async def test_missing_key_reports_failure_without_a_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No ``respx`` mock is installed here on purpose: with an unconfigured
        # key this command must not attempt any HTTP call at all.
        monkeypatch.setattr(
            diagnostics,
            "_settings",
            lambda: _settings(leaseweb_api_key=""),
        )
        lines = await diagnostics.auth_check_lines()
        assert len(lines) == 1 and "FAIL" in lines[0]

    @respx.mock
    async def test_rejected_key_reports_the_safe_provider_summary(self, settings: Any) -> None:
        respx.get(f"{BASE}/ordering/v1/products/vps").mock(
            return_value=httpx.Response(
                401,
                json={"errorMessage": f"bad key {KEY}", "errorCode": "AUTH"},
            )
        )
        lines = await diagnostics.auth_check_lines()
        assert "FAIL" in lines[-1]
        assert KEY not in "\n".join(lines)

    @respx.mock
    async def test_authentication_uses_a_read_only_endpoint(self, settings: Any) -> None:
        route = respx.get(f"{BASE}/ordering/v1/products/vps").mock(
            return_value=httpx.Response(200, json={"vpss": [], "_metadata": {}})
        )
        await diagnostics.auth_check_lines()
        assert {call.request.method for call in route.calls} == {"GET"}


class TestCatalogueCommands:
    """``products list`` / ``product show``."""

    @respx.mock
    async def test_products_list_covers_every_configured_location(self, settings: Any) -> None:
        route = respx.get(f"{BASE}/ordering/v1/products/vps").mock(
            return_value=httpx.Response(
                200,
                json={
                    "vpss": [
                        {
                            "id": "VPS02_1",
                            "name": "VPS 2.1",
                            "vCpu": "2",
                            "vRam": "4",
                            "nvmeStorage": "100 GB",
                            "traffic": "20 TB",
                            "price": {"total": "9.99", "currency": "EUR"},
                        }
                    ],
                    "_metadata": {"totalCount": 1, "offset": 0, "limit": 100},
                },
            )
        )
        lines = await diagnostics.products_list_lines()
        assert [call.request.url.params["location"] for call in route.calls] == [
            "AMS-01",
            "FRA-01",
        ]
        assert any("location AMS-01: 1 product(s)" in line for line in lines)
        assert any("VPS02_1" in line for line in lines)

    @respx.mock
    async def test_products_list_accepts_an_explicit_location(self, settings: Any) -> None:
        route = respx.get(f"{BASE}/ordering/v1/products/vps").mock(
            return_value=httpx.Response(200, json={"vpss": [], "_metadata": {}})
        )
        await diagnostics.products_list_lines("SFO-01")
        assert dict(route.calls.last.request.url.params)["location"] == "SFO-01"

    @respx.mock
    async def test_product_show_prints_every_option_group(self, settings: Any) -> None:
        route = respx.get(f"{BASE}/ordering/v1/products/vps/VPS02_1").mock(
            return_value=httpx.Response(200, json={"vps": PRODUCT_DETAIL})
        )
        lines = await diagnostics.product_show_lines(
            "VPS02_1",
            location="AMS-01",
            operating_system="Ubuntu 22.04",
            control_panel="Plesk",
            contract_term="1_MONTH",
            billing_cycle="1_MONTH",
            disk_upgrade="250 GB",
            service_level_agreement="Basic",
        )
        params = dict(route.calls.last.request.url.params)
        assert params["operatingSystem"] == "Ubuntu 22.04"
        assert params["controlPanel"] == "Plesk"
        assert params["diskUpgrade"] == "250 GB"
        assert params["serviceLevelAgreement"] == "Basic"
        body = "\n".join(lines)
        assert "product VPS02_1 at AMS-01" in body
        assert "price: total=9.99 EUR" in body
        assert "contractTerms: 1_MONTH=9.99" in body
        assert "operating_system:" in body and "Ubuntu 22.04" in body
        assert "control_panel:" in body and "Plesk" in body
        assert "disk_upgrade:" in body and "service_level_agreement:" in body


class TestOrderCommands:
    """``orders list`` / ``order show`` — read-only order inspection."""

    @respx.mock
    async def test_orders_list_reports_services(self, settings: Any) -> None:
        route = respx.get(f"{BASE}/account/v1/orders").mock(
            return_value=httpx.Response(
                200,
                json={
                    "orders": [
                        {
                            "id": ORDER_ID,
                            "type": "NEW_ORDER",
                            "createdAt": "2024-08-23T11:00:00Z",
                            "services": [
                                {
                                    "id": "SVC-1",
                                    "productId": "VIRTUAL_SERVER",
                                    "status": "ACTIVE",
                                    "equipmentId": VPS,
                                }
                            ],
                        }
                    ],
                    "_metadata": {"totalCount": 1, "offset": 0, "limit": 20},
                },
            )
        )
        lines = await diagnostics.orders_list_lines(limit=5)
        assert dict(route.calls.last.request.url.params) == {"limit": "5", "offset": "0"}
        assert any(ORDER_ID in line and f"equipmentId={VPS}" in line for line in lines)

    @respx.mock
    async def test_order_show_prints_the_equipment_identity(self, settings: Any) -> None:
        route = respx.get(f"{BASE}/account/v1/orders/{ORDER_ID}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": ORDER_ID,
                    "type": "NEW_ORDER",
                    "origin": "WEBSITE",
                    "contractId": "CT-1",
                    "services": [
                        {
                            "id": "SVC-1",
                            "productId": "VIRTUAL_SERVER",
                            "status": "TO_BE_PROVISIONED",
                            "deliveryEstimate": "2024-08-23T12:00:00Z",
                            "pricePerFrequency": "9.99",
                            "currency": "EUR",
                            "contractTerm": "1_MONTH",
                            "billingCycle": "1_MONTH",
                        }
                    ],
                },
            )
        )
        lines = await diagnostics.order_show_lines(ORDER_ID)
        assert route.calls.last.request.method == "GET"
        body = "\n".join(lines)
        assert "<not yet>" in body
        assert "status=TO_BE_PROVISIONED" in body

    @respx.mock
    async def test_order_commands_only_use_get(self, settings: Any) -> None:
        route = respx.route(url__regex=rf"{BASE}/account/v1/orders.*").mock(
            return_value=httpx.Response(200, json={"orders": [], "_metadata": {}})
        )
        await diagnostics.orders_list_lines()
        assert all(call.request.method == "GET" for call in route.calls)


class TestVpsCommands:
    """``vps list|show|ips|metrics|snapshots|monitoring`` — all read-only."""

    @respx.mock
    async def test_vps_list(self, settings: Any) -> None:
        route = respx.get(f"{BASE}/publicCloud/v1/vps/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "vps": [VPS_DETAIL],
                    "_metadata": {"totalCount": 1, "offset": 0, "limit": 100},
                },
            )
        )
        lines = await diagnostics.vps_list_lines()
        assert route.calls.last.request.method == "GET"
        assert any(f"{VPS} state=RUNNING" in line for line in lines)
        assert any("ipv4=1.2.3.4" in line for line in lines)

    @respx.mock
    async def test_vps_show_includes_resources_contract_and_ips(self, settings: Any) -> None:
        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}").mock(
            return_value=httpx.Response(200, json=VPS_DETAIL)
        )
        lines = await diagnostics.vps_show_lines(VPS)
        body = "\n".join(lines)
        assert "resources: cpu=2vCPU memory=4GiB network=10Gbps" in body
        assert "contract: id=41228459000100" in body
        assert "ip 1.2.3.4/32 v4 network=PUBLIC" in body

    @respx.mock
    async def test_vps_show_tolerates_missing_optional_blocks(self, settings: Any) -> None:
        minimal = {
            key: value
            for key, value in VPS_DETAIL.items()
            if key not in {"resources", "contract", "iso"}
        }
        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}").mock(
            return_value=httpx.Response(200, json=minimal)
        )
        lines = await diagnostics.vps_show_lines(VPS)
        assert any("iso=-" in line for line in lines)
        assert not any("resources:" in line for line in lines)

    @respx.mock
    async def test_vps_ips(self, settings: Any) -> None:
        route = respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}/ips").mock(
            return_value=httpx.Response(
                200,
                json={"ips": [IP_PAYLOAD], "_metadata": {"totalCount": 1}},
            )
        )
        lines = await diagnostics.vps_ips_lines(VPS)
        assert route.calls.last.request.method == "GET"
        assert lines[0] == "1 IP(s)"
        assert "1.2.3.4/32 v4 PUBLIC" in lines[1]

    @respx.mock
    async def test_vps_metrics(self, settings: Any) -> None:
        route = respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}/metrics/datatraffic").mock(
            return_value=httpx.Response(200, json=METRICS_PAYLOAD)
        )
        lines = await diagnostics.vps_metrics_lines(
            VPS, from_="2024-01-01T00:00:00Z", to="2024-01-02T00:00:00Z"
        )
        params = dict(route.calls.last.request.url.params)
        assert params["from"] == "2024-01-01T00:00:00Z"
        assert params["granularity"] == "DAY" and params["aggregation"] == "SUM"
        body = "\n".join(lines)
        assert "total_bytes=1024" in body
        assert "downPublic: 1 point(s)" in body

    @respx.mock
    async def test_vps_snapshots(self, settings: Any) -> None:
        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}/snapshots").mock(
            return_value=httpx.Response(
                200,
                json={
                    "snapshots": [
                        {
                            "id": SNAPSHOT_ID,
                            "displayName": "before upgrade",
                            "state": "READY",
                            "created": "2024-01-01T00:00:00Z",
                        }
                    ],
                    "_metadata": {"totalCount": 1},
                },
            )
        )
        lines = await diagnostics.vps_snapshots_lines(VPS)
        assert any(SNAPSHOT_ID in line and "state=READY" in line for line in lines)

    @respx.mock
    async def test_vps_monitoring_is_status_only(self, settings: Any) -> None:
        route = respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}/monitoring/status").mock(
            return_value=httpx.Response(200, json={"status": "UP", "description": "Service is Up"})
        )
        lines = await diagnostics.vps_monitoring_lines(VPS)
        assert route.calls.last.request.method == "GET"
        assert "monitoring status=UP" in lines[0]
        # The enable operation is never invoked by a diagnostic.
        assert "use the monitoring/enable operation" in lines[1]

    @respx.mock
    async def test_every_vps_diagnostic_is_read_only(self, settings: Any) -> None:
        route = respx.route(url__regex=rf"{BASE}/publicCloud/v1/vps.*").mock(
            return_value=httpx.Response(
                200,
                json={**VPS_DETAIL, "ips": [IP_PAYLOAD], "snapshots": [], "vps": [VPS_DETAIL]},
            )
        )
        await diagnostics.vps_list_lines()
        await diagnostics.vps_show_lines(VPS)
        await diagnostics.vps_ips_lines(VPS)
        await diagnostics.vps_snapshots_lines(VPS)
        await diagnostics.vps_monitoring_lines(VPS)
        assert {call.request.method for call in route.calls} == {"GET"}


class TestCoverageCommand:
    """``leaseweb coverage`` — offline, no network at all."""

    def test_coverage_lines_summarise_the_inventory(self) -> None:
        lines = diagnostics.coverage_lines()
        body = "\n".join(lines)
        assert "total implemented:  43" in body
        assert "VPS operations:     38" in body
        assert "POST   /publicCloud/v1/vps/{vpsId}/start" in body
        assert "(destructive)" in body
        assert "/virtualServers" in body  # the legacy exclusion is documented

    def test_coverage_lists_every_operation_exactly_once(self) -> None:
        lines = diagnostics.coverage_lines()
        starts = [line for line in lines if "-> LeaseWeb" in line]
        assert len(starts) == 43
        assert len(set(starts)) == 43


class TestNoBillableOrDestructiveCommand:
    """A CLI mistake must never purchase or destroy anything."""

    def test_diagnostics_module_never_calls_a_mutating_client_method(self) -> None:
        from pathlib import Path

        source = Path(diagnostics.__file__).read_text(encoding="utf-8")
        for forbidden in (
            ".order_vps(",
            ".start_vps(",
            ".stop_vps(",
            ".reboot_vps(",
            ".reinstall(",
            ".reset_password(",
            ".create_snapshot(",
            ".restore_snapshot(",
            ".delete_snapshot(",
            ".attach_iso(",
            ".detach_iso(",
            ".null_route_ip(",
            ".store_credential(",
            ".update_credential(",
            ".delete_credential(",
            ".delete_credentials(",
            ".update_vps(",
            ".create_data_traffic_notification_setting(",
            ".update_data_traffic_notification_setting(",
            ".delete_data_traffic_notification_setting(",
        ):
            assert forbidden not in source, forbidden

    def test_cli_exposes_no_order_now_or_smoke_order_command(self) -> None:
        from pathlib import Path

        source = Path(cli.__file__).read_text(encoding="utf-8")
        for forbidden in ("order-now", "smoke-order", "order_now", "smoke_order"):
            assert forbidden not in source

    def test_no_diagnostic_prints_a_secret(self) -> None:
        from pathlib import Path

        source = Path(diagnostics.__file__).read_text(encoding="utf-8")
        for forbidden in ("get_secret_value", "X-LSW-Auth: ", "reveal()"):
            # ``ConsoleSession.reveal``-style access is never wired into a
            # printable diagnostic line.
            assert forbidden not in source, forbidden
