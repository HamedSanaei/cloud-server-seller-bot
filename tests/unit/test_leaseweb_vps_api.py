"""Mocked contract tests for the modern Leaseweb VPS API (38 operations).

Every test mocks the HTTP boundary (``respx``): NO test can reach the real
Leaseweb API and NO test can purchase, reinstall, reset, snapshot, stop or
delete a real resource. For each operation the suite asserts the documented
HTTP method, the exact endpoint path (including percent-encoded hostile path
parameters), the documented query parameters, the ``X-LSW-Auth`` header, the
documented request-body serialization and the documented success/error
semantics (401/403/404/400/429/500/503, timeouts, unknown values).

It also proves the security properties of the integration: credential
passwords, console URLs and API keys never appear in ``repr()``, exceptions,
structured logs, logger output or failed-response diagnostics.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
import respx

from cloud_platform.providers.errors import (
    ProviderConflict,
    ProviderError,
    ProviderNotFound,
    ProviderOutcomeUnknown,
    ProviderRateLimited,
    ProviderUnavailable,
)
from cloud_platform.providers.leaseweb.errors import (
    LeasewebAmbiguousMutationError,
    LeasewebAuthenticationError,
    LeasewebConflictError,
    LeasewebForbiddenError,
    LeasewebNotFoundError,
    LeasewebRateLimitError,
    LeasewebResponseError,
    LeasewebServerError,
    LeasewebTimeoutError,
    LeasewebUnavailableError,
    LeasewebValidationError,
    parse_error_payload,
    redact_sensitive,
)
from cloud_platform.providers.leaseweb.transport import LeasewebTransport, Throttle
from cloud_platform.providers.leaseweb.vps.client import (
    CUSTOMER_EXPOSABLE_OPERATIONS,
    DESTRUCTIVE_OPERATIONS,
    OPERATOR_ONLY_OPERATIONS,
    LeaseWebVpsApi,
)
from cloud_platform.providers.leaseweb.vps.models import (
    CreateNotificationSettingRequest,
    CreateSnapshotRequest,
    CredentialType,
    NullRouteIpRequest,
    ReinstallRequest,
    StoreCredentialRequest,
    UpdateCredentialRequest,
    UpdateIpRequest,
    UpdateNotificationSettingRequest,
    UpdateVpsRequest,
)

KEY = "test-leaseweb-key"
BASE = "https://api.test"
VPS = "VPS02_1"
SNAPSHOT_ID = "3a042956-0689-45dc-8322-8b8325464182"
NOTIFICATION_ID = "3a042956-0689-45dc-8322-8b8325464183"
PASSWORD = "super-secret-root-password"  # pragma: allowlist secret

API_KEY_SENTINEL = "test-api-key-should-never-leak"  # pragma: allowlist secret
PRIVATE_KEY = "PRIVATE-SSH-KEY-CONTENT"  # pragma: allowlist secret
# Deliberate fake PEM block for the redaction tests (never a real key).
PEM_BLOCK = (
    "-----BEGIN RSA PRIVATE KEY-----\n"  # pragma: allowlist secret
    f"{PRIVATE_KEY}\n-----END RSA PRIVATE KEY-----"
)


def _no_sleep() -> Throttle:
    async def wait(_seconds: float) -> None:
        return None

    return Throttle(max_rps=100_000.0, wait=wait)


def _api() -> LeaseWebVpsApi:
    return LeaseWebVpsApi(LeasewebTransport(KEY, BASE, throttle=_no_sleep(), max_retries=2))


def _page(**extra: Any) -> dict[str, Any]:
    return {"_metadata": {"totalCount": 1, "offset": 0, "limit": 100}, **extra}


IP_PAYLOAD = {
    "ip": "1.2.3.4",
    "prefixLength": "32",
    "version": 4,
    "nullRouted": False,
    "reverseLookup": "host.example.com",
    "mainIp": True,
    "networkType": "PUBLIC",
    "ddos": {"detectionProfile": "STANDARD_DEFAULT", "protectionType": "STANDARD"},
}

VPS_SUMMARY = {
    "id": VPS,
    "pack": "Leaseweb VPS 2",
    "region": "eu-west-3",
    "datacenter": "AMS-01",
    "reference": "srv-1",
    "image": {
        "id": "UBUNTU_22_04_64BIT",
        "name": "Ubuntu 22.04 LTS (x86_64)",
        "family": "linux",
        "flavour": "ubuntu",
        "custom": False,
    },
    "marketAppId": None,
    "state": "RUNNING",
    "hasPublicIpV4": True,
    "rootDiskSize": 50,
    "startedAt": "2023-11-30T16:31:28+00:00",
    "ips": [IP_PAYLOAD],
}

VPS_DETAIL = {
    **VPS_SUMMARY,
    "iso": {"id": "GRML", "name": "GRML Rescue"},
    "ips": [IP_PAYLOAD],
    "resources": {
        "cpu": {"value": 2, "unit": "vCPU"},
        "memory": {"value": 3.75, "unit": "GiB"},
        "publicNetworkSpeed": {"value": 10, "unit": "Gbps"},
    },
    "contract": {
        "id": "41228459000100",
        "type": "MONTHLY",
        "state": "ACTIVE",
        "term": 12,
        "billingFrequency": 1,
        "startsAt": "2023-11-30T16:31:28+00:00",
        "endsAt": None,
        "sla": "Basic",
        "controlPanel": None,
        "inModification": False,
        "dataTraffic": {"value": 30, "unit": "TB"},
    },
}

SNAPSHOT_PAYLOAD = {
    "id": SNAPSHOT_ID,
    "displayName": "my snapshot",
    "state": "READY",
    "created": "2024-01-01T00:00:00Z",
}

NOTIFICATION_PAYLOAD = {
    "id": NOTIFICATION_ID,
    "threshold": {"value": 1000, "unit": "GB"},
    "timePeriod": "DAY",
    "type": "DATA_TRAFFIC",
    "action": "POWER_OFF",
    "channels": [{"type": "EMAIL", "contactGroup": "GENERAL", "contacts": []}],
}

METRICS_PAYLOAD = {
    "metrics": {
        "downPublic": {
            "values": [{"value": 1024, "timestamp": "2024-01-01T00:00:00Z"}],
            "unit": "B",
        },
        "upPublic": {"values": [], "unit": "B"},
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

#: (client method, args, kwargs, HTTP method, documented path, response status,
#: response payload) — one row per documented operation.
CONTRACT_CASES: list[tuple[Any, ...]] = [
    ("start_vps", (VPS,), {}, "POST", f"/publicCloud/v1/vps/{VPS}/start", 202, None),
    ("stop_vps", (VPS,), {}, "POST", f"/publicCloud/v1/vps/{VPS}/stop", 202, None),
    ("reboot_vps", (VPS,), {}, "POST", f"/publicCloud/v1/vps/{VPS}/reboot", 202, None),
    (
        "get_console_access",
        (VPS,),
        {},
        "GET",
        f"/publicCloud/v1/vps/{VPS}/console",
        200,
        {"url": "https://console.example/abc"},
    ),
    (
        "list_credentials",
        (VPS,),
        {},
        "GET",
        f"/publicCloud/v1/vps/{VPS}/credentials",
        200,
        _page(credentials=[{"type": "OPERATING_SYSTEM", "username": "root"}]),
    ),
    (
        "store_credential",
        (
            VPS,
            StoreCredentialRequest(
                type=CredentialType.OPERATING_SYSTEM, username="root", password=PASSWORD
            ),
        ),
        {},
        "POST",
        f"/publicCloud/v1/vps/{VPS}/credentials",
        200,
        {"type": "OPERATING_SYSTEM", "username": "root", "password": PASSWORD},
    ),
    (
        "delete_credentials",
        (VPS,),
        {},
        "DELETE",
        f"/publicCloud/v1/vps/{VPS}/credentials",
        204,
        None,
    ),
    (
        "list_credentials_by_type",
        (VPS, CredentialType.OPERATING_SYSTEM),
        {},
        "GET",
        f"/publicCloud/v1/vps/{VPS}/credentials/OPERATING_SYSTEM",
        200,
        _page(credentials=[{"type": "OPERATING_SYSTEM", "username": "root"}]),
    ),
    (
        "get_credential",
        (VPS, CredentialType.OPERATING_SYSTEM, "root"),
        {},
        "GET",
        f"/publicCloud/v1/vps/{VPS}/credentials/OPERATING_SYSTEM/root",
        200,
        {"type": "OPERATING_SYSTEM", "username": "root", "password": PASSWORD},
    ),
    (
        "update_credential",
        (VPS, CredentialType.OPERATING_SYSTEM, "root", UpdateCredentialRequest(password=PASSWORD)),
        {},
        "PUT",
        f"/publicCloud/v1/vps/{VPS}/credentials/OPERATING_SYSTEM/root",
        200,
        {"type": "OPERATING_SYSTEM", "username": "root", "password": PASSWORD},
    ),
    (
        "delete_credential",
        (VPS, CredentialType.OPERATING_SYSTEM, "root"),
        {},
        "DELETE",
        f"/publicCloud/v1/vps/{VPS}/credentials/OPERATING_SYSTEM/root",
        204,
        None,
    ),
    (
        "reset_password",
        (VPS,),
        {},
        "POST",
        f"/publicCloud/v1/vps/{VPS}/resetPassword",
        202,
        None,
    ),
    ("attach_iso", (VPS, "GRML"), {}, "POST", f"/publicCloud/v1/vps/{VPS}/attachIso", 202, None),
    ("detach_iso", (VPS,), {}, "POST", f"/publicCloud/v1/vps/{VPS}/detachIso", 202, None),
    (
        "list_reinstall_images",
        (VPS,),
        {},
        "GET",
        f"/publicCloud/v1/vps/{VPS}/reinstall/images",
        200,
        _page(images=[{"id": "UBUNTU_22_04_64BIT", "name": "Ubuntu 22.04", "family": "linux"}]),
    ),
    (
        "reinstall",
        (VPS, ReinstallRequest(image_id="UBUNTU_22_04_64BIT")),
        {},
        "PUT",
        f"/publicCloud/v1/vps/{VPS}/reinstall",
        202,
        None,
    ),
    (
        "list_ips",
        (VPS,),
        {},
        "GET",
        f"/publicCloud/v1/vps/{VPS}/ips",
        200,
        _page(ips=[IP_PAYLOAD]),
    ),
    (
        "get_ip",
        (VPS, "1.2.3.4"),
        {},
        "GET",
        f"/publicCloud/v1/vps/{VPS}/ips/1.2.3.4",
        200,
        IP_PAYLOAD,
    ),
    (
        "update_ip",
        (VPS, "1.2.3.4", UpdateIpRequest(reverse_lookup="a-valid-domain.xpto")),
        {},
        "PUT",
        f"/publicCloud/v1/vps/{VPS}/ips/1.2.3.4",
        200,
        IP_PAYLOAD,
    ),
    (
        "null_route_ip",
        (VPS, "1.2.3.4", NullRouteIpRequest(comment="ddos", automated_unnuling_at=2)),
        {},
        "POST",
        f"/publicCloud/v1/vps/{VPS}/ips/1.2.3.4/null",
        200,
        IP_PAYLOAD,
    ),
    (
        "remove_ip_null_route",
        (VPS, "1.2.3.4"),
        {},
        "POST",
        f"/publicCloud/v1/vps/{VPS}/ips/1.2.3.4/unnull",
        200,
        IP_PAYLOAD,
    ),
    (
        "get_data_traffic_metrics",
        (VPS,),
        {"from_": "2024-01-01", "to": "2024-01-02", "granularity": "DAY"},
        "GET",
        f"/publicCloud/v1/vps/{VPS}/metrics/datatraffic",
        200,
        METRICS_PAYLOAD,
    ),
    (
        "list_snapshots",
        (VPS,),
        {},
        "GET",
        f"/publicCloud/v1/vps/{VPS}/snapshots",
        200,
        _page(snapshots=[SNAPSHOT_PAYLOAD]),
    ),
    (
        "create_snapshot",
        (VPS, CreateSnapshotRequest(name="my snapshot")),
        {},
        "POST",
        f"/publicCloud/v1/vps/{VPS}/snapshots",
        202,
        None,
    ),
    (
        "get_snapshot",
        (VPS, SNAPSHOT_ID),
        {},
        "GET",
        f"/publicCloud/v1/vps/{VPS}/snapshots/{SNAPSHOT_ID}",
        200,
        SNAPSHOT_PAYLOAD,
    ),
    (
        "restore_snapshot",
        (VPS, SNAPSHOT_ID),
        {},
        "PUT",
        f"/publicCloud/v1/vps/{VPS}/snapshots/{SNAPSHOT_ID}",
        202,
        None,
    ),
    (
        "delete_snapshot",
        (VPS, SNAPSHOT_ID),
        {},
        "DELETE",
        f"/publicCloud/v1/vps/{VPS}/snapshots/{SNAPSHOT_ID}",
        202,
        None,
    ),
    ("list_vps", (), {}, "GET", "/publicCloud/v1/vps/", 200, _page(vps=[VPS_SUMMARY])),
    ("get_vps", (VPS,), {}, "GET", f"/publicCloud/v1/vps/{VPS}", 200, VPS_DETAIL),
    (
        "update_vps",
        (VPS, UpdateVpsRequest(reference="updated reference")),
        {},
        "PUT",
        f"/publicCloud/v1/vps/{VPS}",
        200,
        VPS_DETAIL,
    ),
    (
        "list_isos",
        (),
        {},
        "GET",
        "/publicCloud/v1/vps/isos",
        200,
        _page(isos=[{"id": "GRML", "name": "GRML Rescue"}]),
    ),
    (
        "list_data_traffic_notification_settings",
        (VPS,),
        {},
        "GET",
        f"/publicCloud/v1/vps/{VPS}/notificationSettings/dataTraffic",
        200,
        _page(notificationSettings=[NOTIFICATION_PAYLOAD]),
    ),
    (
        "get_data_traffic_notification_setting",
        (VPS, NOTIFICATION_ID),
        {},
        "GET",
        f"/publicCloud/v1/vps/{VPS}/notificationSettings/dataTraffic/{NOTIFICATION_ID}",
        200,
        NOTIFICATION_PAYLOAD,
    ),
    (
        "create_data_traffic_notification_setting",
        (
            VPS,
            NOTIFICATION_ID,
            CreateNotificationSettingRequest(
                threshold={"value": 1000, "unit": "GB"},
                time_period="DAY",
                action="POWER_OFF",
                channels=[{"type": "EMAIL", "contact_group": "GENERAL"}],
            ),
        ),
        {},
        "POST",
        f"/publicCloud/v1/vps/{VPS}/notificationSettings/dataTraffic/{NOTIFICATION_ID}",
        201,
        NOTIFICATION_PAYLOAD,
    ),
    (
        "update_data_traffic_notification_setting",
        (VPS, NOTIFICATION_ID, UpdateNotificationSettingRequest(time_period="MONTH")),
        {},
        "PUT",
        f"/publicCloud/v1/vps/{VPS}/notificationSettings/dataTraffic/{NOTIFICATION_ID}",
        200,
        NOTIFICATION_PAYLOAD,
    ),
    (
        "delete_data_traffic_notification_setting",
        (VPS, NOTIFICATION_ID),
        {},
        "DELETE",
        f"/publicCloud/v1/vps/{VPS}/notificationSettings/dataTraffic/{NOTIFICATION_ID}",
        204,
        None,
    ),
    (
        "get_monitoring_status",
        (VPS,),
        {},
        "GET",
        f"/publicCloud/v1/vps/{VPS}/monitoring/status",
        200,
        {"status": "UP", "description": "Service is Up"},
    ),
    (
        "enable_monitoring",
        (VPS,),
        {},
        "POST",
        f"/publicCloud/v1/vps/{VPS}/monitoring/enable",
        204,
        None,
    ),
]


@pytest.mark.parametrize(
    ("client_method", "args", "kwargs", "http_method", "path", "status", "payload"),
    CONTRACT_CASES,
    ids=[case[0] for case in CONTRACT_CASES],
)
@respx.mock
async def test_documented_operation_contract(
    client_method: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    http_method: str,
    path: str,
    status: int,
    payload: Any,
) -> None:
    """Each operation sends the documented method/path and parses the response."""
    route = respx.route(method=http_method, url=f"{BASE}{path}").mock(
        return_value=httpx.Response(status, json=payload)
        if payload is not None
        else httpx.Response(status)
    )
    api = _api()
    try:
        result = await getattr(api, client_method)(*args, **kwargs)
    finally:
        await api.aclose()
    assert route.called, f"{client_method} did not call {http_method} {path}"
    request = route.calls.last.request
    assert request.method == http_method
    # Contract: plain API key in X-LSW-Auth, never a Bearer token.
    assert request.headers["X-LSW-Auth"] == KEY
    assert "authorization" not in {k.lower() for k in request.headers}
    if status == 204:
        assert result is None
    else:
        assert result is not None


def test_contract_cases_cover_every_operation() -> None:
    """The case table covers all 38 documented VPS operations exactly once."""
    documented = {
        "start_vps",
        "stop_vps",
        "reboot_vps",
        "get_console_access",
        "list_credentials",
        "store_credential",
        "delete_credentials",
        "list_credentials_by_type",
        "get_credential",
        "update_credential",
        "delete_credential",
        "reset_password",
        "attach_iso",
        "detach_iso",
        "list_reinstall_images",
        "reinstall",
        "list_ips",
        "get_ip",
        "update_ip",
        "null_route_ip",
        "remove_ip_null_route",
        "get_data_traffic_metrics",
        "list_snapshots",
        "create_snapshot",
        "get_snapshot",
        "restore_snapshot",
        "delete_snapshot",
        "list_vps",
        "get_vps",
        "update_vps",
        "list_isos",
        "list_data_traffic_notification_settings",
        "get_data_traffic_notification_setting",
        "create_data_traffic_notification_setting",
        "update_data_traffic_notification_setting",
        "delete_data_traffic_notification_setting",
        "get_monitoring_status",
        "enable_monitoring",
    }
    covered = [case[0] for case in CONTRACT_CASES]
    assert len(covered) == 38
    assert set(covered) == documented


class TestMutationMethods:
    """Phase 13: methods must match the documentation, not another provider."""

    @pytest.mark.parametrize(
        ("method", "verb", "suffix", "status"),
        [
            ("start_vps", "POST", "start", 202),
            ("stop_vps", "POST", "stop", 202),
            ("reboot_vps", "POST", "reboot", 202),
            ("reset_password", "POST", "resetPassword", 202),
            ("attach_iso", "POST", "attachIso", 202),
            ("detach_iso", "POST", "detachIso", 202),
            ("reinstall", "PUT", "reinstall", 202),
            ("create_snapshot", "POST", "snapshots", 202),
        ],
        ids=[
            "start",
            "stop",
            "reboot",
            "resetPassword",
            "attachIso",
            "detachIso",
            "reinstall",
            "createSnapshot",
        ],
    )
    @respx.mock
    async def test_action_uses_documented_verb(
        self, method: str, verb: str, suffix: str, status: int
    ) -> None:
        route = respx.route(
            method=verb, url__regex=rf"{BASE}/publicCloud/v1/vps/{VPS}/{suffix}$"
        ).mock(return_value=httpx.Response(status))
        api = _api()
        extra: tuple[Any, ...] = ()
        if method in {"attach_iso"}:
            extra = ("GRML",)
        if method in {"reinstall"}:
            extra = (ReinstallRequest(image_id="UBUNTU_22_04_64BIT"),)
        if method in {"create_snapshot"}:
            extra = (CreateSnapshotRequest(name="snap"),)
        try:
            await getattr(api, method)(VPS, *extra)
        finally:
            await api.aclose()
        assert route.called

    @respx.mock
    async def test_snapshot_restore_and_delete_use_put_and_delete(self) -> None:
        restore = respx.put(f"{BASE}/publicCloud/v1/vps/{VPS}/snapshots/{SNAPSHOT_ID}").mock(
            return_value=httpx.Response(202)
        )
        delete = respx.delete(f"{BASE}/publicCloud/v1/vps/{VPS}/snapshots/{SNAPSHOT_ID}").mock(
            return_value=httpx.Response(202)
        )
        api = _api()
        try:
            await api.restore_snapshot(VPS, SNAPSHOT_ID)
            await api.delete_snapshot(VPS, SNAPSHOT_ID)
        finally:
            await api.aclose()
        assert restore.calls.last.request.method == "PUT"
        assert delete.calls.last.request.method == "DELETE"

    @respx.mock
    async def test_ip_null_route_and_unnull_use_post(self) -> None:
        null = respx.post(f"{BASE}/publicCloud/v1/vps/{VPS}/ips/1.2.3.4/null").mock(
            return_value=httpx.Response(200, json=IP_PAYLOAD)
        )
        unnull = respx.post(f"{BASE}/publicCloud/v1/vps/{VPS}/ips/1.2.3.4/unnull").mock(
            return_value=httpx.Response(200, json=IP_PAYLOAD)
        )
        api = _api()
        try:
            await api.null_route_ip(VPS, "1.2.3.4")
            await api.remove_ip_null_route(VPS, "1.2.3.4")
        finally:
            await api.aclose()
        assert null.calls.last.request.method == "POST"
        assert unnull.calls.last.request.method == "POST"

    @respx.mock
    async def test_credential_update_and_delete_use_put_and_delete(self) -> None:
        update = respx.put(f"{BASE}/publicCloud/v1/vps/{VPS}/credentials/CONTROL_PANEL/admin").mock(
            return_value=httpx.Response(200, json={"type": "CONTROL_PANEL", "username": "admin"})
        )
        delete = respx.delete(
            f"{BASE}/publicCloud/v1/vps/{VPS}/credentials/CONTROL_PANEL/admin"
        ).mock(return_value=httpx.Response(204))
        api = _api()
        try:
            await api.update_credential(
                VPS, "CONTROL_PANEL", "admin", UpdateCredentialRequest(password=PASSWORD)
            )
            await api.delete_credential(VPS, "CONTROL_PANEL", "admin")
        finally:
            await api.aclose()
        assert update.calls.last.request.method == "PUT"
        assert delete.calls.last.request.method == "DELETE"

    @respx.mock
    async def test_notification_settings_use_documented_methods_and_client_supplied_id(
        self,
    ) -> None:
        router = [
            respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}/notificationSettings/dataTraffic").mock(
                return_value=httpx.Response(200, json=_page(notificationSettings=[]))
            ),
            respx.get(
                f"{BASE}/publicCloud/v1/vps/{VPS}/notificationSettings/dataTraffic/{NOTIFICATION_ID}"
            ).mock(return_value=httpx.Response(200, json=NOTIFICATION_PAYLOAD)),
            respx.post(
                f"{BASE}/publicCloud/v1/vps/{VPS}/notificationSettings/dataTraffic/{NOTIFICATION_ID}"
            ).mock(return_value=httpx.Response(201, json=NOTIFICATION_PAYLOAD)),
            respx.put(
                f"{BASE}/publicCloud/v1/vps/{VPS}/notificationSettings/dataTraffic/{NOTIFICATION_ID}"
            ).mock(return_value=httpx.Response(200, json=NOTIFICATION_PAYLOAD)),
            respx.delete(
                f"{BASE}/publicCloud/v1/vps/{VPS}/notificationSettings/dataTraffic/{NOTIFICATION_ID}"
            ).mock(return_value=httpx.Response(204)),
        ]
        api = _api()
        create = CreateNotificationSettingRequest(
            threshold={"value": 1000, "unit": "GB"},
            time_period="DAY",
            action="POWER_OFF",
            channels=[{"type": "EMAIL", "contact_group": "GENERAL"}],
        )
        try:
            await api.list_data_traffic_notification_settings(VPS)
            await api.get_data_traffic_notification_setting(VPS, NOTIFICATION_ID)
            created = await api.create_data_traffic_notification_setting(
                VPS, NOTIFICATION_ID, create
            )
            await api.update_data_traffic_notification_setting(
                VPS, NOTIFICATION_ID, UpdateNotificationSettingRequest(time_period="WEEK")
            )
            await api.delete_data_traffic_notification_setting(VPS, NOTIFICATION_ID)
        finally:
            await api.aclose()
        assert created.id == NOTIFICATION_ID
        assert [route.calls.last.request.method for route in router] == [
            "GET",
            "GET",
            "POST",
            "PUT",
            "DELETE",
        ]
        assert router[2].calls.last.request.url.path.endswith(f"/{NOTIFICATION_ID}")


class TestRequestBodySerialization:
    """The documented bodies are sent with the documented (camelCase) names."""

    @respx.mock
    async def test_store_credential_body_and_secrecy(self) -> None:
        route = respx.post(f"{BASE}/publicCloud/v1/vps/{VPS}/credentials").mock(
            return_value=httpx.Response(
                200, json={"type": "OPERATING_SYSTEM", "username": "root", "password": PASSWORD}
            )
        )
        api = _api()
        request = StoreCredentialRequest(
            type=CredentialType.OPERATING_SYSTEM, username="root", password=PASSWORD
        )
        assert PASSWORD not in repr(request)
        try:
            result = await api.store_credential(VPS, request)
        finally:
            await api.aclose()
        body = json.loads(route.calls.last.request.content)
        assert body == {"type": "OPERATING_SYSTEM", "username": "root", "password": PASSWORD}
        assert result is not None and PASSWORD not in repr(result)

    @respx.mock
    async def test_update_credential_sends_only_password(self) -> None:
        route = respx.put(
            f"{BASE}/publicCloud/v1/vps/{VPS}/credentials/OPERATING_SYSTEM/root"
        ).mock(return_value=httpx.Response(200, json={"type": "OPERATING_SYSTEM"}))
        api = _api()
        try:
            await api.update_credential(
                VPS, "OPERATING_SYSTEM", "root", UpdateCredentialRequest(password=PASSWORD)
            )
        finally:
            await api.aclose()
        assert json.loads(route.calls.last.request.content) == {"password": PASSWORD}

    @respx.mock
    async def test_attach_iso_body_and_absent_detach_body(self) -> None:
        attach = respx.post(f"{BASE}/publicCloud/v1/vps/{VPS}/attachIso").mock(
            return_value=httpx.Response(202)
        )
        detach = respx.post(f"{BASE}/publicCloud/v1/vps/{VPS}/detachIso").mock(
            return_value=httpx.Response(202)
        )
        api = _api()
        try:
            await api.attach_iso(VPS, "GRML")
            await api.detach_iso(VPS)
        finally:
            await api.aclose()
        assert json.loads(attach.calls.last.request.content) == {"isoId": "GRML"}
        assert not detach.calls.last.request.content

    @respx.mock
    async def test_null_route_body_keeps_documented_typo(self) -> None:
        route = respx.post(f"{BASE}/publicCloud/v1/vps/{VPS}/ips/1.2.3.4/null").mock(
            return_value=httpx.Response(200, json=IP_PAYLOAD)
        )
        api = _api()
        try:
            await api.null_route_ip(
                VPS,
                "1.2.3.4",
                NullRouteIpRequest(comment="Getting DDoS", automated_unnuling_at=2),
            )
        finally:
            await api.aclose()
        body = json.loads(route.calls.last.request.content)
        assert body == {"comment": "Getting DDoS", "automatedUnnulingAt": 2}

    @respx.mock
    async def test_reinstall_body_maps_image_and_market_app(self) -> None:
        route = respx.put(f"{BASE}/publicCloud/v1/vps/{VPS}/reinstall").mock(
            return_value=httpx.Response(202)
        )
        api = _api()
        try:
            await api.reinstall(
                VPS, ReinstallRequest(image_id="UBUNTU_22_04_64BIT", market_app_id="CPANEL_30")
            )
        finally:
            await api.aclose()
        assert json.loads(route.calls.last.request.content) == {
            "imageId": "UBUNTU_22_04_64BIT",
            "marketAppId": "CPANEL_30",
        }

    @respx.mock
    async def test_update_vps_sends_reference(self) -> None:
        route = respx.put(f"{BASE}/publicCloud/v1/vps/{VPS}").mock(
            return_value=httpx.Response(200, json=VPS_DETAIL)
        )
        api = _api()
        try:
            await api.update_vps(VPS, reference="updated reference")
        finally:
            await api.aclose()
        assert json.loads(route.calls.last.request.content) == {"reference": "updated reference"}

    @respx.mock
    async def test_notification_create_body_matches_documentation(self) -> None:
        route = respx.post(
            f"{BASE}/publicCloud/v1/vps/{VPS}/notificationSettings/dataTraffic/{NOTIFICATION_ID}"
        ).mock(return_value=httpx.Response(201, json=NOTIFICATION_PAYLOAD))
        api = _api()
        request = CreateNotificationSettingRequest(
            threshold={"value": 1000, "unit": "GB"},
            time_period="DAY",
            action="POWER_OFF",
            channels=[{"type": "EMAIL", "contact_group": "GENERAL"}],
        )
        try:
            await api.create_data_traffic_notification_setting(VPS, NOTIFICATION_ID, request)
        finally:
            await api.aclose()
        assert json.loads(route.calls.last.request.content) == {
            "threshold": {"value": 1000, "unit": "GB"},
            "timePeriod": "DAY",
            "action": "POWER_OFF",
            "channels": [{"type": "EMAIL", "contactGroup": "GENERAL"}],
        }


class TestQueryParameters:
    """Documented filters are sent with their documented names and values."""

    @respx.mock
    async def test_list_vps_sends_every_documented_filter(self) -> None:
        route = respx.get(f"{BASE}/publicCloud/v1/vps/").mock(
            return_value=httpx.Response(200, json=_page(vps=[VPS_SUMMARY]))
        )
        api = _api()
        try:
            await api.list_vps(
                limit=25,
                offset=50,
                vps_id="123581321",
                reference="ref-1",
                ip="1.2.3.4",
                state="RUNNING",
                pack="Leaseweb VPS 2",
                region="eu-west-3",
            )
        finally:
            await api.aclose()
        params = route.calls.last.request.url.params
        assert dict(params) == {
            "limit": "25",
            "offset": "50",
            "id": "123581321",
            "reference": "ref-1",
            "ip": "1.2.3.4",
            "state": "RUNNING",
            "pack": "Leaseweb VPS 2",
            "region": "eu-west-3",
        }

    @respx.mock
    async def test_list_vps_without_filters_sends_no_query(self) -> None:
        route = respx.get(f"{BASE}/publicCloud/v1/vps/").mock(
            return_value=httpx.Response(200, json=_page(vps=[]))
        )
        api = _api()
        try:
            await api.list_vps()
        finally:
            await api.aclose()
        assert not route.calls.last.request.url.query

    @respx.mock
    async def test_list_ips_sends_documented_filters(self) -> None:
        route = respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}/ips").mock(
            return_value=httpx.Response(200, json=_page(ips=[]))
        )
        api = _api()
        try:
            await api.list_ips(VPS, version=4, null_routed=False, ips="1.2.3.4|5.6.7.8")
        finally:
            await api.aclose()
        assert dict(route.calls.last.request.url.params) == {
            "version": "4",
            "nullRouted": "false",
            "ips": "1.2.3.4|5.6.7.8",
        }

    @respx.mock
    async def test_reinstall_images_documented_filters(self) -> None:
        route = respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}/reinstall/images").mock(
            return_value=httpx.Response(200, json=_page(images=[]))
        )
        api = _api()
        try:
            await api.list_reinstall_images(VPS, limit=10, offset=5, standard=True)
        finally:
            await api.aclose()
        assert dict(route.calls.last.request.url.params) == {
            "limit": "10",
            "offset": "5",
            "standard": "true",
        }

    @respx.mock
    async def test_data_traffic_metrics_require_documented_parameters(self) -> None:
        route = respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}/metrics/datatraffic").mock(
            return_value=httpx.Response(200, json=METRICS_PAYLOAD)
        )
        api = _api()
        try:
            metrics = await api.get_data_traffic_metrics(
                VPS,
                from_="2024-08-23T00:00:00Z",
                to="2024-09-03T00:00:00Z",
                granularity="5m",
                aggregation="SUM",
            )
        finally:
            await api.aclose()
        assert dict(route.calls.last.request.url.params) == {
            "from": "2024-08-23T00:00:00Z",
            "to": "2024-09-03T00:00:00Z",
            "granularity": "5m",
            "aggregation": "SUM",
        }
        # Provider facts are preserved verbatim.
        assert metrics.from_ == "2024-01-01T00:00:00Z"
        assert metrics.granularity == "DAY"
        assert metrics.aggregation == "SUM"
        assert metrics.unit == "B"
        assert metrics.down_public is not None
        point = metrics.down_public.values[0]
        assert point.value == 1024
        assert point.timestamp == "2024-01-01T00:00:00Z"
        assert point.timestamp_dt is not None and point.timestamp_dt.year == 2024
        summary = metrics.summary["downPublic"]
        assert summary.total == 1024 and summary.average == 512 and summary.expected == 1024
        assert summary.peak is not None and summary.peak.value == 2048
        assert metrics.total_bytes() == 1024

    @respx.mock
    async def test_snapshot_and_notification_pagination_parameters(self) -> None:
        snapshots = respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}/snapshots").mock(
            return_value=httpx.Response(200, json=_page(snapshots=[]))
        )
        notifications = respx.get(
            f"{BASE}/publicCloud/v1/vps/{VPS}/notificationSettings/dataTraffic"
        ).mock(return_value=httpx.Response(200, json=_page(notificationSettings=[])))
        isos = respx.get(f"{BASE}/publicCloud/v1/vps/isos").mock(
            return_value=httpx.Response(200, json=_page(isos=[]))
        )
        api = _api()
        try:
            await api.list_snapshots(VPS, limit=10, offset=20)
            await api.list_data_traffic_notification_settings(VPS, limit=5, offset=5)
            await api.list_isos(limit=3, offset=6)
        finally:
            await api.aclose()
        assert dict(snapshots.calls.last.request.url.params) == {"limit": "10", "offset": "20"}
        assert dict(notifications.calls.last.request.url.params) == {"limit": "5", "offset": "5"}
        assert dict(isos.calls.last.request.url.params) == {"limit": "3", "offset": "6"}

    @respx.mock
    async def test_credential_list_operations_send_no_undocumented_pagination(self) -> None:
        plain = respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}/credentials").mock(
            return_value=httpx.Response(200, json=_page(credentials=[]))
        )
        by_type = respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}/credentials/OPERATING_SYSTEM").mock(
            return_value=httpx.Response(200, json=_page(credentials=[]))
        )
        api = _api()
        try:
            await api.list_credentials(VPS)
            await api.list_credentials_by_type(VPS, "OPERATING_SYSTEM")
        finally:
            await api.aclose()
        assert not plain.calls.last.request.url.query
        assert not by_type.calls.last.request.url.query


class TestPathParameterSafety:
    """Hostile path parameters can never change the request shape."""

    @respx.mock
    async def test_username_is_percent_encoded(self) -> None:
        route = respx.route(
            method="GET",
            url__regex=rf"{BASE}/publicCloud/v1/vps/{VPS}/credentials/OPERATING_SYSTEM/.*",
        ).mock(
            return_value=httpx.Response(
                200, json={"type": "OPERATING_SYSTEM", "username": "x", "password": "y"}
            )
        )
        api = _api()
        try:
            await api.get_credential(VPS, "OPERATING_SYSTEM", "root/../../admin?x=1&y=2")
        finally:
            await api.aclose()
        url = route.calls.last.request.url
        # ``URL.path`` is the DECODED form, so inspect the wire form: every
        # hostile character (`/`, `?`, `&`) must arrive percent-encoded inside
        # a single path segment, which makes traversal impossible.
        raw = url.raw_path.decode()
        assert raw.endswith("/root%2F..%2F..%2Fadmin%3Fx%3D1%26y%3D2")
        assert "%3F" in raw and "%26" in raw
        # publicCloud / v1 / vps / {vpsId} / credentials / {type} / {username}
        assert raw.count("/") == 7
        assert not url.query

    async def test_invalid_ip_is_rejected_before_any_request(self) -> None:
        api = _api()
        try:
            with pytest.raises(LeasewebValidationError):
                await api.get_ip(VPS, "../../etc/passwd")
            with pytest.raises(LeasewebValidationError):
                await api.get_ip(VPS, "999.999.999.999")
            with pytest.raises(LeasewebValidationError):
                await api.update_ip(VPS, "not-an-ip", UpdateIpRequest(reverse_lookup="x"))
            with pytest.raises(LeasewebValidationError):
                await api.list_vps(ip="1.2.3.4/../..")
        finally:
            await api.aclose()

    async def test_invalid_uuid_is_rejected_before_any_request(self) -> None:
        api = _api()
        try:
            with pytest.raises(LeasewebValidationError):
                await api.get_snapshot(VPS, "../../secrets")
            with pytest.raises(LeasewebValidationError):
                await api.restore_snapshot(VPS, "not-a-uuid")
            with pytest.raises(LeasewebValidationError):
                await api.get_data_traffic_notification_setting(VPS, "' OR 1=1")
        finally:
            await api.aclose()

    async def test_undocumented_credential_type_is_rejected(self) -> None:
        api = _api()
        try:
            with pytest.raises(LeasewebValidationError):
                await api.get_credential(VPS, "FIREWALL", "root")
            with pytest.raises(LeasewebValidationError):
                await api.list_credentials_by_type(VPS, "VNC")
        finally:
            await api.aclose()

    async def test_empty_username_is_rejected(self) -> None:
        api = _api()
        try:
            with pytest.raises(LeasewebValidationError):
                await api.get_credential(VPS, "OPERATING_SYSTEM", "   ")
        finally:
            await api.aclose()

    async def test_ip_path_preserves_the_caller_text(self) -> None:
        """A valid address is not silently rewritten (no normalization)."""
        api = _api()
        assert api._ip("1.2.3.4") == "1.2.3.4"
        assert api._ip("2001:0db8::0001") == "2001:0db8::0001"
        with pytest.raises(LeasewebValidationError):
            api._ip("")


class TestErrorMapping:
    """Documented error statuses map onto the Leaseweb hierarchy."""

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (401, LeasewebAuthenticationError),
            (403, LeasewebForbiddenError),
            (404, LeasewebNotFoundError),
            (400, LeasewebValidationError),
            (422, LeasewebValidationError),
            (429, LeasewebRateLimitError),
            (500, LeasewebServerError),
            (503, LeasewebServerError),
        ],
    )
    @respx.mock
    async def test_read_error_mapping(self, status: int, expected: type[Exception]) -> None:
        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}").mock(
            return_value=httpx.Response(
                status,
                json={"errorCode": "E-1", "errorMessage": "nope", "correlationId": "corr-1"},
            )
        )
        api = _api()
        try:
            with pytest.raises(expected) as excinfo:
                await api.get_vps(VPS)
        finally:
            await api.aclose()
        error = excinfo.value
        assert getattr(error, "correlation_id", None) == "corr-1"
        assert getattr(error, "error_code", None) == "E-1"
        assert KEY not in str(error)

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (401, LeasewebAuthenticationError),
            (403, LeasewebForbiddenError),
            (404, LeasewebNotFoundError),
            (400, LeasewebValidationError),
            (409, LeasewebConflictError),
        ],
    )
    @respx.mock
    async def test_mutation_definitive_rejection_mapping(
        self, status: int, expected: type[Exception]
    ) -> None:
        respx.post(f"{BASE}/publicCloud/v1/vps/{VPS}/start").mock(
            return_value=httpx.Response(status, json={"errorMessage": "rejected"})
        )
        api = _api()
        try:
            with pytest.raises(expected):
                await api.start_vps(VPS)
        finally:
            await api.aclose()

    @respx.mock
    async def test_read_rate_limit_is_retried_then_succeeds(self) -> None:
        route = respx.get(f"{BASE}/publicCloud/v1/vps/").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "0"}, json={"errorMessage": "slow"}),
                httpx.Response(200, json=_page(vps=[VPS_SUMMARY])),
            ]
        )
        api = _api()
        try:
            page = await api.list_vps()
        finally:
            await api.aclose()
        assert route.call_count == 2
        assert len(page.items) == 1

    @respx.mock
    async def test_mutating_429_is_ambiguous_and_never_retried(self) -> None:
        route = respx.post(f"{BASE}/publicCloud/v1/vps/{VPS}/start").mock(
            return_value=httpx.Response(429, json={"errorMessage": "slow down"})
        )
        api = _api()
        try:
            with pytest.raises(LeasewebAmbiguousMutationError) as excinfo:
                await api.start_vps(VPS)
        finally:
            await api.aclose()
        assert route.call_count == 1  # never re-sent
        assert isinstance(excinfo.value, ProviderOutcomeUnknown)

    @respx.mock
    async def test_mutating_5xx_is_ambiguous_and_never_retried(self) -> None:
        route = respx.post(f"{BASE}/publicCloud/v1/vps/{VPS}/reboot").mock(
            return_value=httpx.Response(503, json={"errorMessage": "down"})
        )
        api = _api()
        try:
            with pytest.raises(LeasewebAmbiguousMutationError):
                await api.reboot_vps(VPS)
        finally:
            await api.aclose()
        assert route.call_count == 1

    @respx.mock
    async def test_read_timeout_on_mutation_is_ambiguous(self) -> None:
        # Reinstall is documented as PUT (mutation method test):
        # a read timeout after transmission is an UNKNOWN outcome.
        route = respx.put(f"{BASE}/publicCloud/v1/vps/{VPS}/reinstall").mock(
            side_effect=httpx.ReadTimeout("no response")
        )
        api = _api()
        try:
            with pytest.raises(LeasewebAmbiguousMutationError):
                await api.reinstall(VPS, ReinstallRequest(image_id="UBUNTU_22_04_64BIT"))
        finally:
            await api.aclose()
        assert route.call_count == 1

    @respx.mock
    async def test_connect_error_on_mutation_proves_non_transmission(self) -> None:
        respx.post(f"{BASE}/publicCloud/v1/vps/{VPS}/start").mock(
            side_effect=httpx.ConnectError("refused")
        )
        api = _api()
        try:
            with pytest.raises(LeasewebUnavailableError):
                await api.start_vps(VPS)
        finally:
            await api.aclose()

    @respx.mock
    async def test_read_timeout_maps_to_timeout_error(self) -> None:
        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}").mock(side_effect=httpx.ReadTimeout("slow"))
        api = _api()
        try:
            with pytest.raises(LeasewebTimeoutError):
                await api.get_vps(VPS)
        finally:
            await api.aclose()

    @respx.mock
    async def test_5xx_read_maps_to_server_error(self) -> None:
        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}").mock(
            return_value=httpx.Response(500, json={"errorMessage": "boom"})
        )
        api = _api()
        try:
            with pytest.raises(LeasewebServerError):
                await api.get_vps(VPS)
        finally:
            await api.aclose()

    @respx.mock
    async def test_malformed_success_body_is_a_response_error(self) -> None:
        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}").mock(
            return_value=httpx.Response(200, json={"unexpected": True})
        )
        api = _api()
        try:
            with pytest.raises(LeasewebResponseError) as excinfo:
                await api.get_vps(VPS)
        finally:
            await api.aclose()
        assert "provider response does not match" in str(excinfo.value)

    def test_legacy_error_hierarchy_stays_compatible(self) -> None:
        """The new hierarchy is a subclass of the existing provider errors."""
        assert issubclass(LeasewebAuthenticationError, ProviderError)
        assert issubclass(LeasewebNotFoundError, ProviderNotFound)
        assert issubclass(LeasewebRateLimitError, ProviderRateLimited)
        assert issubclass(LeasewebServerError, ProviderUnavailable)
        assert issubclass(LeasewebConflictError, ProviderConflict)
        assert issubclass(LeasewebAmbiguousMutationError, ProviderOutcomeUnknown)


class TestPagination:
    """Reusable pagination over the documented ``_metadata`` envelope."""

    @respx.mock
    async def test_iter_vps_follows_metadata(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            offset = int(request.url.params.get("offset", "0"))
            rows = [VPS_SUMMARY] if offset == 0 else [{**VPS_SUMMARY, "id": "VPS02_2"}]
            return httpx.Response(
                200,
                json={
                    "vps": rows,
                    "_metadata": {"totalCount": 2, "offset": offset, "limit": 1},
                },
            )

        route = respx.get(f"{BASE}/publicCloud/v1/vps/").mock(side_effect=handler)
        api = _api()
        try:
            items = [item async for item in api.iter_vps(page_size=1)]
        finally:
            await api.aclose()
        assert [item.id for item in items] == [VPS, "VPS02_2"]
        assert route.call_count == 2

    @respx.mock
    async def test_iter_stops_on_empty_page(self) -> None:
        route = respx.get(f"{BASE}/publicCloud/v1/vps/").mock(
            return_value=httpx.Response(
                200, json={"_metadata": {"totalCount": 0, "offset": 0, "limit": 100}, "vps": []}
            )
        )
        api = _api()
        try:
            assert await api.all_vps() == []
        finally:
            await api.aclose()
        assert route.call_count == 1

    @respx.mock
    async def test_page_metadata_is_typed(self) -> None:
        respx.get(f"{BASE}/publicCloud/v1/vps/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "vps": [VPS_SUMMARY],
                    "_metadata": {"totalCount": 7, "offset": 0, "limit": 1},
                },
            )
        )
        api = _api()
        try:
            page = await api.list_vps_page(limit=1)
        finally:
            await api.aclose()
        assert page.metadata is not None
        assert page.metadata.total_count == 7
        assert page.has_more is True
        assert page.total_count == 7


class TestUnknownValueCompatibility:
    """Unknown provider values survive verbatim (forward compatibility)."""

    @respx.mock
    async def test_unknown_vps_state_is_preserved(self) -> None:
        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}").mock(
            return_value=httpx.Response(200, json={**VPS_DETAIL, "state": "MIGRATING"})
        )
        api = _api()
        try:
            detail = await api.get_vps(VPS)
        finally:
            await api.aclose()
        assert str(detail.state) == "MIGRATING"
        assert detail.state.is_documented is False

    @respx.mock
    async def test_unknown_monitoring_status_is_preserved(self) -> None:
        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}/monitoring/status").mock(
            return_value=httpx.Response(200, json={"status": "DEGRADED", "description": "new"})
        )
        api = _api()
        try:
            status = await api.get_monitoring_status(VPS)
        finally:
            await api.aclose()
        assert str(status.status) == "DEGRADED"

    @respx.mock
    async def test_unknown_snapshot_state_is_preserved(self) -> None:
        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}/snapshots/{SNAPSHOT_ID}").mock(
            return_value=httpx.Response(
                200, json={**SNAPSHOT_PAYLOAD, "state": "DELETING", "extraField": 1}
            )
        )
        api = _api()
        try:
            snapshot = await api.get_snapshot(VPS, SNAPSHOT_ID)
        finally:
            await api.aclose()
        assert str(snapshot.state) == "DELETING"
        # Forward compatibility: unknown provider fields survive on the DTO.
        assert snapshot.model_extra is not None and snapshot.model_extra["extraField"] == 1

    @respx.mock
    async def test_unknown_pack_and_region_are_preserved(self) -> None:
        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}").mock(
            return_value=httpx.Response(
                200, json={**VPS_DETAIL, "pack": "Leaseweb VPS 9", "region": "mars-central-1"}
            )
        )
        api = _api()
        try:
            detail = await api.get_vps(VPS)
        finally:
            await api.aclose()
        assert str(detail.pack) == "Leaseweb VPS 9"
        assert str(detail.region) == "mars-central-1"


class TestSecretHygiene:
    """Credentials, console URLs and API keys never leak."""

    @respx.mock
    async def test_console_url_is_secret_and_redacted(self) -> None:
        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}/console").mock(
            return_value=httpx.Response(200, json={"url": "https://console.example/token-abc"})
        )
        api = _api()
        try:
            access = await api.get_console_access(VPS)
        finally:
            await api.aclose()
        assert access.reveal() == "https://console.example/token-abc"
        assert "token-abc" not in repr(access)
        assert "token-abc" not in str(access)
        assert "**********" in repr(access)

    @respx.mock
    async def test_credential_password_never_in_repr_or_exception(self) -> None:
        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}/credentials/OPERATING_SYSTEM/root").mock(
            return_value=httpx.Response(
                200, json={"type": "OPERATING_SYSTEM", "username": "root", "password": PASSWORD}
            )
        )
        api = _api()
        try:
            credential = await api.get_credential(VPS, "OPERATING_SYSTEM", "root")
        finally:
            await api.aclose()
        assert credential.reveal_password() == PASSWORD
        assert PASSWORD not in repr(credential)
        assert PASSWORD not in str(credential)

    @respx.mock
    async def test_schema_failure_never_echoes_values(self) -> None:
        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}/credentials").mock(
            return_value=httpx.Response(
                200, json={"credentials": [{"type": "OPERATING_SYSTEM", "password": PASSWORD}]}
            )
        )
        api = _api()
        try:
            with pytest.raises(LeasewebResponseError) as excinfo:
                await api.list_credentials(VPS)
        finally:
            await api.aclose()
        assert PASSWORD not in str(excinfo.value)
        assert PASSWORD not in repr(excinfo.value)

    @respx.mock
    async def test_error_payloads_are_redacted_in_exceptions(self) -> None:
        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}").mock(
            return_value=httpx.Response(
                400,
                json={
                    "errorCode": "VALIDATION",
                    "errorMessage": f'bad body {{"password": "{PASSWORD}"}}',
                    "correlationId": "corr-9",
                },
            )
        )
        api = _api()
        try:
            with pytest.raises(LeasewebValidationError) as excinfo:
                await api.get_vps(VPS)
        finally:
            await api.aclose()
        assert PASSWORD not in str(excinfo.value)
        assert "<redacted>" in str(excinfo.value)
        assert "corr-9" in str(excinfo.value)

    @respx.mock
    async def test_api_key_never_appears_in_logs_or_metrics(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}").mock(
            return_value=httpx.Response(401, json={"errorMessage": f"bad key {KEY}"})
        )
        caplog.set_level(logging.DEBUG)
        api = _api()
        try:
            with pytest.raises(LeasewebAuthenticationError) as excinfo:
                await api.get_vps(VPS)
        finally:
            await api.aclose()
        assert KEY not in str(excinfo.value)
        assert KEY not in caplog.text
        assert KEY not in repr(excinfo.value)

    def test_redaction_helper_covers_documented_secret_shapes(self) -> None:
        assert API_KEY_SENTINEL not in redact_sensitive(f"X-LSW-Auth: {API_KEY_SENTINEL}")
        assert API_KEY_SENTINEL not in redact_sensitive(f"Authorization: Bearer {API_KEY_SENTINEL}")
        assert PASSWORD not in redact_sensitive(f'body {{"password": "{PASSWORD}"}}')
        assert PRIVATE_KEY not in redact_sensitive(PEM_BLOCK)
        assert "ssh-rsa" not in redact_sensitive("ssh-rsa AAAAB3NzaC1yc2EAAAABIwAAAQEAtest")

    def test_error_payload_parser_keeps_safe_fields_only(self) -> None:
        response = httpx.Response(
            403,
            headers={"APIGW-CORRELATION-ID": "corr-abc"},
            json={
                "errorCode": "FORBIDDEN",
                "errorMessage": "denied",
                "userMessage": "not for you",
                "reference": "ref-1",
                "errorDetails": {"field": ["a", "b"]},
                "password": PASSWORD,
            },
            request=httpx.Request("GET", f"{BASE}/x"),
        )
        payload = parse_error_payload(response)
        assert payload.correlation_id == "corr-abc"
        assert payload.error_code == "FORBIDDEN"
        assert payload.user_message == "not for you"
        assert payload.reference == "ref-1"
        assert payload.error_details == {"field": ("a", "b")}
        assert PASSWORD not in payload.summary

    def test_transport_error_messages_are_redacted(self) -> None:
        api = _api()
        error = api.transport._transport_error(
            httpx.ReadTimeout(f"timeout with X-LSW-Auth: {API_KEY_SENTINEL}"),
            endpoint="GET /x",
        )
        assert API_KEY_SENTINEL not in str(error)


class TestDestructiveClassification:
    """Destructive/operator-only classification is explicit and complete."""

    def test_classification_sets_are_consistent(self) -> None:
        assert DESTRUCTIVE_OPERATIONS <= OPERATOR_ONLY_OPERATIONS
        assert DESTRUCTIVE_OPERATIONS.isdisjoint(CUSTOMER_EXPOSABLE_OPERATIONS)
        assert {
            "delete_credential",
            "reset_password",
            "reinstall",
            "restore_snapshot",
            "delete_snapshot",
            "null_route_ip",
        } <= DESTRUCTIVE_OPERATIONS

    def test_destructive_methods_exist_on_the_client(self) -> None:
        for method in DESTRUCTIVE_OPERATIONS:
            assert callable(getattr(LeaseWebVpsApi, method, None)), method

    def test_no_destructive_operation_is_customer_exposable(self) -> None:
        for method in OPERATOR_ONLY_OPERATIONS:
            assert method not in CUSTOMER_EXPOSABLE_OPERATIONS


class TestTransportConfiguration:
    """The transport owns configuration, auth and lifecycle."""

    def test_api_key_header_is_plain_not_bearer(self) -> None:
        transport = LeasewebTransport(KEY, BASE)
        headers = {k.lower(): v for k, v in transport.client.headers.items()}
        assert headers["x-lsw-auth"] == KEY
        assert "authorization" not in headers

    def test_base_url_and_timeout_are_configurable(self) -> None:
        transport = LeasewebTransport(KEY, "https://example.test/", timeout_seconds=5.0)
        assert transport.base_url == "https://example.test"
        assert transport.client.timeout.read == pytest.approx(5.0)

    def test_constructor_validates_inputs(self) -> None:
        with pytest.raises(ValueError):
            LeasewebTransport("")
        with pytest.raises(ValueError):
            LeasewebTransport(KEY, max_retries=-1)
        with pytest.raises(ValueError):
            LeasewebTransport(KEY, timeout_seconds=0)
        with pytest.raises(ValueError):
            Throttle(max_rps=0)

    async def test_rotatable_credential_is_resolved_per_request(self) -> None:
        calls: list[str] = []

        class Source:
            def __init__(self) -> None:
                self.value = "key-one"

            async def get(self):
                from cloud_platform.providers.credentials import credential_from_value

                calls.append(self.value)
                return credential_from_value(self.value)

        source = Source()
        transport = LeasewebTransport(KEY, BASE, throttle=_no_sleep(), credential_source=source)
        try:
            with respx.mock:
                route = respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}").mock(
                    return_value=httpx.Response(200, json=VPS_DETAIL)
                )
                api = LeaseWebVpsApi(transport)
                await api.get_vps(VPS)
                source.value = "key-two"
                await api.get_vps(VPS)
            sent = [call.request.headers["X-LSW-Auth"] for call in route.calls]
        finally:
            await transport.aclose()
        assert sent == ["key-one", "key-two"]
        assert calls == ["key-one", "key-two"]

    def test_operation_label_shapes_identifiers(self) -> None:
        from cloud_platform.providers.leaseweb.transport import operation_label

        assert (
            operation_label("GET", f"/publicCloud/v1/vps/{SNAPSHOT_ID}/snapshots")
            == "GET /publicCloud/v1/vps/{id}/snapshots"
        )


class TestAcceptedSemantics:
    """Documented 202/204 semantics are preserved exactly."""

    @respx.mock
    async def test_accepted_actions_return_typed_markers(self) -> None:
        for suffix, _action in (
            ("start", "start"),
            ("stop", "stop"),
            ("reboot", "reboot"),
            ("resetPassword", "reset_password"),
            ("detachIso", "detach_iso"),
        ):
            respx.post(f"{BASE}/publicCloud/v1/vps/{VPS}/{suffix}").mock(
                return_value=httpx.Response(202)
            )
        api = _api()
        try:
            assert (await api.start_vps(VPS)).action == "start"
            assert (await api.stop_vps(VPS)).action == "stop"
            assert (await api.reboot_vps(VPS)).action == "reboot"
            assert (await api.reset_password(VPS)).action == "reset_password"
            assert (await api.detach_iso(VPS)).action == "detach_iso"
        finally:
            await api.aclose()

    @respx.mock
    async def test_delete_snapshot_accepts_202_not_only_204(self) -> None:
        respx.delete(f"{BASE}/publicCloud/v1/vps/{VPS}/snapshots/{SNAPSHOT_ID}").mock(
            return_value=httpx.Response(202)
        )
        api = _api()
        try:
            accepted = await api.delete_snapshot(VPS, SNAPSHOT_ID)
        finally:
            await api.aclose()
        assert accepted.status_code == 202

    @respx.mock
    async def test_no_content_operations_return_none(self) -> None:
        respx.delete(f"{BASE}/publicCloud/v1/vps/{VPS}/credentials").mock(
            return_value=httpx.Response(204)
        )
        respx.post(f"{BASE}/publicCloud/v1/vps/{VPS}/monitoring/enable").mock(
            return_value=httpx.Response(204)
        )
        api = _api()
        try:
            assert await api.delete_credentials(VPS) is None
            assert await api.enable_monitoring(VPS) is None
        finally:
            await api.aclose()

    @respx.mock
    async def test_get_vps_parses_full_detail(self) -> None:
        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}").mock(
            return_value=httpx.Response(200, json=VPS_DETAIL)
        )
        api = _api()
        try:
            detail = await api.get_vps(VPS)
        finally:
            await api.aclose()
        assert detail.id == VPS
        assert detail.public_ip(4) == "1.2.3.4"
        assert detail.iso is not None and detail.iso.id == "GRML"
        assert detail.resources is not None and detail.resources.cpu is not None
        assert detail.resources.cpu.value == 2
        assert detail.resources.memory is not None and str(detail.resources.memory.value) == "3.75"
        assert detail.contract is not None
        assert str(detail.contract.type) == "MONTHLY"
        assert detail.contract.term == 12
        assert detail.contract.starts_at_dt is not None
        assert detail.contract.ends_at_dt is None
        assert detail.ips[0].prefix_length == "32"
        assert detail.ips[0].prefix_length_int == 32
        assert detail.ips[0].ddos is not None
        assert detail.ips[0].network_type == "PUBLIC"
        assert detail.started_at_dt is not None

    @respx.mock
    async def test_list_vps_handles_missing_reference(self) -> None:
        """Recorded relaxation: an empty reference must not break a read."""
        respx.get(f"{BASE}/publicCloud/v1/vps/").mock(
            return_value=httpx.Response(
                200, json=_page(vps=[{**VPS_SUMMARY, "reference": None, "startedAt": None}])
            )
        )
        api = _api()
        try:
            page = await api.list_vps()
        finally:
            await api.aclose()
        assert page.items[0].reference is None
        assert page.items[0].started_at_dt is None

    @respx.mock
    async def test_monitoring_status_and_enable(self) -> None:
        respx.get(f"{BASE}/publicCloud/v1/vps/{VPS}/monitoring/status").mock(
            return_value=httpx.Response(200, json={"status": "NOT_MONITORED"})
        )
        enable = respx.post(f"{BASE}/publicCloud/v1/vps/{VPS}/monitoring/enable").mock(
            return_value=httpx.Response(204)
        )
        api = _api()
        try:
            status = await api.get_monitoring_status(VPS)
            await api.enable_monitoring(VPS)
        finally:
            await api.aclose()
        assert status.status == "NOT_MONITORED"
        assert enable.called

    @respx.mock
    async def test_iso_catalogue_is_account_scoped_and_paginated(self) -> None:
        route = respx.get(f"{BASE}/publicCloud/v1/vps/isos").mock(
            return_value=httpx.Response(
                200,
                json={
                    "isos": [{"id": "GRML", "name": "GRML"}],
                    "_metadata": {"totalCount": 1, "offset": 0, "limit": 100},
                },
            )
        )
        api = _api()
        try:
            isos = [iso async for iso in api.iter_isos()]
        finally:
            await api.aclose()
        assert [iso.id for iso in isos] == ["GRML"]
        assert "/vps/isos" in route.calls.last.request.url.path
