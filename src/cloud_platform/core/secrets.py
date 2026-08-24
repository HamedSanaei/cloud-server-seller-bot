"""Encrypted credential envelope interface.

Provider credentials must never be persisted in plaintext. This module defines
the envelope contract used across the platform plus a Fernet-backed reference
implementation. Only ``SecretEnvelope`` values are safe to store; plaintext
exists solely in memory for the duration of a provider call.

Security notes:
- ``MasterKey`` and ``SecretEnvelope`` implement redacted ``__repr__``/``__str__``
  so secrets cannot leak through logs or tracebacks.
- Decryption failures raise :class:`SecretBoxError` so callers never need to
  import ``cryptography`` exceptions themselves.
"""

from __future__ import annotations

import hashlib
import os
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken

__all__ = [
    "EnvelopeService",
    "FernetSecretBox",
    "MasterKey",
    "SecretBox",
    "SecretBoxError",
    "SecretEnvelope",
    "master_key_from_env",
]

_FERNET_ALGORITHM = "Fernet"
_KEY_ID_LENGTH = 12


class SecretBoxError(RuntimeError):
    """Raised when sealing/unsealing credentials fails."""


def _fingerprint(material: bytes) -> str:
    """Derive a short non-reversible key fingerprint."""
    return hashlib.sha256(material).hexdigest()[:_KEY_ID_LENGTH]


@dataclass(frozen=True, slots=True)
class MasterKey:
    """32-byte symmetric master key with redacted representation."""

    material: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.material, bytes) or len(self.material) != 32:
            raise ValueError("master key material must be exactly 32 bytes")

    @classmethod
    def generate(cls) -> MasterKey:
        """Generate a fresh random 32-byte key."""
        return cls(material=os.urandom(32))

    @classmethod
    def from_passphrase(cls, passphrase: str) -> MasterKey:
        """Derive a deterministic 32-byte key from a passphrase via SHA-256."""
        if not passphrase:
            raise ValueError("passphrase must not be empty")
        return cls(material=hashlib.sha256(passphrase.encode("utf-8")).digest())

    def __repr__(self) -> str:
        return f"MasterKey(<redacted sha256:{_fingerprint(self.material)}>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class SecretEnvelope:
    """Persistable encrypted credential container.

    ``token`` is an opaque ciphertext (url-safe base64 for Fernet); it is what
    gets stored in the database or secret store.
    """

    token: str
    key_id: str
    algorithm: str = _FERNET_ALGORITHM

    def __repr__(self) -> str:
        return (
            f"SecretEnvelope(token=<redacted len={len(self.token)}>, "
            f"key_id={self.key_id!r}, algorithm={self.algorithm!r})"
        )

    __str__ = __repr__


class SecretBox(Protocol):
    """Port for encrypting/decrypting credential envelopes."""

    @property
    def key_id(self) -> str:
        """Stable identifier of the active master key."""
        ...

    def encrypt(self, plaintext: str, *, key_id: str | None = None) -> SecretEnvelope:
        """Encrypt plaintext into a persistable envelope."""
        ...

    def decrypt(self, envelope: SecretEnvelope) -> str:
        """Decrypt an envelope back to plaintext."""
        ...


class FernetSecretBox:
    """Fernet-based :class:`SecretBox` reference implementation."""

    def __init__(self, master_key: MasterKey) -> None:
        self._key_id = _fingerprint(master_key.material)
        self._fernet = Fernet(urlsafe_b64encode(master_key.material))

    @property
    def key_id(self) -> str:
        return self._key_id

    def encrypt(self, plaintext: str, *, key_id: str | None = None) -> SecretEnvelope:
        del key_id  # Single-key box; key_id parameter kept for port symmetry.
        try:
            token = self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
        except Exception as exc:  # pragma: no cover - defensive
            raise SecretBoxError(f"encryption failed: {type(exc).__name__}") from exc
        return SecretEnvelope(token=token, key_id=self._key_id)

    def decrypt(self, envelope: SecretEnvelope) -> str:
        if envelope.algorithm != _FERNET_ALGORITHM:
            raise SecretBoxError(f"unsupported envelope algorithm: {envelope.algorithm}")
        try:
            plaintext = self._fernet.decrypt(envelope.token.encode("ascii"))
        except InvalidToken as exc:
            raise SecretBoxError("decryption failed: invalid token or wrong key") from exc
        except (ValueError, UnicodeEncodeError) as exc:
            raise SecretBoxError(f"malformed envelope: {type(exc).__name__}") from exc
        return plaintext.decode("utf-8")

    def encrypt_bytes(self, plaintext: bytes) -> str:
        """Encrypt arbitrary bytes (e.g. a database dump) into a Fernet token."""
        try:
            return self._fernet.encrypt(plaintext).decode("ascii")
        except Exception as exc:  # pragma: no cover - defensive
            raise SecretBoxError(f"encryption failed: {type(exc).__name__}") from exc

    def decrypt_bytes(self, token: str) -> bytes:
        """Decrypt a Fernet token back to bytes."""
        try:
            return self._fernet.decrypt(token.encode("ascii"))
        except InvalidToken as exc:
            raise SecretBoxError("decryption failed: invalid token or wrong key") from exc


class EnvelopeService:
    """Convenience facade over two boxes for sealing and rotation."""

    def __init__(self, box: SecretBox) -> None:
        self._box = box

    @property
    def key_id(self) -> str:
        return self._box.key_id

    def seal(self, name: str, plaintext: str) -> SecretEnvelope:
        """Encrypt a named credential; ``name`` is metadata only and never embedded."""
        del name
        return self._box.encrypt(plaintext)

    def open(self, name: str, envelope: SecretEnvelope) -> str:
        """Decrypt a named credential."""
        del name
        return self._box.decrypt(envelope)

    def rotate(
        self, old_box: SecretBox, new_box: SecretBox, envelope: SecretEnvelope
    ) -> SecretEnvelope:
        """Re-encrypt an envelope under a new master key."""
        plaintext = old_box.decrypt(envelope)
        return new_box.encrypt(plaintext)


def master_key_from_env(var: str = "CLOUD_PLATFORM_SECRET_MASTER_KEY") -> MasterKey:
    """Load the master key from an environment variable (never logged)."""
    value = os.environ.get(var)
    if not value:
        raise SecretBoxError(
            f"environment variable {var} is not set; provide a 32-byte "
            "url-safe base64 master key to enable credential encryption"
        )
    import base64

    try:
        material = base64.urlsafe_b64decode(value.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise SecretBoxError(f"environment variable {var} is not valid base64") from exc
    try:
        return MasterKey(material=material)
    except ValueError as exc:
        raise SecretBoxError(f"environment variable {var} must decode to exactly 32 bytes") from exc
