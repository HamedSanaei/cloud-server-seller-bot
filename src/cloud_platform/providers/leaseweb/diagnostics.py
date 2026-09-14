"""Read-only Leaseweb diagnostics for operator CLI commands.

Every function here performs READ-ONLY provider calls and returns printable
lines. There is deliberately NO diagnostic that can create a VPS, place an
order, reinstall, reset a password, delete a credential or mutate a snapshot:
billable and destructive operations only ever flow through the durable
checkout -> wallet hold -> operation ledger -> worker pipeline, so a CLI
mistake can never purchase or destroy a customer resource.

Secrets: no function prints a credential value, a console URL, an API key or
an ``X-LSW-Auth`` header. Error lines carry the provider's safe summary
(``errorCode``/``correlationId``) only.
"""

from __future__ import annotations

from typing import Any

from cloud_platform.core.config import get_settings
from cloud_platform.providers.leaseweb.errors import (
    LeasewebError,
    LeasewebValidationError,
)
from cloud_platform.providers.leaseweb.ordering_api import LeaseWebOrderingApi
from cloud_platform.providers.leaseweb.orders_api import LeaseWebAccountOrdersApi
from cloud_platform.providers.leaseweb.transport import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT_SECONDS,
    LeasewebTransport,
)
from cloud_platform.providers.leaseweb.vps.client import LeaseWebVpsApi
from cloud_platform.providers.leaseweb.vps.inventory import (
    ALL_OPERATIONS,
    LEGACY_VIRTUAL_SERVERS_NOTE,
    coverage_summary,
)

__all__ = [
    "auth_check_lines",
    "build_ordering_api",
    "build_orders_api",
    "build_transport",
    "build_vps_api",
    "coverage_lines",
    "order_show_lines",
    "orders_list_lines",
    "product_show_lines",
    "products_list_lines",
    "vps_ips_lines",
    "vps_list_lines",
    "vps_metrics_lines",
    "vps_monitoring_lines",
    "vps_show_lines",
    "vps_snapshots_lines",
]


def _settings() -> Any:
    return get_settings()


def _api_key(settings: Any) -> str:
    key = str(getattr(settings, "leaseweb_api_key", "") or "").strip()
    if not key or key == "CHANGE_ME":
        raise LeasewebValidationError(
            "leaseweb api key is not configured (set [providers.leaseweb] api_key)"
        )
    return key


