from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class CatalogPrice:
    provider_key: str
    provider_plan_id: str
    provider_location_id: str
    currency: str
    provider_hourly: Decimal
    provider_monthly_cap: Decimal | None
    customer_hourly: Decimal
    enabled: bool = True
