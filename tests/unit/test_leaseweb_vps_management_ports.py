"""The Leaseweb adapter's provider-neutral VPS management ports (§10/§22).

The adapter implements the neutral records in
``cloud_platform.providers.vps_ports`` over the typed modern-VPS client, so
domain/application code never sees a Leaseweb URL, DTO or error class.

The suite exercises every port method against a mocked HTTP boundary and
asserts that:

- the documented method/path is used and the neutral record is mapped
  correctly (states, IPs, snapshots, metrics in bytes, notification channels);
- bytes are integers (never floats) on the usage path;
- a missing VPS maps to ``None`` rather than raising;
- the console session record never leaks the URL through its ``repr``.
"""

from __future__ import annotations

from typing import Any

import httpx
import respx

from cloud_platform.providers.leaseweb.client import Throttle
from cloud_platform.providers.leaseweb.ordering import LeaseWebOrderingProvider
from cloud_platform.providers.leaseweb.vps.models import NotificationAction, TimePeriod

KEY = "test-leaseweb-key"
BASE = "https://api.test"
VPS = "VPS02_1"
SNAPSHOT_ID = "3a042956-0689-45dc-8322-8b8325464182"
NOTIFICATION_ID = "3a042956-0689-45dc-8322-8b8325464183"
ISO_ID = "GRML"


def _no_sleep() -> Throttle:
    async def wait(_seconds: float) -> None:
        return None

    return Throttle(max_rps=100_000.0, wait=wait)


def _provider() -> LeaseWebOrderingProvider:
    return LeaseWebOrderingProvider(
        api_key=KEY,
        base_url=BASE,
        locations=("AMS-01",),
        throttle=_no_sleep(),
        max_retries=0,
    )


IP = {
    "ip": "1.2.3.4",
    "prefixLength": "32",
    "version": 4,
    "nullRouted": False,
    "reverseLookup": "host.example.com",
    "mainIp": True,
    "networkType": "PUBLIC",
    "ddos": {"detectionProfile": "STANDARD_DEFAULT", "protectionType": "STANDARD"},
}

SUMMARY = {
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
    "marketAppId": None,
    "state": "RUNNING",
    "hasPublicIpV4": True,
    "rootDiskSize": 50,
    "startedAt": "2023-11-30T16:31:28+00:00",
    "ips": [IP],
}

DETAIL = {
    **SUMMARY,
    "iso": {"id": ISO_ID, "name": "GRML Rescue"},
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
        "controlPanel": None,
        "inModification": False,
    },
}

SNAPSHOT = {
    "id": SNAPSHOT_ID,
    "displayName": "before upgrade",
    "state": "READY",
    "created": "2024-01-01T00:00:00Z",
}

NOTIFICATION = {
    "id": NOTIFICATION_ID,
    "threshold": {"value": 1000, "unit": "GB"},
    "timePeriod": "DAY",
    "type": "DATA_TRAFFIC",
    "action": "POWER_OFF",
    "channels": [{"type": "EMAIL", "contactGroup": "GENERAL", "contacts": ["ops@example.com"]}],
}

METRICS = {
    "metrics": {
        "downPublic": {
            "values": [{"value": 1024, "timestamp": "2024-01-01T00:00:00Z"}],
            "unit": "B",
        }
    },
    "_metadata": {
        "from": "2024-01-01T00:00:00Z",
        "to": "2024-01-02T00:00:00Z",
        "granularity": "DAY",
        "aggregation": "SUM",
        "unit": "B",
        "summary": {
            "downPublic": {
                "average": 512,
                "expected": 1024,
                "total": 1024,
                "peak": {"value": 2048, "timestamp": "2024-01-01T12:00:00Z"},
            }
        },
    },
}


def _page(key: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        key: rows,
        "_metadata": {"totalCount": len(rows), "offset": 0, "limit": 100},
    }


