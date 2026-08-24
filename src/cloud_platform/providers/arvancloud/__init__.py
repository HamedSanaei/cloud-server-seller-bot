"""ArvanCloud IaaS provider adapter (M15-002).

The adapter for the first Iranian provider, selected and contracted in
docs/iranian/PROVIDER_CONTRACT.md. The transport, error mapping, idempotency
guard, throttle, and status normalization live in ``client.py``; this module
exposes the public surface and the status table so tests and the
integration notes import one place.
"""

from cloud_platform.providers.arvancloud.client import (
    ARVANCLOUD_CAPABILITIES,
    ArvanCloudProvider,
    Throttle,
    normalize_provider_status,
    parse_server_id,
    qualify_server_id,
)

__all__ = [
    "ARVANCLOUD_CAPABILITIES",
    "ArvanCloudProvider",
    "Throttle",
    "normalize_provider_status",
    "parse_server_id",
    "qualify_server_id",
]
