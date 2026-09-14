"""Typed client for the Leaseweb **Ordering** API (VPS products).

Covers the three documented operations of the ``Ordering`` tag that belong to
the VPS product family (``api_docs/leaseweb``):

- ``GET /ordering/v1/products/vps`` — list sellable VPS products + prices;
- ``GET /ordering/v1/products/vps/{vpsId}`` — one product and its full
  configuration options/prices for a location, contract term and billing
  cycle;
- ``POST /ordering/v1/products/vps/{vpsId}/order`` — **BILLABLE**: creates a
  real VPS contract and returns ``201 {orderId}``.

Dedicated-server ordering is deliberately NOT implemented (different product
family, out of scope).

Money: every provider amount is parsed as :class:`decimal.Decimal` from its
text form (``Money``) — never binary float — and can be converted to integer
minor units with :func:`~cloud_platform.providers.leaseweb.models.to_minor_units`.

Billable-order safety: :meth:`LeaseWebOrderingApi.order_vps` is a
``mutating=True`` request. The transport never retries a mutation and raises
``LeasewebAmbiguousMutationError`` when the outcome cannot be proven, so a
single local operation can never produce two billable POSTs by accident. The
low-level method must NOT be called from Telegram handlers, HTTP routes, UI
rendering or reconciliation loops: orders flow through the durable
checkout -> wallet hold -> operation ledger -> worker pipeline.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast
from urllib.parse import quote

from pydantic import Field

from cloud_platform.providers.leaseweb.errors import (
    LeasewebAmbiguousMutationError,
    LeasewebResponseError,
    LeasewebValidationError,
)
from cloud_platform.providers.leaseweb.models import (
    LeasewebModel,
    LeasewebPage,
    LeasewebRequestModel,
    Money,
    OpenStrEnum,
    PaginationMetadata,
    collect_pages,
)
from cloud_platform.providers.leaseweb.transport import LeasewebTransport

__all__ = [
    "BillingCycle",
    "ContractTerm",
    "LeaseWebOrderingApi",
    "OrderVpsRequest",
    "ProductDiscounts",
    "ProductPrice",
    "ProductPriceItem",
    "ProductPriceList",
    "ServiceLevelAgreement",
    "VpsConfigurationOption",
    "VpsConfigurationOptions",
    "VpsOrderResult",
    "VpsProductDetail",
    "VpsProductListItem",
]


class ContractTerm(OpenStrEnum):
    """``contractTerm`` — documented contract terms."""

    ONE_MONTH = "1_MONTH"
    THREE_MONTHS = "3_MONTHS"
    SIX_MONTHS = "6_MONTHS"
    ONE_YEAR = "1_YEAR"
    TWO_YEARS = "2_YEARS"
    THREE_YEARS = "3_YEARS"


class BillingCycle(OpenStrEnum):
    """``billingCycle`` — documented billing cycles."""

    ONE_MONTH = "1_MONTH"
    THREE_MONTHS = "3_MONTHS"
    SIX_MONTHS = "6_MONTHS"
    ONE_YEAR = "1_YEAR"
    TWO_YEARS = "2_YEARS"
    THREE_YEARS = "3_YEARS"


class ServiceLevelAgreement(OpenStrEnum):
    """``serviceLevelAgreement`` — documented SLA options."""

    BASIC = "Basic"
    BRONZE = "Bronze"
    SILVER = "Silver"
    GOLD = "Gold"
    PLATINUM = "Platinum"


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------


class DiscountItem(LeasewebModel):
    """One documented discount line (``description`` + ``value``)."""

    description: str | None = None
    value: Money = Decimal(0)


class ProductDiscounts(LeasewebModel):
    """``discounts`` — total plus the individual discount lines."""

    total: Money = Decimal(0)
    details: list[DiscountItem] = Field(default_factory=list)


class ProductPriceItem(LeasewebModel):
    """One ``contractTerms``/``billingCycles`` row (key + discounted total)."""

    key: str
    description: str | None = None
    discount_text: str | None = None
    discount_value: Money = Decimal(0)
    total: Money = Decimal(0)


class ProductPrice(LeasewebModel):
    """``price`` — the full documented price breakdown of a product.

    All amounts are :class:`Decimal`; ``contract_terms``/``billing_cycles``
    carry every option the provider offers at this location, and
    ``details`` preserves the provider's per-component breakdown verbatim.
    """

    currency: str = "EUR"
    base_price: Money = Decimal(0)
    tax: Money = Decimal(0)
    setup_fee: Money = Decimal(0)
    fee: Money = Decimal(0)
    total: Money = Decimal(0)
    discounts: ProductDiscounts | None = None
    contract_term: str | None = None
    contract_terms: list[ProductPriceItem] = Field(default_factory=list)
    billing_cycle: str | None = None
    billing_cycles: list[ProductPriceItem] = Field(default_factory=list)
    details: dict[str, dict[str, str]] = Field(default_factory=dict)

    def total_for_term(self, term: str) -> Decimal | None:
        """The provider ``total`` for a contract term key, if offered."""
        for row in self.contract_terms:
            if row.key == term:
                return row.total
        return None

    def total_for_cycle(self, cycle: str) -> Decimal | None:
        """The provider ``total`` for a billing cycle key, if offered."""
        for row in self.billing_cycles:
            if row.key == cycle:
                return row.total
        return None


class ProductPriceList(LeasewebModel):
    """``price`` of a product LIST row (``currency``/``basePrice``/...)."""

    currency: str = "EUR"
    base_price: Money = Decimal(0)
    discount: Money = Decimal(0)
    total: Money = Decimal(0)


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


class VpsConfigurationOption(LeasewebModel):
    """``vpsConfigurationOption`` — one selectable option and its price."""

    name: str
    selected: bool = False
    price: Money = Decimal(0)
    currency: str = "EUR"

    @property
    def is_free(self) -> bool:
        """Whether choosing this option does not change the price."""
        return self.price == 0


class VpsConfigurationOptions(LeasewebModel):
    """``configurationOptions`` — every documented option group."""

    disk_upgrade: list[VpsConfigurationOption] = Field(default_factory=list)
    operating_system: list[VpsConfigurationOption] = Field(default_factory=list)
    control_panel: list[VpsConfigurationOption] = Field(default_factory=list)
    service_level_agreement: list[VpsConfigurationOption] = Field(default_factory=list)

    def free_operating_systems(self) -> list[VpsConfigurationOption]:
        """OS options that do not change the base price."""
        return [option for option in self.operating_system if option.is_free]

    def find(self, group: str, name: str) -> VpsConfigurationOption | None:
        """Find an option by documented group name and exact option name.

        The group may be given either as the documented camelCase
        (``controlPanel``) or as the Python attribute name (``control_panel``);
        matching ignores case and separators.
        """
        wanted = "".join(ch for ch in group if ch.isalnum()).lower()
        for field_name, value in self:
            if "".join(ch for ch in field_name if ch.isalnum()).lower() != wanted:
                continue
            if not isinstance(value, list):
                return None
            options = cast("list[VpsConfigurationOption]", value)
            for option in options:
                if option.name == name:
                    return option
            return None
        return None


class VpsProductListItem(LeasewebModel):
    """``vpsListItem`` — one row of ``GET /ordering/v1/products/vps``."""

    id: str
    name: str | None = None
    v_cpu: str | None = None
    v_ram: str | None = None
    nvme_storage: str | None = None
    traffic: str | None = None
    price: ProductPriceList | None = None

    @property
    def vcpu_count(self) -> int | None:
        """``vCpu`` as an int (documentation types it as a string)."""
        return _int_or_none(self.v_cpu)

    @property
    def ram_gb(self) -> int | None:
        """``vRam`` (GB) as an int."""
        return _int_or_none(self.v_ram)

    @property
    def disk_gb(self) -> int | None:
        """``nvmeStorage`` (e.g. ``"100 GB"``) as GB."""
        return _size_gb(self.nvme_storage)


class VpsProductDetail(LeasewebModel):
    """``vps`` — the full detail of ``GET /ordering/v1/products/vps/{vpsId}``.

    Combines the documented ``vpsDetail``, ``productPrice`` and
    ``vpsConfigurationOptions`` schemas. ``location`` is the list of all
    locations where the product is available.
    """

    id: str
    name: str | None = None
    v_cpu: str | None = None
    v_ram: str | None = None
    nvme_storage: str | None = None
    traffic: str | None = None
    location: list[str] = Field(default_factory=list)
    price: ProductPrice | None = None
    configuration_options: VpsConfigurationOptions | None = None

    @property
    def vcpu_count(self) -> int | None:
        return _int_or_none(self.v_cpu)

    @property
    def ram_gb(self) -> int | None:
        return _int_or_none(self.v_ram)

    @property
    def disk_gb(self) -> int | None:
        return _size_gb(self.nvme_storage)

    def available_in(self, location: str) -> bool:
        """Whether the product is available at ``location``.

        An empty list means the provider did not restrict the product, which
        the caller must treat as "unknown" rather than "unavailable".
        """
        return not self.location or location in self.location


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


class OrderVpsRequest(LeasewebRequestModel):
    """``orderVpsOpts`` — the documented VPS order body.

    Only ``location`` is documented as required; ``operatingSystem`` is
    matched by Leaseweb case-insensitively and ignoring a licensing/core
    suffix, so the exact option name from the product detail must be used.
    """

    location: str
    disk_upgrade: str | None = None
    operating_system: str | None = None
    control_panel: str | None = None
    service_level_agreement: ServiceLevelAgreement | str | None = None
    contract_term: ContractTerm | str | None = None
    billing_cycle: BillingCycle | str | None = None


class VpsOrderResult(LeasewebModel):
    """``order`` — the ``201`` response of a VPS order.

    The value is a PROVIDER ORDER id, never a VPS/server id: provisioning is
    tracked through ``GET /account/v1/orders/{Id}`` until the order exposes
    its ``equipmentId``.
    """

    order_id: int

    @property
    def order_id_str(self) -> str:
        """The order id as the string the orders API accepts."""
        return str(self.order_id)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LeaseWebOrderingApi:
    """Typed client for the Leaseweb VPS ordering catalog + billable order."""

    def __init__(self, transport: LeasewebTransport) -> None:
        self._transport = transport

    @property
    def transport(self) -> LeasewebTransport:
        return self._transport

    async def aclose(self) -> None:
        await self._transport.aclose()

    async def list_products(
        self,
        *,
        location: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> LeasewebPage[VpsProductListItem]:
        """``GET /ordering/v1/products/vps`` (documented ``location``/``limit``/``offset``)."""
        params: dict[str, Any] = {}
        if location is not None:
            params["location"] = location
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        payload = await self._transport.request("GET", "/ordering/v1/products/vps", params=params)
        rows = _as_list(payload, "vpss")
        items = [VpsProductListItem.model_validate(row) for row in rows]
        return LeasewebPage[VpsProductListItem](
            items=items, metadata=_metadata(payload, len(items))
        )

    async def all_products(
        self, *, location: str | None = None, page_size: int = 100
    ) -> list[VpsProductListItem]:
        """Every product for a location, following the documented metadata."""

        async def fetch(limit: int, offset: int) -> LeasewebPage[VpsProductListItem]:
            return await self.list_products(location=location, limit=limit, offset=offset)

        return await collect_pages(fetch, page_size=page_size)

    async def get_product(
        self,
        product_id: str,
        *,
        location: str,
        disk_upgrade: str | None = None,
        operating_system: str | None = None,
        control_panel: str | None = None,
        contract_term: ContractTerm | str | None = None,
        billing_cycle: BillingCycle | str | None = None,
        service_level_agreement: ServiceLevelAgreement | str | None = None,
    ) -> VpsProductDetail:
        """``GET /ordering/v1/products/vps/{vpsId}`` with every documented option.

        ``location`` is documented as REQUIRED. The returned price reflects
        exactly the requested term/cycle/options; no price is ever computed
        locally from a cached value.
        """
        params: dict[str, Any] = {"location": location}
        if disk_upgrade is not None:
            params["diskUpgrade"] = disk_upgrade
        if operating_system is not None:
            params["operatingSystem"] = operating_system
        if control_panel is not None:
            params["controlPanel"] = control_panel
        if contract_term is not None:
            params["contractTerm"] = str(contract_term)
        if billing_cycle is not None:
            params["billingCycle"] = str(billing_cycle)
        if service_level_agreement is not None:
            params["serviceLevelAgreement"] = str(service_level_agreement)
        payload = await self._transport.request(
            "GET", f"/ordering/v1/products/vps/{_encoded(product_id)}", params=params
        )
        if not isinstance(payload, dict):
            raise LeasewebResponseError("get_product: expected a JSON object response")
        detail = payload.get("vps") if isinstance(payload.get("vps"), dict) else payload
        return VpsProductDetail.model_validate(detail)

    async def order_vps(self, product_id: str, request: OrderVpsRequest) -> VpsOrderResult:
        """``POST /ordering/v1/products/vps/{vpsId}/order`` — **BILLABLE**.

        Creates a real VPS contract. The transport treats this as a mutation:
        never retried, and an unprovable outcome raises
        ``LeasewebAmbiguousMutationError``. Callers MUST have persisted the
        operation identity (and any wallet hold) BEFORE calling this, and
        MUST persist the returned ``orderId`` immediately.
        """
        payload = await self._transport.request(
            "POST",
            f"/ordering/v1/products/vps/{_encoded(product_id)}/order",
            json=request.body(),
            mutating=True,
        )
        if not isinstance(payload, dict) or payload.get("orderId") is None:
            # 2xx without a usable order id: the order MAY exist -> never re-send.
            raise LeasewebAmbiguousMutationError(
                "order_vps: Leaseweb accepted the request but returned no orderId; outcome unknown"
            )
        return VpsOrderResult.model_validate(payload)


def _encoded(value: str) -> str:
    """Percent-encode a path parameter so it can never alter the path shape."""
    text = str(value or "").strip()
    if not text:
        raise LeasewebValidationError("path parameter must not be empty")
    return quote(text, safe="")


def _as_list(payload: Any, key: str) -> list[dict[str, Any]]:
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


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _size_gb(value: str | None) -> int | None:
    """Parse ``"100 GB"``/``"1 TB"`` into GB (documentation uses text sizes)."""
    if value is None:
        return None
    text = str(value).strip().upper()
    if text.endswith("TB"):
        parsed = _int_or_none(text[:-2].strip())
        return None if parsed is None else parsed * 1024
    if text.endswith("GB"):
        return _int_or_none(text[:-2].strip())
    return _int_or_none(text)
