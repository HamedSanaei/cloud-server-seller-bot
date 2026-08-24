"""SSH key domain (M13-001).

Acceptance: user keys are ownership-scoped.

An ``SshKey`` belongs to exactly ONE user; every service operation takes
the acting user and enforces ownership in the APPLICATION layer - another
user's keys look like they do not exist. Public key material is validated
(OpenSSH wire format) and fingerprinted (OpenSSH-style SHA256, which is
derived from PUBLIC material only - no secret ever enters this module).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


class SshKeyError(Exception):
    """Base class for SSH-key errors."""


class InvalidPublicKeyError(SshKeyError):
    """The supplied string is not a supported OpenSSH public key."""


class DuplicateSshKeyError(SshKeyError):
    """The user already registered a key with this name or fingerprint."""


class SshKeyLimitError(SshKeyError):
    """The per-user key quota is exhausted."""


class SshKeyNotFoundError(SshKeyError):
    """No such key FOR THIS USER (other users' keys do not exist here)."""


#: Supported OpenSSH public-key types (first wire field).
SUPPORTED_KEY_TYPES: frozenset[str] = frozenset(
    {
        "ssh-ed25519",
        "ssh-rsa",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
    }
)

#: Hard upper bound on the decoded key blob (64 KiB is far beyond any real key).
_MAX_BLOB_BYTES = 64 * 1024

#: Per-user registration quota (a spam guard, not a product feature).
MAX_KEYS_PER_USER = 20

MAX_NAME_LENGTH = 64


def parse_public_key(public_key: str) -> tuple[str, bytes]:
    """Split an OpenSSH public key into (type, raw blob); raise on garbage.

    Format: ``<type> <base64 blob> [comment]``. The comment may be absent
    or contain spaces.
    """
    parts = (public_key or "").strip().split(None, 2)
    if len(parts) < 2:
        raise InvalidPublicKeyError("public key must be '<type> <base64> [comment]'")
    key_type, encoded = parts[0], parts[1]
    if key_type not in SUPPORTED_KEY_TYPES:
        raise InvalidPublicKeyError(f"unsupported key type {key_type!r}")
    try:
        blob = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidPublicKeyError("key body is not valid base64") from exc
    if not blob:
        raise InvalidPublicKeyError("key body decodes to nothing")
    if len(blob) > _MAX_BLOB_BYTES:
        raise InvalidPublicKeyError("key body unreasonably large")
    return key_type, blob


def compute_fingerprint(public_key: str) -> str:
    """OpenSSH-style SHA256 fingerprint of the PUBLIC key blob.

    ``SHA256:<base64(no padding)>`` - derived from public material only.
    """
    _key_type, blob = parse_public_key(public_key)
    digest = hashlib.sha256(blob).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


@dataclass(slots=True)
class SshKey:
    """One user-owned public key, ready to sync to providers."""

    user_id: UUID
    name: str
    public_key: str
    id: UUID | None = None
    fingerprint: str = ""
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        self.name = (self.name or "").strip()
        if not self.name:
            raise ValueError("ssh key name must not be empty")
        if len(self.name) > MAX_NAME_LENGTH:
            raise ValueError(f"ssh key name longer than {MAX_NAME_LENGTH} characters")
        # normalizes whitespace AND validates the format
        parts = (self.public_key or "").strip().split(None, 2)
        if len(parts) < 2:
            raise InvalidPublicKeyError("public key must be '<type> <base64> [comment]'")
        self.public_key = f"{parts[0]} {parts[1]}"
        self.fingerprint = compute_fingerprint(self.public_key)
