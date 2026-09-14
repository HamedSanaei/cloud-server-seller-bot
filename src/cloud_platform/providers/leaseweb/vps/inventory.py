"""Canonical Leaseweb operation inventory (LEASEWEB-VPS-API §2/§19).

One machine-readable row per operation the modern VPS integration covers,
transcribed from the local OpenAPI documentation (``api_docs/leaseweb`` —
all 38 operations of tag ``VPS``, the 3 VPS ``Ordering`` operations and the
2 ``Orders`` operations used to track provisioning).

This module is the single source of truth for:

- ``docs/leaseweb/VPS_API_COVERAGE.md`` (the human-readable matrix is
  checked against it by ``tests/unit/test_leaseweb_coverage_inventory.py``);
- ``leaseweb coverage`` (the read-only diagnostic CLI command);
- the destructive/operator-only classification used by the adapter and the
  reseller-safety review.

Nothing here is generated at runtime from the network: it is a static
contract, so a documentation change is always a deliberate, reviewable edit.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "ALL_OPERATIONS",
    "BASE_PATH",
    "COVERAGE_DOC",
    "LEGACY_VIRTUAL_SERVERS_NOTE",
    "OPERATIONS_BY_ID",
    "ORDERING_OPERATIONS",
    "ORDERS_OPERATIONS",
    "VPS_OPERATIONS",
    "LeasewebOperation",
    "coverage_summary",
]

#: Documentation section this inventory implements.
BASE_PATH = "/publicCloud/v1/vps"

#: Path of the generated-by-hand coverage matrix.
COVERAGE_DOC = "docs/leaseweb/VPS_API_COVERAGE.md"

#: Why the legacy ``Virtual-Servers`` section is NOT implemented here.
LEGACY_VIRTUAL_SERVERS_NOTE = (
    "The local documentation also contains a separate, legacy "
    "'Virtual Servers' section with /virtualServers/... endpoints. It is a "
    "different (older) product family with different models, paths and "
    "power semantics; nothing in this platform depends on it, so it is "
    "deliberately NOT implemented and NOT mixed into the modern VPS client."
)


@dataclass(frozen=True, slots=True)
class LeasewebOperation:
    """One documented Leaseweb operation and where it is implemented."""

    #: Documentation category (``VPS``, ``Ordering``, ``Orders``).
    category: str
    #: The exact ``operationId`` from the local OpenAPI document.
    operation_id: str
    #: HTTP method exactly as documented.
    method: str
    #: Path exactly as documented (including the trailing slash where the
    #: documentation has one).
    path: str
    #: The client class that implements it.
    client: str
    #: The client method name.
    client_method: str
    #: Request model (``-`` when the documented request has no body).
    request_model: str
    #: Response model (``-`` for a documented empty 202/204 response).
    response_model: str
    #: The mocked contract test that covers this operation.
    test: str
    #: ``implemented`` for every row; kept explicit so a not-yet-covered
    #: operation could be recorded as such instead of being forgotten.
    status: str = "implemented"
    #: Whether the operation mutates/destroys provider state.
    destructive: bool = False
    #: Additional documented facts worth surfacing in the matrix.
    notes: tuple[str, ...] = field(default_factory=tuple)


def _vps(
    operation_id: str,
    method: str,
    path: str,
    client_method: str,
    request_model: str,
    response_model: str,
    test: str,
    *,
    destructive: bool = False,
    notes: tuple[str, ...] = (),
) -> LeasewebOperation:
    return LeasewebOperation(
        category="VPS",
        operation_id=operation_id,
        method=method,
        path=path,
        client="LeaseWebVpsApi",
        client_method=client_method,
        request_model=request_model,
        response_model=response_model,
        test=test,
        destructive=destructive,
        notes=notes,
    )


_VPS_TEST = "tests/unit/test_leaseweb_vps_api.py"

#: The 38 documented operations of the modern VPS API.
VPS_OPERATIONS: tuple[LeasewebOperation, ...] = (
    # A. Power
    _vps(
        "start",
        "POST",
        f"{BASE_PATH}/{{vpsId}}/start",
        "start_vps",
        "-",
        "AcceptedVpsAction",
        f"{_VPS_TEST}::test_documented_operation_contract[start_vps]",
        notes=("202 no body (documented)", "precondition: VPS must be stopped"),
    ),
    _vps(
        "stop",
        "POST",
        f"{BASE_PATH}/{{vpsId}}/stop",
        "stop_vps",
        "-",
        "AcceptedVpsAction",
        f"{_VPS_TEST}::test_documented_operation_contract[stop_vps]",
        notes=("202 no body (documented)", "precondition: VPS must be running"),
    ),
    _vps(
        "reboot",
        "POST",
        f"{BASE_PATH}/{{vpsId}}/reboot",
        "reboot_vps",
        "-",
        "AcceptedVpsAction",
        f"{_VPS_TEST}::test_documented_operation_contract[reboot_vps]",
        notes=("202 no body (documented)", "precondition: VPS must be running"),
    ),
    # B. Console
    _vps(
        "getConsoleAccess1",
        "GET",
        f"{BASE_PATH}/{{vpsId}}/console",
        "get_console_access",
        "-",
        "ConsoleAccess",
        f"{_VPS_TEST}::test_documented_operation_contract[get_console_access]",
        notes=("temporary access URL: SecretStr, never logged",),
    ),
    # C. Credentials
    _vps(
        "getCredentialList1",
        "GET",
        f"{BASE_PATH}/{{vpsId}}/credentials",
        "list_credentials",
        "-",
        "list[CredentialSummary]",
        f"{_VPS_TEST}::test_documented_operation_contract[list_credentials]",
        notes=("documented without query parameters",),
    ),
    _vps(
        "storeCredential1",
        "POST",
        f"{BASE_PATH}/{{vpsId}}/credentials",
        "store_credential",
        "StoreCredentialRequest",
        "StoredCredential",
        f"{_VPS_TEST}::test_documented_operation_contract[store_credential]",
        destructive=True,
        notes=("password is SecretStr in request and response",),
    ),
    _vps(
        "deleteCredentials1",
        "DELETE",
        f"{BASE_PATH}/{{vpsId}}/credentials",
        "delete_credentials",
        "-",
        "None (204)",
        f"{_VPS_TEST}::test_documented_operation_contract[delete_credentials]",
        destructive=True,
    ),
    _vps(
        "getCredentialListByType1",
        "GET",
        f"{BASE_PATH}/{{vpsId}}/credentials/{{type}}",
        "list_credentials_by_type",
        "-",
        "list[CredentialSummary]",
        f"{_VPS_TEST}::test_documented_operation_contract[list_credentials_by_type]",
        notes=("type enum: OPERATING_SYSTEM, CONTROL_PANEL",),
    ),
    _vps(
        "getCredential1",
        "GET",
        f"{BASE_PATH}/{{vpsId}}/credentials/{{type}}/{{username}}",
        "get_credential",
        "-",
        "CredentialDetail",
        f"{_VPS_TEST}::test_documented_operation_contract[get_credential]",
        notes=("password returned as SecretStr",),
    ),
    _vps(
        "updateCredential1",
        "PUT",
        f"{BASE_PATH}/{{vpsId}}/credentials/{{type}}/{{username}}",
        "update_credential",
        "UpdateCredentialRequest",
        "StoredCredential",
        f"{_VPS_TEST}::test_documented_operation_contract[update_credential]",
        destructive=True,
    ),
    _vps(
        "deleteCredential1",
        "DELETE",
        f"{BASE_PATH}/{{vpsId}}/credentials/{{type}}/{{username}}",
        "delete_credential",
        "-",
        "None (204)",
        f"{_VPS_TEST}::test_documented_operation_contract[delete_credential]",
        destructive=True,
    ),
    _vps(
        "resetPassword1",
        "POST",
        f"{BASE_PATH}/{{vpsId}}/resetPassword",
        "reset_password",
        "-",
        "AcceptedVpsAction",
        f"{_VPS_TEST}::test_documented_operation_contract[reset_password]",
        destructive=True,
        notes=("202 no body (documented)", "read the new value via credentials endpoints"),
    ),
    # D. ISO
    _vps(
        "getIsoList1",
        "GET",
        f"{BASE_PATH}/isos",
        "list_isos",
        "-",
        "LeasewebPage[IsoRecord]",
        f"{_VPS_TEST}::test_documented_operation_contract[list_isos]",
        notes=("account-wide catalogue, not vps-scoped (documented path)",),
    ),
    _vps(
        "attachIso1",
        "POST",
        f"{BASE_PATH}/{{vpsId}}/attachIso",
        "attach_iso",
        "AttachIsoRequest",
        "AcceptedVpsAction",
        f"{_VPS_TEST}::test_documented_operation_contract[attach_iso]",
        destructive=True,
        notes=("202 no body; precondition: no ISO attached",),
    ),
    _vps(
        "detachIso1",
        "POST",
        f"{BASE_PATH}/{{vpsId}}/detachIso",
        "detach_iso",
        "-",
        "AcceptedVpsAction",
        f"{_VPS_TEST}::test_documented_operation_contract[detach_iso]",
        destructive=True,
        notes=("202 no body; no request body documented",),
    ),
    # E. Reinstall
    _vps(
        "getReinstallImageList1",
        "GET",
        f"{BASE_PATH}/{{vpsId}}/reinstall/images",
        "list_reinstall_images",
        "-",
        "LeasewebPage[ReinstallImage]",
        f"{_VPS_TEST}::test_documented_operation_contract[list_reinstall_images]",
        notes=("query: limit, offset, standard",),
    ),
    _vps(
        "reinstall",
        "PUT",
        f"{BASE_PATH}/{{vpsId}}/reinstall",
        "reinstall",
        "ReinstallRequest",
        "AcceptedVpsAction",
        f"{_VPS_TEST}::test_documented_operation_contract[reinstall]",
        destructive=True,
        notes=("DESTRUCTIVE: recreates the VPS", "cannot run while snapshots exist"),
    ),
    # F. IPs
    _vps(
        "getIPList1",
        "GET",
        f"{BASE_PATH}/{{vpsId}}/ips",
        "list_ips",
        "-",
        "LeasewebPage[VpsIpDetails]",
        f"{_VPS_TEST}::test_documented_operation_contract[list_ips]",
        notes=("query: version (4|6), nullRouted, ips ('|' separated)",),
    ),
    _vps(
        "getIP",
        "GET",
        f"{BASE_PATH}/{{vpsId}}/ips/{{ip}}",
        "get_ip",
        "-",
        "VpsIpDetails",
        f"{_VPS_TEST}::test_documented_operation_contract[get_ip]",
        notes=("ip path parameter: format ip",),
    ),
    _vps(
        "updateIP1",
        "PUT",
        f"{BASE_PATH}/{{vpsId}}/ips/{{ip}}",
        "update_ip",
        "UpdateIpRequest",
        "VpsIpDetails",
        f"{_VPS_TEST}::test_documented_operation_contract[update_ip]",
    ),
    _vps(
        "nullRouteIP1",
        "POST",
        f"{BASE_PATH}/{{vpsId}}/ips/{{ip}}/null",
        "null_route_ip",
        "NullRouteIpRequest",
        "VpsIpDetails",
        f"{_VPS_TEST}::test_documented_operation_contract[null_route_ip]",
        destructive=True,
        notes=("IPv4 only (documented)", "body optional; automatedUnnulingAt is hours"),
    ),
    _vps(
        "removeIPNullRoute1",
        "POST",
        f"{BASE_PATH}/{{vpsId}}/ips/{{ip}}/unnull",
        "remove_ip_null_route",
        "-",
        "VpsIpDetails",
        f"{_VPS_TEST}::test_documented_operation_contract[remove_ip_null_route]",
    ),
    # G. Metrics
    _vps(
        "getDataTrafficMetrics",
        "GET",
        f"{BASE_PATH}/{{vpsId}}/metrics/datatraffic",
        "get_data_traffic_metrics",
        "-",
        "DataTrafficMetrics",
        f"{_VPS_TEST}::test_documented_operation_contract[get_data_traffic_metrics]",
        notes=(
            "query: from, to, granularity (5m|10m|30m|60m|DAY), aggregation (SUM)",
            "values are bytes as integers; metadata preserved verbatim",
        ),
    ),
    # H. Snapshots
    _vps(
        "getSnapshotList1",
        "GET",
        f"{BASE_PATH}/{{vpsId}}/snapshots",
        "list_snapshots",
        "-",
        "LeasewebPage[Snapshot]",
        f"{_VPS_TEST}::test_documented_operation_contract[list_snapshots]",
    ),
    _vps(
        "createSnapshot1",
        "POST",
        f"{BASE_PATH}/{{vpsId}}/snapshots",
        "create_snapshot",
        "CreateSnapshotRequest",
        "AcceptedVpsAction",
        f"{_VPS_TEST}::test_documented_operation_contract[create_snapshot]",
        notes=("202 no body; one snapshot per VPS (documented)",),
    ),
    _vps(
        "getSnapshot1",
        "GET",
        f"{BASE_PATH}/{{vpsId}}/snapshots/{{snapshotId}}",
        "get_snapshot",
        "-",
        "Snapshot",
        f"{_VPS_TEST}::test_documented_operation_contract[get_snapshot]",
        notes=("snapshotId path parameter: format uuid",),
    ),
    _vps(
        "restoreSnapshot1",
        "PUT",
        f"{BASE_PATH}/{{vpsId}}/snapshots/{{snapshotId}}",
        "restore_snapshot",
        "-",
        "AcceptedVpsAction",
        f"{_VPS_TEST}::test_documented_operation_contract[restore_snapshot]",
        destructive=True,
        notes=("202 no body (documented)",),
    ),
    _vps(
        "deleteSnapshot1",
        "DELETE",
        f"{BASE_PATH}/{{vpsId}}/snapshots/{{snapshotId}}",
        "delete_snapshot",
        "-",
        "AcceptedVpsAction",
        f"{_VPS_TEST}::test_documented_operation_contract[delete_snapshot]",
        destructive=True,
        notes=("202 no body (documented, NOT 204)",),
    ),
    # I. List / detail / update
    _vps(
        "getVpsList",
        "GET",
        f"{BASE_PATH}/",
        "list_vps",
        "-",
        "LeasewebPage[VpsSummary]",
        f"{_VPS_TEST}::test_documented_operation_contract[list_vps]",
        notes=("query: limit, offset, id, reference, ip, state, pack, region",),
    ),
    _vps(
        "getVps",
        "GET",
        f"{BASE_PATH}/{{vpsId}}",
        "get_vps",
        "-",
        "VpsDetail",
        f"{_VPS_TEST}::test_documented_operation_contract[get_vps]",
        notes=("no 404 in the documented response list (401/403/500/503 only)",),
    ),
    _vps(
        "updateVps",
        "PUT",
        f"{BASE_PATH}/{{vpsId}}",
        "update_vps",
        "UpdateVpsRequest",
        "VpsDetail",
        f"{_VPS_TEST}::test_documented_operation_contract[update_vps]",
        notes=("documented body: reference",),
    ),
    # J. Data-traffic notification settings
    _vps(
        "getNotificationSettingList1",
        "GET",
        f"{BASE_PATH}/{{vpsId}}/notificationSettings/dataTraffic",
        "list_data_traffic_notification_settings",
        "-",
        "LeasewebPage[NotificationSetting]",
        f"{_VPS_TEST}::test_documented_operation_contract[list_data_traffic_notification_settings]",
    ),
    _vps(
        "getNotificationSetting1",
        "GET",
        f"{BASE_PATH}/{{vpsId}}/notificationSettings/dataTraffic/{{notificationSettingId}}",
        "get_data_traffic_notification_setting",
        "-",
        "NotificationSetting",
        f"{_VPS_TEST}::test_documented_operation_contract[get_data_traffic_notification_setting]",
    ),
    _vps(
        "createNotificationSetting1",
        "POST",
        f"{BASE_PATH}/{{vpsId}}/notificationSettings/dataTraffic/{{notificationSettingId}}",
        "create_data_traffic_notification_setting",
        "CreateNotificationSettingRequest",
        "NotificationSetting",
        f"{_VPS_TEST}::test_documented_operation_contract[create_data_traffic_notification_setting]",
        notes=(
            "documented 201",
            "CLIENT-SUPPLIED id in the PATH (unusual shape kept verbatim)",
        ),
    ),
    _vps(
        "updateNotificationSetting1",
        "PUT",
        f"{BASE_PATH}/{{vpsId}}/notificationSettings/dataTraffic/{{notificationSettingId}}",
        "update_data_traffic_notification_setting",
        "UpdateNotificationSettingRequest",
        "NotificationSetting",
        f"{_VPS_TEST}::test_documented_operation_contract[update_data_traffic_notification_setting]",
    ),
    _vps(
        "deleteNotificationSetting1",
        "DELETE",
        f"{BASE_PATH}/{{vpsId}}/notificationSettings/dataTraffic/{{notificationSettingId}}",
        "delete_data_traffic_notification_setting",
        "-",
        "None (204)",
        f"{_VPS_TEST}::test_documented_operation_contract[delete_data_traffic_notification_setting]",
        destructive=True,
    ),
    # K. Monitoring
    _vps(
        "getVpsMonitoringStatus",
        "GET",
        f"{BASE_PATH}/{{vpsId}}/monitoring/status",
        "get_monitoring_status",
        "-",
        "MonitoringStatusResult",
        f"{_VPS_TEST}::test_documented_operation_contract[get_monitoring_status]",
    ),
    _vps(
        "enableVpsMonitoring",
        "POST",
        f"{BASE_PATH}/{{vpsId}}/monitoring/enable",
        "enable_monitoring",
        "-",
        "None (204)",
        f"{_VPS_TEST}::test_documented_operation_contract[enable_monitoring]",
        notes=("204 no content (documented)",),
    ),
)

#: The 3 documented VPS operations of the Ordering API.
ORDERING_OPERATIONS: tuple[LeasewebOperation, ...] = (
    LeasewebOperation(
        category="Ordering",
        operation_id="getVpsList1",
        method="GET",
        path="/ordering/v1/products/vps",
        client="LeaseWebOrderingApi",
        client_method="list_products",
        request_model="-",
        response_model="LeasewebPage[VpsProductListItem]",
        test="tests/unit/test_leaseweb_ordering_api.py::test_list_products_sends_documented_query",
        notes=("query: location, limit, offset",),
    ),
    LeasewebOperation(
        category="Ordering",
        operation_id="getVps1",
        method="GET",
        path="/ordering/v1/products/vps/{vpsId}",
        client="LeaseWebOrderingApi",
        client_method="get_product",
        request_model="-",
        response_model="VpsProductDetail",
        test="tests/unit/test_leaseweb_ordering_api.py::test_get_product_sends_every_documented_parameter",
        notes=(
            "query (all documented): location (required), diskUpgrade, "
            "operatingSystem, controlPanel, contractTerm, billingCycle, "
            "serviceLevelAgreement",
        ),
    ),
    LeasewebOperation(
        category="Ordering",
        operation_id="orderVps",
        method="POST",
        path="/ordering/v1/products/vps/{vpsId}/order",
        client="LeaseWebOrderingApi",
        client_method="order_vps",
        request_model="OrderVpsRequest",
        response_model="VpsOrderResult",
        test="tests/unit/test_leaseweb_ordering_api.py::test_order_posts_documented_body_and_returns_order_id",
        status="implemented",
        destructive=True,
        notes=(
            "BILLABLE: creates a real contract; 201 {orderId}",
            "never retried; ambiguous outcome raises LeasewebAmbiguousMutationError",
            "must flow through checkout -> hold -> operation ledger -> worker",
        ),
    ),
)

#: The 2 documented account-order operations used to track provisioning.
ORDERS_OPERATIONS: tuple[LeasewebOperation, ...] = (
    LeasewebOperation(
        category="Orders",
        operation_id="getOrders",
        method="GET",
        path="/account/v1/orders",
        client="LeaseWebAccountOrdersApi",
        client_method="list_orders",
        request_model="-",
        response_model="LeasewebPage[AccountOrder]",
        test="tests/unit/test_leaseweb_orders_api.py::test_list_orders_paginates",
        notes=("read-only; limit/offset",),
    ),
    LeasewebOperation(
        category="Orders",
        operation_id="getOrder",
        method="GET",
        path="/account/v1/orders/{Id}",
        client="LeaseWebAccountOrdersApi",
        client_method="get_order",
        request_model="-",
        response_model="AccountOrder",
        test="tests/unit/test_leaseweb_orders_api.py::test_get_order_exposes_equipment_id",
        notes=(
            "read-only; reconciliation MUST use this (GET) path only",
            "equipmentId is the only identity that may attach a delivered VPS",
        ),
    ),
)

ALL_OPERATIONS: tuple[LeasewebOperation, ...] = (
    VPS_OPERATIONS + ORDERING_OPERATIONS + ORDERS_OPERATIONS
)

OPERATIONS_BY_ID: dict[str, LeasewebOperation] = {
    operation.operation_id: operation for operation in ALL_OPERATIONS
}


def coverage_summary() -> dict[str, int]:
    """Counts per category plus the total (used by the CLI and the tests)."""
    summary: dict[str, int] = {}
    for operation in ALL_OPERATIONS:
        summary[operation.category] = summary.get(operation.category, 0) + 1
    summary["total"] = len(ALL_OPERATIONS)
    summary["destructive"] = sum(1 for op in ALL_OPERATIONS if op.destructive)
    return summary