class TestInventoryPorts:
    """``VpsInventoryProvider``."""

    @respx.mock
    async def test_get_vps_info_maps_detail_and_usage_metadata(self) -> None:
        route = respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}").mock(
            return_value=httpx.Response(200, json=DETAIL)
        )
        provider = _provider()
        try:
            info = await provider.get_vps_info(VPS)
        finally:
            await provider.close()
        assert route.calls.last.request.method == "GET"
        assert info is not None
        assert (info.id, info.state, info.pack, info.region, info.datacenter) == (
            VPS,
            "RUNNING",
            "Leaseweb VPS 2",
            "eu-west-3",
            "AMS-01",
        )
        assert info.image_id == "UBUNTU_22_04_64BIT"
        assert info.root_disk_gb == 50
        assert info.contract_id == "41228459000100"
        assert info.contract_state == "ACTIVE"
        assert info.contract_term == 12
        assert info.sla == "Basic"
        assert info.metadata["iso_id"] == ISO_ID
        resources = info.metadata["resources"]
        assert resources["cpu"] == 2 and resources["memory"] == "4"
        assert info.metadata["documented_state"] is True

    @respx.mock
    async def test_missing_vps_is_none_not_an_error(self) -> None:
        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}").mock(
            return_value=httpx.Response(404, json={"errorMessage": "unknown"})
        )
        provider = _provider()
        try:
            assert await provider.get_vps_info(VPS) is None
        finally:
            await provider.close()

    @respx.mock
    async def test_list_vps_info_iterates_every_page(self) -> None:
        respx.get(f"{BASE}/publicCloud/v1/vps/").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "vps": [SUMMARY],
                        "_metadata": {"totalCount": 2, "offset": 0, "limit": 1},
                    },
                ),
                httpx.Response(
                    200,
                    json={
                        "vps": [{**SUMMARY, "id": "VPS04_1"}],
                        "_metadata": {"totalCount": 2, "offset": 1, "limit": 1},
                    },
                ),
            ]
        )
        provider = _provider()
        try:
            rows = await provider.list_vps_info()
        finally:
            await provider.close()
        assert [row.id for row in rows] == [VPS, "VPS04_1"]
        assert rows[0].metadata["has_public_ip_v4"] is True
        assert rows[0].metadata["ips"][0]["ip"] == "1.2.3.4"

    @respx.mock
    async def test_rename_vps_uses_the_documented_put(self) -> None:
        route = respx.put(f"{BASE}/publicCloud/v1/vps/{VPS}").mock(
            return_value=httpx.Response(200, json={**DETAIL, "reference": "srv-2"})
        )
        provider = _provider()
        try:
            info = await provider.rename_vps(VPS, "srv-2")
        finally:
            await provider.close()
        assert route.calls.last.request.method == "PUT"
        assert route.calls.last.request.content == b'{"reference":"srv-2"}'
        assert info.reference == "srv-2"


class TestPowerPorts:
    """``VpsPowerProvider``."""

    @respx.mock
    async def test_power_actions_use_post_and_return_neutral_records(self) -> None:
        start = respx.post(f"{BASE}/publicCloud/v1/vps/{VPS}/start").mock(
            return_value=httpx.Response(202)
        )
        stop = respx.post(f"{BASE}/publicCloud/v1/vps/{VPS}/stop").mock(
            return_value=httpx.Response(202)
        )
        reboot = respx.post(f"{BASE}/publicCloud/v1/vps/{VPS}/reboot").mock(
            return_value=httpx.Response(202)
        )
        provider = _provider()
        try:
            assert (await provider.start_vps(VPS)).action == "start"
            assert (await provider.stop_vps(VPS)).action == "stop"
            assert (await provider.reboot_vps(VPS)).action == "reboot"
        finally:
            await provider.close()
        assert start.calls.last.request.method == "POST"
        assert stop.calls.last.request.method == "POST"
        assert reboot.calls.last.request.method == "POST"

    @respx.mock
    async def test_console_session_is_secret_redacting(self) -> None:
        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}/console").mock(
            return_value=httpx.Response(200, json={"url": "https://console.example.com/?token=abc"})
        )
        provider = _provider()
        try:
            session = await provider.get_console_session(VPS)
        finally:
            await provider.close()
        assert session.url == "https://console.example.com/?token=abc"
        # The neutral record never prints the temporary credential.
        from cloud_platform.providers.vps_ports import ConsoleSession

        assert "console.example.com" not in repr(ConsoleSession(url=session.url))


