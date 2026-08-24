"""Credential rotation domain (M10-008).

Provider credentials can be rotated WITHOUT DOWNTIME: the live credential
lives in a runtime :class:`~cloud_platform.providers.credentials.CredentialHolder`
that the adapter consults at request time. Rotation is verify-first:
the candidate credential is proven against the provider with a read-only
call BEFORE it is activated, and the swap is atomic - in-flight requests
keep the old value (the provider-side overlap window keeps it valid),
subsequent requests use the new one. No restart, no interruption.

Security: only key fingerprints (``key_hint``) ever appear in audit
metadata or results - never the credential value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from cloud_platform.modules.audit.domain import ActorType
from cloud_platform.providers.credentials import Credential


class CredentialRotationError(Exception):
    """Base error for the credential rotation workflow."""


class CredentialVerifyError(CredentialRotationError):
    """The candidate credential was rejected by the provider.

    Nothing was changed: the live credential keeps serving.
    """


class ProviderCredentialNotFoundError(CredentialRotationError):
    """No configured provider (and holder) for the requested key."""


class CredentialNotVerifiableError(CredentialRotationError):
    """The provider adapter cannot verify a candidate credential.

    Rotation is REFUSED rather than performed blind: an unverified swap
    would take the live credential offline with no fallback.
    """


@dataclass(frozen=True, slots=True)
class RotationResult:
    """Outcome of one rotation (audit-safe: fingerprints only)."""

    provider_key: str
    previous_key_hint: str
    new_key_hint: str


@dataclass(frozen=True, slots=True)
class CredentialStatus:
    """The current credential's identity for one provider (no value)."""

    provider_key: str
    key_hint: str


class CredentialHolderRegistry(Protocol):
    """Port: the live credential holder per provider key."""

    def get_holder(self, provider_key: str) -> CredentialHolderLike | None: ...


class CredentialHolderLike(Protocol):
    """The subset of the holder the rotation service needs."""

    @property
    def key_hint(self) -> str: ...

    async def get(self) -> Credential: ...

    async def swap(self, new: Credential) -> Credential: ...


def _require_reason(reason: str) -> None:
    if not reason or not reason.strip():
        raise CredentialRotationError("credential rotation requires a non-empty reason")


def _actor_context(actor_type: ActorType, actor_id: UUID | None) -> tuple[ActorType, UUID | None]:
    if actor_id is None and actor_type is not ActorType.SYSTEM:
        raise CredentialRotationError("non-system rotations must carry an actor id")
    return actor_type, actor_id
