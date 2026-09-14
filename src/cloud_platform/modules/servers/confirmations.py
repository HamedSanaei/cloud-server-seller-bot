"""One-time confirmation tokens for destructive customer operations (§12).

A Telegram callback is not a confirmation. ``confirm=true`` in button data
would be forgeable, replayable and unbound to what is being confirmed. This
module issues instead a **short-lived, signed, single-use token** bound to:

- the customer (``customer_id``),
- the local server (``server_id``),
- the operation (a :class:`~cloud_platform.modules.servers.models.ServerOperation`),
- a digest of the operation *arguments* (image ref, snapshot ref, IP, new
  name — so editing the button cannot change what gets executed),
- an expiry timestamp, and
- a random nonce that makes the token single-use.

Verification distinguishes the failure modes so the customer gets an honest
message: ``EXPIRED`` ("request a new confirmation"), ``REPLAYED`` ("already
executed"), ``MISMATCH`` (the button does not belong to this server/customer/
operation/arguments), ``INVALID`` (tampered or malformed) and ``UNAVAILABLE``
(the shared store cannot be reached — the operation must NOT run). A replayed
confirmation is never executed a second time — this is the guarantee the
double-click tests assert.

The store is a port; :class:`RedisConfirmationStore` backs the production
bot (durable across restarts and shared between replicas) and
:class:`InMemoryConfirmationStore` is the single-process test/development
backend. Redis consumption is a single server-side script, so two concurrent
callbacks for the same nonce cannot both win.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from cloud_platform.core.session_store import (
    NS_CONFIRMATION,
    BotSessionStore,
    SessionStoreUnavailable,
)
from cloud_platform.modules.servers.models import ServerOperation

__all__ = [
    "ConfirmationBinding",
    "ConfirmationResult",
    "ConfirmationStatus",
    "ConfirmationStore",
    "ConfirmationToken",
    "ConfirmationVerifier",
    "InMemoryConfirmationStore",
    "SharedConfirmationStore",
    "arguments_digest",
]

_TOKEN_VERSION = "v1"
_SIGNATURE_LEN = 16

logger = logging.getLogger(__name__)


class ConfirmationStatus(StrEnum):
    """The outcome of verifying (and consuming) a confirmation token."""

    OK = "ok"
    EXPIRED = "expired"
    REPLAYED = "replayed"
    MISMATCH = "mismatch"
    INVALID = "invalid"
    #: The shared confirmation store could not be reached. Fail CLOSED: the
    #: operation is refused, never executed on a guess.
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ConfirmationBinding:
    """What a confirmation is bound to.

    A token only validates against the exact same binding, which is what makes
    a "wrong server/customer/arguments" replay impossible.
    """

    customer_id: UUID
    server_id: UUID
    operation: ServerOperation
    arguments: Mapping[str, Any] | None = None

    @property
    def digest(self) -> str:
        return arguments_digest(self.operation, self.arguments)


@dataclass(frozen=True, slots=True)
class ConfirmationToken:
    """An issued confirmation: the wire token plus its metadata."""

    token: str
    nonce: str
    expires_at: datetime

    def is_expired(self, *, now: datetime | None = None) -> bool:
        return self.expires_at <= (now or datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class ConfirmationResult:
    """The result of consuming a token (never carries the operation payload)."""

    status: ConfirmationStatus
    binding: ConfirmationBinding | None = None

    @property
    def ok(self) -> bool:
        return self.status is ConfirmationStatus.OK


class ConfirmationStore(Protocol):
    """Port: remembers consumed nonces so a token is single-use."""

    async def consume(self, nonce: str, *, expires_at: datetime, now: datetime) -> bool:
        """Atomically mark ``nonce`` used; False when it was used already."""
        ...

    async def is_consumed(self, nonce: str) -> bool:
        """Whether ``nonce`` has already been consumed."""
        ...


class InMemoryConfirmationStore:
    """Single-process :class:`ConfirmationStore` (default deployment).

    Expired entries are swept opportunistically, so a long-running bot cannot
    grow without bound. The process restarting loses the consumed set, which
    only ever means a *not-yet-expired* token could be replayed once — the
    operation itself remains protected by the command/idempotency layer.
    """

    def __init__(self, *, max_entries: int = 10_000) -> None:
        self._consumed: dict[str, datetime] = {}
        self._max_entries = max(1, max_entries)

    def _sweep(self, now: datetime) -> None:
        if len(self._consumed) < self._max_entries:
            return
        self._consumed = {nonce: expiry for nonce, expiry in self._consumed.items() if expiry > now}

    async def consume(self, nonce: str, *, expires_at: datetime, now: datetime) -> bool:
        # No ``await`` between the check and the insert: in a single event loop
        # this is atomic, so two callbacks for one nonce cannot both succeed.
        self._sweep(now)
        if nonce in self._consumed:
            return False
        self._consumed[nonce] = expires_at
        return True

    async def is_consumed(self, nonce: str) -> bool:
        return nonce in self._consumed


class SharedConfirmationStore:
    """Durable, multi-process :class:`ConfirmationStore`.

    Backed by the shared :class:`~cloud_platform.core.session_store.BotSessionStore`
    (Redis in production).

    The single-use marker *is* the replay marker: one namespaced key per nonce,
    written with an atomic "set only if absent" claim and a TTL that covers
    both the token's remaining lifetime and the longer replay window. So

    - the first consumption wins in Redis itself (not in process memory),
    - a restart cannot resurrect a consumed token, and
    - a second replica answers ``REPLAYED`` rather than executing again.

    A backend failure raises
    :class:`~cloud_platform.core.session_store.SessionStoreUnavailable`, which
    :class:`ConfirmationVerifier` turns into ``UNAVAILABLE`` — the operation is
    refused instead of falling back to local state.
    """

    def __init__(self, store: BotSessionStore, *, replay_ttl_seconds: int = 1800) -> None:
        if replay_ttl_seconds <= 0:
            raise ValueError("replay_ttl_seconds must be positive")
        self._store = store
        self._replay_ttl = replay_ttl_seconds

    async def consume(self, nonce: str, *, expires_at: datetime, now: datetime) -> bool:
        remaining = int((expires_at - now).total_seconds())
        ttl = max(remaining, self._replay_ttl)
        # The claim is atomic in Redis: exactly one concurrent caller sees True.
        return await self._store.claim(NS_CONFIRMATION, nonce, {"value": "1"}, ttl_seconds=ttl)

    async def is_consumed(self, nonce: str) -> bool:
        return await self._store.exists(NS_CONFIRMATION, nonce)


def arguments_digest(operation: ServerOperation, arguments: Mapping[str, Any] | None) -> str:
    """Canonical digest of an operation's arguments.

    Values are stringified and sorted, so the digest is stable across a
    round-trip through a Telegram callback while any *change* to the arguments
    invalidates the token. Only identifiers belong in here — never a secret.
    """
    payload = {
        str(key): str(value)
        for key, value in sorted((arguments or {}).items())
        if value is not None and value != ""
    }
    raw = json.dumps(
        {"op": operation.value, "args": payload}, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


class ConfirmationVerifier:
    """Issues and verifies one-time confirmation tokens.

    The signing key is the same server secret the callback signer uses, so a
    token never travels to Telegram in a forgeable form: the bot's buttons
    carry only the *screen* identity, and the token itself is held by the
    application service between the confirmation screen and the execution.
    """

    def __init__(
        self,
        signing_key: str,
        *,
        store: ConfirmationStore | None = None,
        ttl_seconds: int = 900,
    ) -> None:
        if not signing_key:
            raise ValueError("signing_key must not be empty")
        if ttl_seconds < 30:
            raise ValueError("ttl_seconds must be >= 30")
        self._key = signing_key.encode("utf-8")
        self._store: ConfirmationStore = store or InMemoryConfirmationStore()
        self._ttl = ttl_seconds

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    def issue(
        self, binding: ConfirmationBinding, *, now: datetime | None = None
    ) -> ConfirmationToken:
        """Mint a single-use token for ``binding``."""
        moment = now or datetime.now(UTC)
        expires_at = moment + timedelta(seconds=self._ttl)
        nonce = secrets.token_urlsafe(12)
        payload = {
            "c": str(binding.customer_id),
            "s": str(binding.server_id),
            "o": binding.operation.value,
            "d": binding.digest,
            "e": int(expires_at.timestamp()),
            "n": nonce,
        }
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        body = _b64encode(raw)
        return ConfirmationToken(
            token=f"{_TOKEN_VERSION}.{body}.{self._sign(body)}",
            nonce=nonce,
            expires_at=expires_at,
        )

    async def consume(
        self,
        token: str,
        binding: ConfirmationBinding,
        *,
        now: datetime | None = None,
    ) -> ConfirmationResult:
        """Verify ``token`` against ``binding`` and consume it once."""
        moment = now or datetime.now(UTC)
        payload = self._payload(token)
        if payload is None:
            return ConfirmationResult(ConfirmationStatus.INVALID)

        try:
            expires_at = datetime.fromtimestamp(int(payload["e"]), tz=UTC)
            recorded = ConfirmationBinding(
                customer_id=UUID(str(payload["c"])),
                server_id=UUID(str(payload["s"])),
                operation=ServerOperation(str(payload["o"])),
                arguments=None,
            )
        except (KeyError, TypeError, ValueError):
            return ConfirmationResult(ConfirmationStatus.INVALID)

        if expires_at <= moment:
            return ConfirmationResult(ConfirmationStatus.EXPIRED, recorded)
        if not hmac.compare_digest(str(payload.get("d", "")), binding.digest):
            return ConfirmationResult(ConfirmationStatus.MISMATCH, recorded)
        if (
            recorded.customer_id != binding.customer_id
            or recorded.server_id != binding.server_id
            or recorded.operation != binding.operation
        ):
            return ConfirmationResult(ConfirmationStatus.MISMATCH, recorded)

        nonce = str(payload.get("n", ""))
        if not nonce:
            return ConfirmationResult(ConfirmationStatus.INVALID, recorded)
        try:
            consumed = await self._store.consume(nonce, expires_at=expires_at, now=moment)
        except SessionStoreUnavailable:
            # Fail closed: an unreachable store must never authorise a mutation.
            logger.error("confirmation store unavailable; refusing operation")
            return ConfirmationResult(ConfirmationStatus.UNAVAILABLE, recorded)
        if not consumed:
            return ConfirmationResult(ConfirmationStatus.REPLAYED, recorded)
        # Re-derive the binding the token actually carried, so the caller can
        # assert it never executes anything the customer did not confirm.
        return ConfirmationResult(
            ConfirmationStatus.OK,
            ConfirmationBinding(
                customer_id=recorded.customer_id,
                server_id=recorded.server_id,
                operation=recorded.operation,
                arguments=binding.arguments,
            ),
        )

    def _sign(self, body: str) -> str:
        return hmac.new(self._key, body.encode("ascii"), hashlib.sha256).hexdigest()[
            :_SIGNATURE_LEN
        ]

    def _payload(self, token: str) -> dict[str, Any] | None:
        parts = (token or "").split(".")
        if len(parts) != 3:
            return None
        version, body, signature = parts
        if version != _TOKEN_VERSION:
            return None
        if not hmac.compare_digest(signature, self._sign(body)):
            return None
        try:
            decoded = json.loads(_b64decode(body).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return decoded if isinstance(decoded, dict) else None
