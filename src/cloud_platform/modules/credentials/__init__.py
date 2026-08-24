"""Credential rotation module (M10-008)."""

from cloud_platform.modules.credentials.domain import (
    CredentialNotVerifiableError,
    CredentialRotationError,
    CredentialStatus,
    CredentialVerifyError,
    ProviderCredentialNotFoundError,
    RotationResult,
)
from cloud_platform.modules.credentials.service import CredentialRotationService

__all__ = [
    "CredentialNotVerifiableError",
    "CredentialRotationError",
    "CredentialRotationService",
    "CredentialStatus",
    "CredentialVerifyError",
    "ProviderCredentialNotFoundError",
    "RotationResult",
]