class TestIsoAndReinstallPorts:
    """``VpsReinstallProvider`` + the ISO catalogue."""

    @respx.mock
    async def test_iso_catalogue_is_neutral(self) -> None:
        respx.get(f"{BASE}/publicCloud/v1/vps/isos").mock(
            return_value=httpx.Response(200, json=_page("isos", [{"id": ISO_ID, "name": "GRML"}]))
        )
        provider = _provider()
        try:
            rows = await provider.list_vps_isos()
        finally:
            await provider.close()
        assert [(row.id, row.name) for row in rows] == [(ISO_ID, "GRML")]

    @respx.mock
    async def test_attach_and_detach_iso_use_post(self) -> None:
        attach = respx.post(f"{BASE}/publicCloud/v1/vps/{VPS}/attachIso").mock(
            return_value=httpx.Response(202)
        )
        detach = respx.post(f"{BASE}/publicCloud/v1/vps/{VPS}/detachIso").mock(
            return_value=httpx.Response(202)
        )
        provider = _provider()
        try:
            assert (await provider.attach_vps_iso(VPS, ISO_ID)).action == "attach_iso"
            assert (await provider.detach_vps_iso(VPS)).action == "detach_iso"
        finally:
            await provider.close()
        assert attach.calls.last.request.content == b'{"isoId":"GRML"}'
        assert not detach.calls.last.request.content

    @respx.mock
    async def test_reinstall_images_map_min_disk_size(self) -> None:
        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}/reinstall/images").mock(
            return_value=httpx.Response(
                200,
                json=_page(
                    "images",
                    [
                        {
                            "id": "UBUNTU_22_04_64BIT",
                            "name": "Ubuntu 22.04 LTS",
                            "family": "linux",
                            "custom": False,
                            "minDiskSize": 20,
                        }
                    ],
                ),
            )
        )
        provider = _provider()
        try:
            rows = await provider.list_vps_reinstall_images(VPS)
        finally:
            await provider.close()
        assert rows[0].id == "UBUNTU_22_04_64BIT"
        assert rows[0].family == "linux"
        assert rows[0].min_disk_gb == 20

    @respx.mock
    async def test_reinstall_uses_put_and_sends_the_image(self) -> None:
        route = respx.put(f"{BASE}/publicCloud/v1/vps/{VPS}/reinstall").mock(
            return_value=httpx.Response(202)
        )
        provider = _provider()
        try:
            accepted = await provider.reinstall_vps(VPS, "UBUNTU_22_04_64BIT")
        finally:
            await provider.close()
        assert accepted.action == "reinstall"
        assert route.calls.last.request.method == "PUT"
        assert route.calls.last.request.content == b'{"imageId":"UBUNTU_22_04_64BIT"}'


class TestIpPorts:
    """``VpsIpProvider``."""

    @respx.mock
    async def test_list_and_get_ip_map_the_neutral_record(self) -> None:
        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}/ips").mock(
            return_value=httpx.Response(200, json=_page("ips", [IP]))
        )
        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}/ips/1.2.3.4").mock(
            return_value=httpx.Response(200, json=IP)
        )
        provider = _provider()
        try:
            rows = await provider.list_vps_ips(VPS)
            single = await provider.get_vps_ip(VPS, "1.2.3.4")
        finally:
            await provider.close()
        assert rows[0].ip == "1.2.3.4"
        assert rows[0].network_type == "PUBLIC"
        assert rows[0].prefix_length == "32"
        assert rows[0].metadata["ddos"]["protection_type"] == "STANDARD"
        assert single.main_ip is True

    @respx.mock
    async def test_reverse_dns_uses_put(self) -> None:
        route = respx.put(f"{BASE}/publicCloud/v1/vps/{VPS}/ips/1.2.3.4").mock(
            return_value=httpx.Response(200, json=IP)
        )
        provider = _provider()
        try:
            await provider.set_vps_ip_reverse_dns(VPS, "1.2.3.4", "host.example.com")
        finally:
            await provider.close()
        assert route.calls.last.request.method == "PUT"
        assert route.calls.last.request.content == b'{"reverseLookup":"host.example.com"}'

    @respx.mock
    async def test_null_route_body_is_omitted_when_no_options(self) -> None:
        route = respx.post(f"{BASE}/publicCloud/v1/vps/{VPS}/ips/1.2.3.4/null").mock(
            return_value=httpx.Response(200, json=IP)
        )
        provider = _provider()
        try:
            await provider.null_route_vps_ip(VPS, "1.2.3.4")
        finally:
            await provider.close()
        assert not route.calls.last.request.content

    @respx.mock
    async def test_null_route_with_options_keeps_the_documented_typo(self) -> None:
        route = respx.post(f"{BASE}/publicCloud/v1/vps/{VPS}/ips/1.2.3.4/null").mock(
            return_value=httpx.Response(200, json=IP)
        )
        provider = _provider()
        try:
            await provider.null_route_vps_ip(
                VPS, "1.2.3.4", comment="abuse", automated_unnuling_hours=24
            )
        finally:
            await provider.close()
        assert route.calls.last.request.content == b'{"comment":"abuse","automatedUnnulingAt":24}'

    @respx.mock
    async def test_unnull_route_uses_post(self) -> None:
        route = respx.post(f"{BASE}/publicCloud/v1/vps/{VPS}/ips/1.2.3.4/unnull").mock(
            return_value=httpx.Response(200, json=IP)
        )
        provider = _provider()
        try:
            await provider.unnull_route_vps_ip(VPS, "1.2.3.4")
        finally:
            await provider.close()
        assert route.calls.last.request.method == "POST"


