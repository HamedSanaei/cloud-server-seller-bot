"""SSH keys module (M13-001): ownership-scoped user SSH keys + provider sync."""

from .domain import (
    DuplicateSshKeyError,
    InvalidPublicKeyError,
    SshKey,
    SshKeyError,
    SshKeyLimitError,
    SshKeyNotFoundError,
    compute_fingerprint,
    parse_public_key,
)
from .repository import SqlAlchemySshKeyRepository
from .service import (
    ProviderSshKeyPort,
    SshKeyRepository,
    SshKeyService,
    provider_key_name,
    ssh_key_port_of,
    sync_keys_to_provider,
)

__all__ = [
    "DuplicateSshKeyError",
    "InvalidPublicKeyError",
    "ProviderSshKeyPort",
    "SqlAlchemySshKeyRepository",
    "SshKey",
    "SshKeyError",
    "SshKeyLimitError",
    "SshKeyNotFoundError",
    "SshKeyRepository",
    "SshKeyService",
    "compute_fingerprint",
    "parse_public_key",
    "provider_key_name",
    "ssh_key_port_of",
    "sync_keys_to_provider",
]
