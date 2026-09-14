"""Typed client for the Leaseweb **Account Orders** API.

Covers the two documented operations used to track a VPS after ordering
(``api_docs/leaseweb``, tag ``Orders``):

- ``GET /account/v1/orders`` — paginated list of the account's orders;
- ``GET /account/v1/orders/{Id}`` — inspect one order, including its
  services' ``status``, ``deliveryEstimate`` and ``equipmentId``.

Provisioning correlation rule (LEASEWEB-MVP, unchanged): the ordering POST
returns an ORDER id, never a VPS id. The ONLY provider identity that may
auto-attach a delivered VPS is the service's ``equipmentId``, confirmed by a
successful ``GET /publicCloud/v1/vps/{equipmentId}``. Plan/price/datacenter/
start-time similarity is NEVER proof of ownership and must not be used to
match a customer's VPS.

All methods here are READ-ONLY; the module deliberately exposes no mutation,
so a reconciliation loop that uses this client can never create an order.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote

from pydantic import Field

from cloud_platform.providers.leaseweb.errors import (
    LeasewebResponseError,
    LeasewebValidationError,
)
from cloud_platform.providers.leaseweb.models import (
    LeasewebModel,
    LeasewebPage,
    Money,
    OpenStrEnum,
    PaginationMetadata,
    parse_leaseweb_datetime,
)
from cloud_platform.providers.leaseweb.transport import LeasewebTransport

__all__ = [
    "VPS_PRODUCT_ID",
    "AccountOrder",
    "AccountOrderService",
    "LeaseWebAccountOrdersApi",
    "OrderOrigin",
    "OrderType",
    "ServiceProductId",
    "ServiceStatus",
]


class OrderType(OpenStrEnum):
    """``order.type`` — documented order types."""

    NEW_ORDER = "NEW_ORDER"
    MODIFICATION = "MODIFICATION"


class OrderOrigin(OpenStrEnum):
    """``order.origin`` — documented order sources."""

    CUSTOMER_PORTAL = "CUSTOMER_PORTAL"
    QUOTATION = "QUOTATION"
    WEBSITE = "WEBSITE"
    OTHER = "OTHER"


class ServiceStatus(OpenStrEnum):
    """``service.status`` — documented service lifecycle states."""

    NEW_CONTRACT = "NEW_CONTRACT"
    ACTIVE = "ACTIVE"
    SCHEDULED = "SCHEDULED"
    TO_BE_MODIFIED = "TO_BE_MODIFIED"
    TO_BE_PROVISIONED = "TO_BE_PROVISIONED"
    TO_BE_SUSPENDED = "TO_BE_SUSPENDED"
    TO_BE_RECONNECTED = "TO_BE_RECONNECTED"
    SUSPENDED = "SUSPENDED"
    CANCELLED = "CANCELLED"


class ServiceProductId(OpenStrEnum):
    """``service.productId`` — the documented product FAMILY ids.

    The list is long and Leaseweb adds families over time; this class keeps
    the families a VPS reseller actually needs and preserves every other
    value verbatim (``OpenStrEnum``).
    """

    VIRTUAL_SERVER = "VIRTUAL_SERVER"
    PUBLIC_CLOUD = "PUBLIC_CLOUD"
    DEDICATED_SERVER = "DEDICATED_SERVER"
    PRIVATE_CLOUD = "PRIVATE_CLOUD"
    ADDITIONAL_SERVICES = "ADDITIONAL_SERVICES"
    DOMAIN = "DOMAIN"
    SSL_CERTIFICATE = "SSL_CERTIFICATE"
    IP_POOL = "IP_POOL"
    FLOATING_IP = "FLOATING_IP"
    OTHER_COST = "OTHER_COST"


#: The documented product id Leaseweb reports for an ordering-VPS service.
#: The Orders API does NOT expose the exact ordering product (e.g.
#: ``VPS02_1``) — only this family id.
VPS_PRODUCT_ID = ServiceProductId.VIRTUAL_SERVER

#: Service statuses that mean "the resource is being/was provisioned".
PROVISIONED_STATUS = ServiceStatus.ACTIVE
PROVISIONING_STATUSES = frozenset(
    {ServiceStatus.NEW_CONTRACT, ServiceStatus.TO_BE_PROVISIONED, ServiceStatus.SCHEDULED}
)
FAILED_STATUSES = frozenset({ServiceStatus.CANCELLED, ServiceStatus.SUSPENDED})


class AccountOrderService(LeasewebModel):
    """``service`` — one service line of an order.

    ``equipment_id`` is the ONLY field that can prove which provider
    resource the order delivered; it is absent until Leaseweb provisions
    the service.
    """

    id: str | None = None
    product_id: ServiceProductId | None = None
    status: ServiceStatus | None = None
    delivery_estimate: str | None = None
    equipment_id: str | None = None
    price_per_frequency: Money | None = None
    currency: str | None = None
    contract_term: str | None = None
    billing_cycle: str | None = None
    domain_name: str | None = None

    @property
    def is_provisioned(self) -> bool:
        """Whether the service is ACTIVE (documented as provisioned)."""
        return self.status == PROVISIONED_STATUS

    @property
    def is_provisioning(self) -> bool:
        return self.status in PROVISIONING_STATUSES

    @property
    def is_failed(self) -> bool:
        return self.status in FAILED_STATUSES

    @property
    def is_vps(self) -> bool:
        """Whether this line belongs to the VIRTUAL_SERVER product family."""
        return self.product_id == VPS_PRODUCT_ID


class AccountOrder(LeasewebModel):
    """``order`` — one account order (list row or full inspection)."""

    id: str
    contract_id: str | None = None
    created_at: str | None = None
    type: OrderType | None = None
    quotation: str | None = None
    origin: OrderOrigin | None = None
    services: list[AccountOrderService] = Field(default_factory=list)

    @property
    def created_at_dt(self) -> datetime | None:
        """``createdAt`` parsed to aware UTC (raw value stays on the DTO)."""
        return parse_leaseweb_datetime(self.created_at)

    def vps_services(self) -> list[AccountOrderService]:
        """The order's VIRTUAL_SERVER service lines."""
        return [service for service in self.services if service.is_vps]

    def first_equipment_id(self) -> str | None:
        """The first provisioned ``equipmentId`` of a VPS service line.

        This is the provider-supported identity used to resolve the delivered
        VPS — never a similarity heuristic.
        """
        for service in self.vps_services():
            if service.equipment_id:
                return service.equipment_id
        return None