class TestSnapshotPorts:
    """``VpsSnapshotProvider``."""

    @respx.mock
    async def test_list_get_create_restore_delete(self) -> None:
        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}/snapshots").mock(
            return_value=httpx.Response(200, json=_page("snapshots", [SNAPSHOT]))
        )
        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}/snapshots/{SNAPSHOT_ID}").mock(
            return_value=httpx.Response(200, json=SNAPSHOT)
        )
        create = respx.post(f"{BASE}/publicCloud/v1/vps/{VPS}/snapshots").mock(
            return_value=httpx.Response(202)
        )
        restore = respx.put(f"{BASE}/publicCloud/v1/vps/{VPS}/snapshots/{SNAPSHOT_ID}").mock(
            return_value=httpx.Response(202)
        )
        delete = respx.delete(f"{BASE}/publicCloud/v1/vps/{VPS}/snapshots/{SNAPSHOT_ID}").mock(
            return_value=httpx.Response(202)
        )
        provider = _provider()
        try:
            rows = await provider.list_vps_snapshots(VPS)
            single = await provider.get_vps_snapshot(VPS, SNAPSHOT_ID)
            assert (await provider.create_vps_snapshot(VPS, "before upgrade")).action == (
                "create_snapshot"
            )
            assert (await provider.restore_vps_snapshot(VPS, SNAPSHOT_ID)).action == (
                "restore_snapshot"
            )
            assert (await provider.delete_vps_snapshot(VPS, SNAPSHOT_ID)).action == (
                "delete_snapshot"
            )
        finally:
            await provider.close()
        assert rows[0].created_at == "2024-01-01T00:00:00Z"
        assert single.name == "before upgrade"
        assert create.calls.last.request.content == b'{"name":"before upgrade"}'
        assert restore.calls.last.request.method == "PUT"
        assert delete.calls.last.request.method == "DELETE"


class TestMetricsMonitoringAndCredentials:
    """``VpsMetricsProvider`` / ``VpsMonitoringProvider`` / credentials."""

    @respx.mock
    async def test_data_traffic_is_integer_bytes_with_preserved_metadata(self) -> None:
        route = respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}/metrics/datatraffic").mock(
            return_value=httpx.Response(200, json=METRICS)
        )
        provider = _provider()
        try:
            usage = await provider.get_vps_data_traffic(
                VPS,
                from_="2024-01-01T00:00:00Z",
                to="2024-01-02T00:00:00Z",
                granularity="DAY",
            )
        finally:
            await provider.close()
        assert route.calls.last.request.url.params["granularity"] == "DAY"
        assert len(usage) == 1
        record = usage[0]
        assert record.direction == "downPublic"
        assert record.unit == "B"
        for value in (
            record.total_bytes,
            record.average_bytes,
            record.expected_bytes,
            record.peak_bytes,
            record.points[0].bytes,
        ):
            assert isinstance(value, int) and not isinstance(value, bool)
        assert record.peak_bytes == 2048
        assert record.metadata["aggregation"] == "SUM"
        assert record.metadata["unit"] == "B"
        assert record.points[0].timestamp is not None

    @respx.mock
    async def test_monitoring_status_and_enable(self) -> None:
        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}/monitoring/status").mock(
            return_value=httpx.Response(200, json={"status": "UP", "description": "Service is Up"})
        )
        enable = respx.post(f"{BASE}/publicCloud/v1/vps/{VPS}/monitoring/enable").mock(
            return_value=httpx.Response(204)
        )
        provider = _provider()
        try:
            record = await provider.get_vps_monitoring(VPS)
            await provider.enable_vps_monitoring(VPS)
        finally:
            await provider.close()
        assert record.status == "UP"
        assert record.description == "Service is Up"
        assert record.documented is True
        assert enable.calls.last.request.method == "POST"

    @respx.mock
    async def test_unknown_monitoring_status_is_flagged_undocumented(self) -> None:
        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}/monitoring/status").mock(
            return_value=httpx.Response(200, json={"status": "SOMETHING_NEW"})
        )
        provider = _provider()
        try:
            record = await provider.get_vps_monitoring(VPS)
        finally:
            await provider.close()
        assert record.status == "SOMETHING_NEW"
        assert record.documented is False

    @respx.mock
    async def test_credential_list_never_returns_a_password(self) -> None:
        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}/credentials").mock(
            return_value=httpx.Response(
                200,
                json={
                    "credentials": [
                        {"type": "OPERATING_SYSTEM", "username": "root"},
                        {"type": "CONTROL_PANEL", "username": "admin"},
                    ]
                },
            )
        )
        provider = _provider()
        try:
            rows = await provider.list_vps_credentials(VPS)
        finally:
            await provider.close()
        assert rows == [
            {"type": "OPERATING_SYSTEM", "username": "root"},
            {"type": "CONTROL_PANEL", "username": "admin"},
        ]

    @respx.mock
    async def test_reset_password_uses_post(self) -> None:
        route = respx.post(f"{BASE}/publicCloud/v1/vps/{VPS}/resetPassword").mock(
            return_value=httpx.Response(202)
        )
        provider = _provider()
        try:
            assert (await provider.reset_vps_password(VPS)).action == "reset_password"
        finally:
            await provider.close()
        assert route.calls.last.request.method == "POST"


