"""Leaseweb provider integration public surface.

Two product families live here:

- **Public Cloud** (``client.py``) — hourly instances,
  ``/publicCloud/v1/instances`` (``LeaseWebProvider``).
- **VPS** (``ordering.py`` + ``vps/`` + ``ordering_api.py`` +
  ``orders_api.py``) — monthly VPS bought through the Ordering API and
  managed through the modern ``/publicCloud/v1/vps`` API
  (``LeaseWebOrderingProvider``).

Everything speaks through ONE shared
:class:`~cloud_platform.providers.leaseweb.transport.LeasewebTransport`
(auth, timeouts, retries, error mapping, redaction).
"""

from cloud_platform.providers.leaseweb.client import (
    LEASEWEBCLOUD_CAPABILITIES,
    LeaseWebProvider,
    Throttle,
    normalize_provider_status,
)
from cloud_platform.providers.leaseweb.errors import (
    LeasewebAmbiguousMutationError,
    LeasewebAuthenticationError,
    LeasewebConflictError,
    LeasewebError,
    LeasewebForbiddenError,
    LeasewebNotFoundError,
    LeasewebRateLimitError,
    LeasewebResponseError,
    LeasewebServerError,
    LeasewebTimeoutError,
    LeasewebUnavailableError,
    LeasewebValidationError,
)
from cloud_platform.providers.leaseweb.ordering import (
    LEASEWEB_ORDERING_CAPABILITIES,
    LeaseWebOrderingProvider,
    LeasewebProduct,
    LeasewebProductDetail,
)
from cloud_platform.providers.leaseweb.ordering_api import LeaseWebOrderingApi
from cloud_platform.providers.leaseweb.orders_api import LeaseWebAccountOrdersApi
from cloud_platform.providers.leaseweb.transport import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT_SECONDS,
    LeasewebTransport,
    MutationOutcome,
)
from cloud_platform.providers.leaseweb.vps.client import LeaseWebVpsApi
from cloud_platform.providers.leaseweb.vps.inventory import (
    ALL_OPERATIONS,
    VPS_OPERATIONS,
    coverage_summary,
)

LeasewebCloudProvider = LeaseWebProvider
LEASEWEB_CAPABILITIES = LEASEWEBCLOUD_CAPABILITIES
LeaseWebThrottle = Throttle
normalize_leaseweb_status = normalize_provider_status
LeasewebOrderingProvider = LeaseWebOrderingProvider
LeasewebVpsApi = LeaseWebVpsApi

__all__ = [
    "ALL_OPERATIONS",
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "LEASEWEBCLOUD_CAPABILITIES",
    "LEASEWEB_CAPABILITIES",
    "LEASEWEB_ORDERING_CAPABILITIES",
    "VPS_OPERATIONS",
    "LeaseWebAccountOrdersApi",
    "LeaseWebOrderingApi",
    "LeaseWebOrderingProvider",
    "LeaseWebProvider",
    "LeaseWebThrottle",
    "LeaseWebVpsApi",
    "LeaseWebVpsManagementMixin",
    "LeasewebAmbiguousMutationError",
    "LeasewebAuthenticationError",
    "LeasewebCloudProvider",
    "LeasewebConflictError",
    "LeasewebError",
    "LeasewebForbiddenError",
    "LeasewebNotFoundError",
    "LeasewebOrderingProvider",
    "LeasewebProduct",
    "LeasewebProductDetail",
    "LeasewebRateLimitError",
    "LeasewebResponseError",
    "LeasewebServerError",
    "LeasewebTimeoutError",
    "LeasewebTransport",
    "LeasewebUnavailableError",
    "LeasewebValidationError",
    "LeasewebVpsApi",
    "MutationOutcome",
    "Throttle",
    "coverage_summary",
    "normalize_leaseweb_status",
    "normalize_provider_status",
]


def __getattr__(name: str) -> object:
    """Lazily expose the VPS management mixin (avoids an import cycle)."""
    if name == "LeaseWebVpsManagementMixin":
        from cloud_platform.providers.leaseweb.vps.management import (
            LeaseWebVpsManagementMixin,
        )

        return LeaseWebVpsManagementMixin
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
