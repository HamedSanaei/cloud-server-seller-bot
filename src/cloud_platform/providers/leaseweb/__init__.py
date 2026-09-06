"""LeaseWeb Cloud provider adapter public surface."""

from cloud_platform.providers.leaseweb.client import (
    LEASEWEBCLOUD_CAPABILITIES,
    LeaseWebProvider,
    Throttle,
    normalize_provider_status,
)

LeasewebCloudProvider = LeaseWebProvider
LEASEWEB_CAPABILITIES = LEASEWEBCLOUD_CAPABILITIES
LeaseWebThrottle = Throttle
normalize_leaseweb_status = normalize_provider_status

__all__ = [
    "LEASEWEBCLOUD_CAPABILITIES",
    "LEASEWEB_CAPABILITIES",
    "LeaseWebProvider",
    "LeaseWebThrottle",
    "LeasewebCloudProvider",
    "Throttle",
    "normalize_leaseweb_status",
    "normalize_provider_status",
]
