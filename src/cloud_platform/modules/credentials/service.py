"""Credential rotation service (M10-008).

Acceptance: provider credential can rotate without downtime.

The workflow (verify-first, atomic swap, audited):

1. Resolve the provider and its live :class:`CredentialHolder` (both must
   exist - a provider without a holder cannot be rotated at runtime).
2. Verify the CANDIDATE credential against the provider with a read-only
   call (the adapter's optional ``verify_credential`` hook). Any failure
   aborts BEFORE anything changes - the live credential keeps serving, so
   a failed rotation never causes downtime.
3. Atomically swap the holder: in-flight requests keep the old value
   (provider-side overlap window), the next request uses the new one.
4. Audit ``credential.rotate`` with fingerprints only (key hints), never
   the credential value.
"""

from __future__ import annotations

from uuid import UUID

from cloud_platform.modules.audit.domain import ActorType, AuditRepository
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.credentials.domain import (
    CredentialHolderRegistry,
    CredentialNotVerifiableError,
    CredentialStatus,
    CredentialVerifyError,
    ProviderCredentialNotFoundError,
    RotationResult,
    _actor_context,
    _require_reason,
)
from cloud_platform.providers.credentials import (
    credential_from_value,
    credential_verifier_of,
)
from cloud_platform.providers.errors import ProviderError
from cloud_platform.providers.registry import ProviderRegistry

_RESOURCE_TYPE = "provider_credential"


class CredentialRotationService:
    """Application-layer rotation of live provider credentials.

    Authorization is enforced by the caller boundary (admin API, M14-003)
    exactly like the other admin services: this service records WHO did it
    and WHY, and refuses non-system actors without an id.
    """

    def __init__(
        self,
        holders: CredentialHolderRegistry,
        provider_registry: ProviderRegistry,
        audit_repo: AuditRepository,
    ) -> None:
        self._holders = holders
        self._providers = provider_registry
        self._audit = AuditTrail(audit_repo)

    async def status(self, provider_key: str) -> CredentialStatus:
        """The current credential fingerprint for one provider (no value)."""
        holder = self._holders.get_holder(provider_key)
        if holder is None:
            raise ProviderCredentialNotFoundError(
                f"no credential holder for provider {provider_key!r}"
            )
        return CredentialStatus(provider_key=provider_key, key_hint=holder.key_hint)

    async def rotate(
        self,
        *,
        provider_key: str,
        new_credential_value: str,
        reason: str,
        actor_type: ActorType,
        actor_id: UUID | None,
    ) -> RotationResult:
        """Verify-then-swap the live credential of one provider."""
        _require_reason(reason)
        actor_type, actor_id = _actor_context(actor_type, actor_id)

        provider: object
        try:
            provider = self._providers.get(provider_key)
        except KeyError as exc:
            raise ProviderCredentialNotFoundError(
                f"provider {provider_key!r} is not configured"
            ) from exc
        holder = self._holders.get_holder(provider_key)
        if holder is None:
            raise ProviderCredentialNotFoundError(
                f"provider {provider_key!r} has no runtime credential holder; "
                "its credential cannot be rotated without a restart"
            )

        new_credential = credential_from_value(new_credential_value)
        verifier = credential_verifier_of(provider)
        if verifier is None:
            raise CredentialNotVerifiableError(
                f"provider {provider_key!r} cannot verify a candidate credential; "
                "refusing an unverified swap"
            )

        # Verify FIRST: the candidate is proven read-only while the old
        # credential keeps serving. Any failure means: nothing changed.
        try:
            await verifier(new_credential.value)
        except ProviderError as exc:
            raise CredentialVerifyError(
                f"candidate credential rejected by {provider_key}: {exc}"
            ) from exc

        previous = await holder.swap(new_credential)
        await self._audit.record_mutation(
            actor_type=actor_type,
            actor_id=actor_id,
            action="credential.rotate",
            resource_type=_RESOURCE_TYPE,
            resource_id=provider_key,
            reason=reason,
            metadata={
                "previous_key_hint": previous.key_hint,
                "new_key_hint": new_credential.key_hint,
            },
        )
        return RotationResult(
            provider_key=provider_key,
            previous_key_hint=previous.key_hint,
            new_key_hint=new_credential.key_hint,
        )