class LeaseWebAccountOrdersApi:
    """Read-only typed client for ``/account/v1/orders``."""

    def __init__(self, transport: LeasewebTransport) -> None:
        self._transport = transport

    @property
    def transport(self) -> LeasewebTransport:
        return self._transport

    async def aclose(self) -> None:
        await self._transport.aclose()

    async def list_orders(
        self, *, limit: int | None = None, offset: int | None = None
    ) -> LeasewebPage[AccountOrder]:
        """``GET /account/v1/orders`` (documented ``limit``/``offset``)."""
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        payload = await self._transport.request("GET", "/account/v1/orders", params=params)
        rows = _rows(payload, "orders")
        items = [AccountOrder.model_validate(row) for row in rows]
        return LeasewebPage[AccountOrder](items=items, metadata=_metadata(payload, len(items)))

    async def get_order(self, order_id: str) -> AccountOrder:
        """``GET /account/v1/orders/{Id}`` — read-only order inspection."""
        payload = await self._transport.request("GET", f"/account/v1/orders/{_encoded(order_id)}")
        if not isinstance(payload, dict):
            raise LeasewebResponseError("get_order: expected a JSON object response")
        return AccountOrder.model_validate(payload)


def _encoded(value: str) -> str:
    """Percent-encode a path parameter so it can never alter the path shape."""
    text = str(value or "").strip()
    if not text:
        raise LeasewebValidationError("path parameter must not be empty")
    return quote(text, safe="")


def _rows(payload: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _metadata(payload: Any, page_size: int) -> PaginationMetadata | None:
    raw = payload.get("_metadata") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return None
    try:
        return PaginationMetadata.model_validate(raw)
    except Exception:
        return PaginationMetadata(total_count=page_size, offset=0, limit=page_size)