def build_transport(settings: Any | None = None) -> LeasewebTransport:
    """Build the ONE shared transport from configuration (never hard-coded)."""
    resolved = settings or _settings()
    return LeasewebTransport(
        _api_key(resolved),
        str(getattr(resolved, "leaseweb_api_base_url", "") or DEFAULT_BASE_URL),
        timeout_seconds=float(
            getattr(resolved, "leaseweb_timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
            or DEFAULT_TIMEOUT_SECONDS
        ),
        provider_key="leaseweb",
    )


def build_ordering_api(settings: Any | None = None) -> LeaseWebOrderingApi:
    return LeaseWebOrderingApi(build_transport(settings))


def build_orders_api(settings: Any | None = None) -> LeaseWebAccountOrdersApi:
    return LeaseWebAccountOrdersApi(build_transport(settings))


def build_vps_api(settings: Any | None = None) -> LeaseWebVpsApi:
    return LeaseWebVpsApi(build_transport(settings))


def _locations(settings: Any) -> list[str]:
    raw = str(getattr(settings, "leaseweb_locations", "") or "")
    return [part.strip() for part in raw.split(",") if part.strip()]


# ---------------------------------------------------------------------------
# Read-only commands
# ---------------------------------------------------------------------------


async def auth_check_lines() -> list[str]:
    """Prove the configured API key works, using ONE read-only catalogue call."""
    settings = _settings()
    if not str(getattr(settings, "leaseweb_api_key", "") or "").strip():
        return ["leaseweb auth-check: FAIL - api_key is not configured"]
    locations = _locations(settings)
    api = build_ordering_api(settings)
    lines = [f"base_url={api.transport.base_url}"]
    try:
        products = await api.list_products(location=locations[0] if locations else None, limit=1)
    except LeasewebError as exc:
        lines.append(f"leaseweb auth-check: FAIL - {exc}")
        return lines
    finally:
        await api.aclose()
    lines.append(
        "leaseweb auth-check: OK - read-only catalogue call succeeded "
        f"({len(products.items)} product row(s) returned)"
    )
    lines.append("no credential value, console URL or API key is ever printed")
    return lines


async def products_list_lines(location: str | None = None) -> list[str]:
    """List sellable VPS products (read-only), one line per product."""
    settings = _settings()
    scopes = [location] if location else _locations(settings)
    api = build_ordering_api(settings)
    lines: list[str] = []
    try:
        for scope in scopes:
            products = await api.all_products(location=scope)
            lines.append(f"location {scope}: {len(products)} product(s)")
            for product in products:
                price = product.price
                lines.append(
                    f"  {product.id:16} {product.name or '':24} "
                    f"vcpu={product.v_cpu or '?'} ram={product.v_ram or '?'} "
                    f"nvme={product.nvme_storage or '?'} traffic={product.traffic or '?'} "
                    f"total={price.total if price else '?'} {price.currency if price else ''}"
                )
    finally:
        await api.aclose()
    return lines


async def product_show_lines(
    product_id: str,
    *,
    location: str,
    operating_system: str | None = None,
    control_panel: str | None = None,
    disk_upgrade: str | None = None,
    contract_term: str | None = None,
    billing_cycle: str | None = None,
    service_level_agreement: str | None = None,
) -> list[str]:
    """Show one product's configuration options and prices (read-only)."""
    settings = _settings()
    api = build_ordering_api(settings)
    try:
        detail = await api.get_product(
            product_id,
            location=location,
            operating_system=operating_system,
            control_panel=control_panel,
            disk_upgrade=disk_upgrade,
            contract_term=contract_term,
            billing_cycle=billing_cycle,
            service_level_agreement=service_level_agreement,
        )
    finally:
        await api.aclose()
    price = detail.price
    lines = [
        f"product {detail.id} at {location}",
        f"  specs: vcpu={detail.v_cpu or '?'} ram={detail.v_ram or '?'} "
        f"nvme={detail.nvme_storage or '?'} traffic={detail.traffic or '?'}",
        f"  available_at={', '.join(detail.location) or 'all locations (not restricted)'}",
    ]
    if price is not None:
        lines.append(
            f"  price: total={price.total} {price.currency} base={price.base_price} "
            f"setup={price.setup_fee} term={price.contract_term or '?'} "
            f"cycle={price.billing_cycle or '?'}"
        )
        lines.append(
            "  contractTerms: "
            + ", ".join(f"{row.key}={row.total}" for row in price.contract_terms)
        )
        lines.append(
            "  billingCycles: "
            + ", ".join(f"{row.key}={row.total}" for row in price.billing_cycles)
        )
    options = detail.configuration_options
    if options is not None:
        for group in (
            "operating_system",
            "control_panel",
            "disk_upgrade",
            "service_level_agreement",
        ):
            rows = getattr(options, group)
            lines.append(f"  {group}:")
            for option in rows:
                lines.append(
                    f"    {option.name:40} price={option.price} {option.currency} "
                    f"free={option.is_free} selected={option.selected}"
                )
    return lines


async def orders_list_lines(*, limit: int = 20) -> list[str]:
    """List recent account orders (read-only)."""
    api = build_orders_api()
    try:
        page = await api.list_orders(limit=limit, offset=0)
    finally:
        await api.aclose()
    lines = [f"{len(page.items)} order(s) returned (limit={limit})"]
    for order in page.items:
        services = ", ".join(
            f"{service.product_id}:{service.status}"
            + (f" equipmentId={service.equipment_id}" if service.equipment_id else "")
            for service in order.services
        )
        lines.append(f"  {order.id} {order.type or '?'} {order.created_at or '?'} [{services}]")
    return lines


async def order_show_lines(order_id: str) -> list[str]:
    """Inspect one order (read-only) — the reconciliation identity source."""
    api = build_orders_api()
    try:
        order = await api.get_order(order_id)
    finally:
        await api.aclose()
    lines = [
        f"order {order.id} type={order.type or '?'} origin={order.origin or '?'} "
        f"created={order.created_at or '?'} contract={order.contract_id or '?'}",
        f"  equipmentId (provider resource identity): {order.first_equipment_id() or '<not yet>'}",
    ]
    for service in order.services:
        lines.append(
            f"  service {service.id or '?'} {service.product_id or '?'} "
            f"status={service.status or '?'} deliveryEstimate={service.delivery_estimate or '?'} "
            f"equipmentId={service.equipment_id or '<not yet>'} "
            f"price={service.price_per_frequency or '?'} {service.currency or ''} "
            f"term={service.contract_term or '?'} cycle={service.billing_cycle or '?'}"
        )
    return lines


async def vps_list_lines() -> list[str]:
    """List the account's VPSes with their documented state (read-only)."""
    api = build_vps_api()
    try:
        vpses = await api.all_vps()
    finally:
        await api.aclose()
    lines = [f"{len(vpses)} VPS(es)"]
    for vps in vpses:
        lines.append(
            f"  {vps.id} state={vps.state} pack={vps.pack} region={vps.region} "
            f"datacenter={vps.datacenter} reference={vps.reference or '-'} "
            f"ipv4={vps.public_ip(4) or '-'}"
        )
    return lines


async def vps_show_lines(vps_id: str) -> list[str]:
    """Show one VPS's full documented detail (read-only)."""
    api = build_vps_api()
    try:
        vps = await api.get_vps(vps_id)
    finally:
        await api.aclose()
    lines = [
        f"vps {vps.id} state={vps.state} reference={vps.reference or '-'}",
        f"  pack={vps.pack} region={vps.region} datacenter={vps.datacenter}",
        f"  image={vps.image.name} ({vps.image.id}) custom={vps.image.custom}",
        f"  rootDiskSize={vps.root_disk_size}GB hasPublicIpV4={vps.has_public_ip_v4}",
        f"  startedAt={vps.started_at or '-'} iso={vps.iso.id if vps.iso else '-'}",
    ]
    if vps.resources is not None:
        cpu = vps.resources.cpu
        memory = vps.resources.memory
        speed = vps.resources.public_network_speed
        lines.append(
            f"  resources: cpu={cpu.value if cpu else '?'}{cpu.unit if cpu else ''} "
            f"memory={memory.value if memory else '?'}{memory.unit if memory else ''} "
            f"network={speed.value if speed else '?'}{speed.unit if speed else ''}"
        )
    if vps.contract is not None:
        lines.append(
            f"  contract: id={vps.contract.id} type={vps.contract.type} "
            f"state={vps.contract.state} term={vps.contract.term} "
            f"billingFrequency={vps.contract.billing_frequency} "
            f"sla={vps.contract.sla or '-'} startsAt={vps.contract.starts_at or '-'} "
            f"endsAt={vps.contract.ends_at or '-'} inModification={vps.contract.in_modification}"
        )
    for ip in vps.ips:
        lines.append(
            f"  ip {ip.ip}/{ip.prefix_length} v{ip.version} network={ip.network_type} "
            f"nullRouted={ip.null_routed} main={ip.main_ip} "
            f"reverse={ip.reverse_lookup or '-'}"
        )
    return lines


async def vps_ips_lines(vps_id: str) -> list[str]:
    """List a VPS's IPs (read-only)."""
    api = build_vps_api()
    try:
        page = await api.list_ips(vps_id)
    finally:
        await api.aclose()
    lines = [f"{len(page.items)} IP(s)"]
    for ip in page.items:
        lines.append(
            f"  {ip.ip}/{ip.prefix_length} v{ip.version} {ip.network_type} "
            f"nullRouted={ip.null_routed} reverse={ip.reverse_lookup or '-'}"
        )
    return lines


async def vps_metrics_lines(
    vps_id: str,
    *,
    from_: str,
    to: str,
    granularity: str = "DAY",
    aggregation: str = "SUM",
) -> list[str]:
    """Show documented data-traffic metrics (read-only, bytes as integers)."""
    api = build_vps_api()
    try:
        metrics = await api.get_data_traffic_metrics(
            vps_id, from_=from_, to=to, granularity=granularity, aggregation=aggregation
        )
    finally:
        await api.aclose()
    lines = [
        f"metrics from={metrics.from_ or '?'} to={metrics.to or '?'} "
        f"granularity={metrics.granularity or '?'} aggregation={metrics.aggregation or '?'} "
        f"unit={metrics.unit or '?'}",
        f"  total_bytes={metrics.total_bytes()}",
    ]
    for direction, metric in sorted(metrics.metrics.items()):
        summary = metrics.summary.get(direction)
        lines.append(
            f"  {direction}: {len(metric.values)} point(s) unit={metric.unit or '?'}"
            + (
                f" total={summary.total} average={summary.average} expected={summary.expected}"
                if summary
                else ""
            )
        )
    return lines


async def vps_snapshots_lines(vps_id: str) -> list[str]:
    """List a VPS's snapshots (read-only)."""
    api = build_vps_api()
    try:
        page = await api.list_snapshots(vps_id)
    finally:
        await api.aclose()
    lines = [f"{len(page.items)} snapshot(s)"]
    for snapshot in page.items:
        lines.append(
            f"  {snapshot.id} state={snapshot.state or '?'} "
            f"name={snapshot.display_name or '-'} created={snapshot.created or '-'}"
        )
    return lines


async def vps_monitoring_lines(vps_id: str) -> list[str]:
    """Show monitoring status (read-only)."""
    api = build_vps_api()
    try:
        status = await api.get_monitoring_status(vps_id)
    finally:
        await api.aclose()
    return [
        f"monitoring status={status.status or '?'} description={status.description or '-'}",
        "use the monitoring/enable operation from an authorized workflow to enable it",
    ]


def coverage_lines() -> list[str]:
    """Print the endpoint coverage matrix summary of the local documentation."""
    summary = coverage_summary()
    lines = [
        "Leaseweb modern VPS integration coverage",
        f"  VPS operations:     {summary.get('VPS', 0)}",
        f"  Ordering (VPS):     {summary.get('Ordering', 0)}",
        f"  Account orders:     {summary.get('Orders', 0)}",
        f"  total implemented:  {summary.get('total', 0)}",
        f"  destructive ops:    {summary.get('destructive', 0)}",
        "matrix: docs/leaseweb/VPS_API_COVERAGE.md",
        "",
    ]
    for category in ("VPS", "Ordering", "Orders"):
        lines.append(f"[{category}]")
        for operation in ALL_OPERATIONS:
            if operation.category != category:
                continue
            flag = " (destructive)" if operation.destructive else ""
            lines.append(
                f"  {operation.method:6} {operation.path}  -> "
                f"{operation.client}.{operation.client_method}{flag}"
            )
    lines.append("")
    lines.append(LEGACY_VIRTUAL_SERVERS_NOTE)
    lines.append("")
    lines.append(
        "no billable or destructive operation is exposed as a CLI command: orders "
        "flow through the durable checkout -> hold -> ledger -> worker pipeline"
    )
    return lines