class TestNotificationPorts:
    """Notification-setting CRUD over the documented (unusual) path shape."""

    @respx.mock
    async def test_list_and_get(self) -> None:
        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}/notificationSettings/dataTraffic").mock(
            return_value=httpx.Response(200, json=_page("notificationSettings", [NOTIFICATION]))
        )
        respx.get(
            f"{BASE}/publicCloud/v1/vps/{VPS}/notificationSettings/dataTraffic/{NOTIFICATION_ID}"
        ).mock(return_value=httpx.Response(200, json=NOTIFICATION))
        provider = _provider()
        try:
            rows = await provider.list_vps_notification_settings(VPS)
            single = await provider.get_vps_notification_setting(VPS, NOTIFICATION_ID)
        finally:
            await provider.close()
        for record in (*rows, single):
            assert record.id == NOTIFICATION_ID
            assert record.threshold_value == 1000
            assert record.threshold_unit == "GB"
            assert record.action == NotificationAction.POWER_OFF
            assert record.time_period == TimePeriod.DAY
            assert record.channels[0]["contact_group"] == "GENERAL"
            assert record.channels[0]["contacts"] == ["ops@example.com"]

    @respx.mock
    async def test_create_and_update_use_the_client_supplied_id_path(self) -> None:
        create = respx.post(
            f"{BASE}/publicCloud/v1/vps/{VPS}/notificationSettings/dataTraffic/{NOTIFICATION_ID}"
        ).mock(return_value=httpx.Response(201, json=NOTIFICATION))
        update = respx.put(
            f"{BASE}/publicCloud/v1/vps/{VPS}/notificationSettings/dataTraffic/{NOTIFICATION_ID}"
        ).mock(return_value=httpx.Response(200, json=NOTIFICATION))
        provider = _provider()
        try:
            created = await provider.create_vps_notification_setting(
                VPS,
                NOTIFICATION_ID,
                {
                    "threshold": {"value": 1000, "unit": "GB"},
                    "timePeriod": "DAY",
                    "action": "POWER_OFF",
                    "channels": [{"type": "EMAIL", "contactGroup": "GENERAL", "contacts": []}],
                },
            )
            updated = await provider.update_vps_notification_setting(
                VPS, NOTIFICATION_ID, {"timePeriod": "MONTH"}
            )
        finally:
            await provider.close()
        assert created.id == NOTIFICATION_ID and updated.id == NOTIFICATION_ID
        assert create.calls.last.request.method == "POST"
        assert update.calls.last.request.method == "PUT"
        assert b'"timePeriod":"MONTH"' in update.calls.last.request.content

    @respx.mock
    async def test_delete_uses_delete_and_returns_none(self) -> None:
        route = respx.delete(
            f"{BASE}/publicCloud/v1/vps/{VPS}/notificationSettings/dataTraffic/{NOTIFICATION_ID}"
        ).mock(return_value=httpx.Response(204))
        provider = _provider()
        try:
            assert await provider.delete_vps_notification_setting(VPS, NOTIFICATION_ID) is None
        finally:
            await provider.close()
        assert route.calls.last.request.method == "DELETE"


class TestNeutralBoundary:
    """Domain/application code must never receive a Leaseweb DTO."""

    @respx.mock
    async def test_records_are_provider_neutral_dataclasses(self) -> None:
        from cloud_platform.providers import vps_ports

        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}").mock(
            return_value=httpx.Response(200, json=DETAIL)
        )
        provider = _provider()
        try:
            info = await provider.get_vps_info(VPS)
        finally:
            await provider.close()
        assert info is not None
        assert isinstance(info, vps_ports.VpsInfo)
        for value in info.metadata.values():
            assert not value.__class__.__module__.startswith("cloud_platform.providers.leaseweb"), (
                value
            )
