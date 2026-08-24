"""Runtime credential handling for provider credentials (M10-008).

Provider credentials are swapped at runtime without downtime: adapters read
the credential from a :class:`CredentialSource` at REQUEST time instead of
capturing it at construction time, and the platform rotates the live value
behind an atomic holder after verifying the candidate against the provider.

Security:
- :class:`Credential` redacts the value in ``__repr__``/``__str__`` so it
  cannot leak through logs or tracebacks; only ``key_hint`` (a short
  NON-reversible fingerprint) may appear in audit metadata.
- The old credential is returned by :meth:`CredentialHolder.swap` only to
  the rotating service (which keeps it in memory to report its fingerprint);
  it is never written to logs or persisted.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any, Protocol

__all__ = [
    "Credential",
    "CredentialHolder",
    "CredentialSource",
    "credential_key_hint",
    "credential_verifier_of",
]

_HINT_LENGTH = 12


def credential_key_hint(value: str) -> str:
    """A short non-reversible fingerprint of a credential value.

    SHA-256 truncated to 12 hex chars: stable across rotations (the same
    value always yields the same hint) and not derivable back to the value.
    Safe for audit metadata; the value itself never is.
    """
    if not value:
        raise ValueError("credential value must not be empty")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_HINT_LENGTH]


@dataclass(frozen=True, slots=True)
class Credential:
    """One provider credential value plus its audit-safe fingerprint."""

    value: str
    key_hint: str

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("credential value must not be empty")
        if not self.key_hint:
            raise ValueError("key_hint must not be empty")

    def __repr__(self) -> str:
        return f"Credential(value=<redacted len={len(self.value)}>, key_hint={self.key_hint!r})"

    __str__ = __repr__


def credential_from_value(value: str) -> Credential:
    """Build a :class:`Credential` with a derived fingerprint."""
    if not value or not value.strip():
        raise ValueError("credential value must not be empty")
    return Credential(value=value, key_hint=credential_key_hint(value))


class CredentialSource(Protocol):
    """Port for the current credential of one provider.

    Adapters resolve this at request time, so a runtime swap changes the
    credential used by the very next request - no restart, no downtime.
    """

    async def get(self) -> Credential: ...


class CredentialHolder:
    """Atomic single-slot holder for one provider's live credential.

    In-process (per API process): :meth:`swap` is atomic under a lock, and
    every :meth:`get` observes either the fully-old or the fully-new
    credential, never a mix.
    """

    def __init__(self, initial: Credential | str) -> None:
        self._lock = asyncio.Lock()
        self._current: Credential = (
            initial if isinstance(initial, Credential) else credential_from_value(initial)
        )

    @property
    def key_hint(self) -> str:
        """The fingerprint of the CURRENT credential (safe to log/audit)."""
        return self._current.key_hint

    async def get(self) -> Credential:
        async with self._lock:
            return self._current

    async def swap(self, new: Credential) -> Credential:
        """Atomically replace the credential; returns the PREVIOUS one.

        In-flight requests keep the old value (the provider-side overlap
        window keeps it valid); all later requests use ``new``.
        """
        async with self._lock:
            previous, self._current = self._current, new
            return previous


def credential_verifier_of(provider: Any) -> Any | None:
    """The provider's optional ``verify_credential`` hook, if it has one.

    Optional port capability (same pattern as the power-effect probe):
    adapters that know how to prove a candidate credential read-only
    expose ``async verify_credential(value: str) -> None`` and raise
    ``ProviderAuthError`` (or any ``ProviderError``) when it is invalid.
    """
    method = getattr(provider, "verify_credential", None)
    return method if callable(method) else None
