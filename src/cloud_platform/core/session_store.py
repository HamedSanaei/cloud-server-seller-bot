"""Redis-backed transient state for the Telegram bot (PROD-HARDENING §2-§11).

Telegram transient state — server callback references, remembered selections,
pending (already-confirmed) actions, free-text prompts and single-use
confirmation markers — used to live in a per-process ``dict``. That is not safe
for a real deployment: a container restart loses every button the customer is
holding, and two bot processes disagree about what a reference means.

This module defines the **port** (:class:`BotSessionStore`) and two adapters:

- :class:`RedisBotSessionStore` — the production backend. Keys are namespaced
  and versioned (``<prefix>:<namespace>:v1[:…]``), every write carries a TTL,
  and single-use consumption uses server-side scripts so it is atomic across
  processes.
- :class:`InMemoryBotSessionStore` — deterministic, single-process backend for
  unit tests and an explicitly configured development mode. It is **never**
  selected implicitly in production (see ``build_session_store``).

Security posture: transient state holds **no** provider secret, credential,
console URL, password or API key. Redis values are JSON we wrote ourselves;
:func:`decode_session_value` rejects anything else rather than trusting a
payload that may have been written by a different schema version.

Failure policy: :class:`SessionStoreUnavailable` is raised when the backend
cannot answer. Callers must treat it as *fail closed* — never fall back to
process-local state and never guess an operation's target.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from typing import Any, Protocol

from cloud_platform.core.redis import RedisCommands

__all__ = [
    "NS_ACTION",
    "NS_CONFIRMATION",
    "NS_PROMPT",
    "NS_REFERENCE",
    "NS_SELECTION",
    "NS_SERVER",
    "SCHEMA_VERSION",
    "BotSessionStore",
    "InMemoryBotSessionStore",
    "RedisBotSessionStore",
    "SessionStoreError",
    "SessionStoreUnavailable",
    "build_session_store",
    "decode_session_value",
    "encode_session_value",
    "session_key",
]

logger = logging.getLogger(__name__)

#: Schema version embedded in every key AND every payload. Bumping it isolates
#: a rollout: old keys stop resolving (they simply expire) instead of being
#: misread under new semantics.
SCHEMA_VERSION = "v1"

#: Namespaces. A namespace is part of the key, so a single Redis database can
#: hold several independent families without collisions.
NS_REFERENCE = "ref"
NS_SERVER = "server"
NS_SELECTION = "selection"
NS_ACTION = "action"
NS_PROMPT = "prompt"
NS_CONFIRMATION = "confirm"
NS_REPLAY = "replay"

#: Namespaces whose values are stored verbatim (markers), not JSON documents.
_OPAQUE_NAMESPACES = frozenset({NS_CONFIRMATION, NS_REPLAY})

#: Atomic "set only if absent", with TTL. Returns 1 when the caller won.
_CLAIM_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  return 0
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return 1
"""

#: Atomic "read and delete" (GETDEL semantics, independent of Redis version).
_TAKE_SCRIPT = """
local value = redis.call('GET', KEYS[1])
if value == false then
  return false
end
redis.call('DEL', KEYS[1])
return value
"""


class SessionStoreError(RuntimeError):
    """Base error for the transient-state backend."""


class SessionStoreUnavailable(SessionStoreError):
    """The backend could not be reached or answered with an error.

    Callers MUST fail closed: refuse the operation, show the customer a safe
    "try again shortly" message and never execute a provider mutation. The
    exception message carries only the backend exception class name — never a
    stored payload, key material or connection string.
    """


def session_key(prefix: str, namespace: str, *parts: str) -> str:
    """Build a namespaced, versioned key: ``<prefix>:<namespace>:v1[:parts]``.

    ``parts`` are restricted to URL-safe identifier characters by the callers
    (UUIDs, 8-character references, nonces), so a key never interpolates a
    secret, a console URL or a free-text value.
    """
    suffix = "".join(f":{part}" for part in parts)
    return f"{prefix}:{namespace}:{SCHEMA_VERSION}{suffix}"


def encode_session_value(value: Mapping[str, Any]) -> str:
    """Serialize a store record as a self-describing JSON document."""
    document = {"schema": SCHEMA_VERSION, "value": dict(value)}
    return json.dumps(document, separators=(",", ":"), sort_keys=True, default=str)


def decode_session_value(raw: Any) -> dict[str, Any] | None:
    """Parse a stored JSON document; ``None`` for anything unrecognisable.

    A malformed payload or an unknown schema version is logged (kind only —
    never the payload) and treated as absent, which degrades to the customer's
    "this button expired" screen instead of an exception.
    """
    if isinstance(raw, bytes):  # pragma: no cover - defensive: clients decode
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("session store: malformed JSON payload rejected")
        return None
    if not isinstance(parsed, dict) or "value" not in parsed:
        logger.warning("session store: payload without a value envelope rejected")
        return None
    schema = parsed.get("schema")
    if schema != SCHEMA_VERSION:
        logger.warning("session store: unknown schema version rejected")
        return None
    value = parsed["value"]
    if not isinstance(value, dict):
        logger.warning("session store: non-object payload rejected")
        return None
    return value


class BotSessionStore(Protocol):
    """Port: the transient-state backend the bot renders from."""

    async def put(
        self, namespace: str, key: str, value: Mapping[str, Any], *, ttl_seconds: int
    ) -> None:
        """Store ``value`` under ``key`` for at most ``ttl_seconds``."""
        ...

    async def get(self, namespace: str, key: str) -> dict[str, Any] | None:
        """Read a record, or ``None`` when absent/expired/malformed."""
        ...

    async def claim(
        self, namespace: str, key: str, value: Mapping[str, Any], *, ttl_seconds: int
    ) -> bool:
        """Atomically store ``value`` **only if** ``key`` is absent.

        ``True`` means this caller won the claim. This is the primitive behind
        single-use consumption: exactly one of two concurrent callbacks can
        observe ``True``.
        """
        ...

    async def take(self, namespace: str, key: str) -> dict[str, Any] | None:
        """Atomically read AND delete ``key`` (``None`` when it was not there)."""
        ...

    async def delete(self, namespace: str, key: str) -> None:
        """Remove ``key`` if present."""
        ...

    async def exists(self, namespace: str, key: str) -> bool:
        """Whether ``key`` currently exists."""
        ...

    async def ping(self) -> None:
        """Round-trip the backend, raising when it is unavailable."""
        ...


class InMemoryBotSessionStore:
    """Single-process backend for tests and explicit development mode.

    Semantics match the Redis adapter (namespaced keys, TTL, atomic claim/take).
    It carries no locking because a single asyncio event loop is assumed, and
    every `claim`/`take` mutates its dict **without** an ``await`` in between.
    """

    def __init__(self, *, prefix: str = "cloud-platform:bot") -> None:
        self._prefix = prefix
        self._values: dict[str, tuple[str, float]] = {}

    def _storage_key(self, namespace: str, key: str) -> str:
        return session_key(self._prefix, namespace, *key.split(":"))

    def _prune(self, now: float) -> None:
        expired = [key for key, (_, deadline) in self._values.items() if deadline <= now]
        for key in expired:
            self._values.pop(key, None)

    async def put(
        self, namespace: str, key: str, value: Mapping[str, Any], *, ttl_seconds: int
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._values[self._storage_key(namespace, key)] = (
            encode_session_value(value),
            time.monotonic() + ttl_seconds,
        )

    async def get(self, namespace: str, key: str) -> dict[str, Any] | None:
        self._prune(time.monotonic())
        stored = self._values.get(self._storage_key(namespace, key))
        return None if stored is None else decode_session_value(stored[0])

    async def claim(
        self, namespace: str, key: str, value: Mapping[str, Any], *, ttl_seconds: int
    ) -> bool:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        now = time.monotonic()
        self._prune(now)
        storage_key = self._storage_key(namespace, key)
        if storage_key in self._values:
            return False
        # No await between the check and the insert: atomic for one event loop.
        self._values[storage_key] = (encode_session_value(value), now + ttl_seconds)
        return True

    async def take(self, namespace: str, key: str) -> dict[str, Any] | None:
        self._prune(time.monotonic())
        stored = self._values.pop(self._storage_key(namespace, key), None)
        return None if stored is None else decode_session_value(stored[0])

    async def delete(self, namespace: str, key: str) -> None:
        self._values.pop(self._storage_key(namespace, key), None)

    async def exists(self, namespace: str, key: str) -> bool:
        self._prune(time.monotonic())
        return self._storage_key(namespace, key) in self._values

    async def ping(self) -> None:
        return None

    def clear(self) -> None:
        """Drop every stored entry (test helper: simulates Redis data loss)."""
        self._values.clear()


class RedisBotSessionStore:
    """Production backend: shared, namespaced and TTL-bounded.

    Every operation surfaces backend failures as
    :class:`SessionStoreUnavailable` (with the backend exception class name
    only), so callers can fail closed without ever seeing a driver traceback.
    """

    def __init__(self, client: RedisCommands, *, prefix: str = "cloud-platform:bot") -> None:
        self._client = client
        self._prefix = prefix

    @property
    def client(self) -> RedisCommands:
        """The underlying client (readiness probes and shutdown use it)."""
        return self._client

    def _key(self, namespace: str, key: str) -> str:
        return session_key(self._prefix, namespace, *key.split(":"))

    async def _call(self, operation: str, awaitable: Any) -> Any:
        try:
            return await awaitable
        except Exception as exc:
            logger.warning("session store %s failed: %s", operation, type(exc).__name__)
            raise SessionStoreUnavailable(f"session store {operation} failed") from None

    async def put(
        self, namespace: str, key: str, value: Mapping[str, Any], *, ttl_seconds: int
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        await self._call(
            "put",
            self._client.set(
                self._key(namespace, key), encode_session_value(value), ex=ttl_seconds
            ),
        )

    async def get(self, namespace: str, key: str) -> dict[str, Any] | None:
        raw = await self._call("get", self._client.get(self._key(namespace, key)))
        return decode_session_value(raw)

    async def claim(
        self, namespace: str, key: str, value: Mapping[str, Any], *, ttl_seconds: int
    ) -> bool:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        storage_key = self._key(namespace, key)
        if namespace in _OPAQUE_NAMESPACES:
            # Markers are opaque: the value is a literal string, not a document.
            payload = str(value.get("value", "1"))
        else:
            payload = encode_session_value(value)
        result = await self._call(
            "claim",
            self._client.eval(_CLAIM_SCRIPT, 1, storage_key, payload, str(ttl_seconds)),
        )
        return int(result or 0) == 1

    async def take(self, namespace: str, key: str) -> dict[str, Any] | None:
        storage_key = self._key(namespace, key)
        raw = await self._call("take", self._client.eval(_TAKE_SCRIPT, 1, storage_key))
        return decode_session_value(raw)

    async def delete(self, namespace: str, key: str) -> None:
        await self._call("delete", self._client.delete(self._key(namespace, key)))

    async def exists(self, namespace: str, key: str) -> bool:
        result = await self._call("exists", self._client.exists(self._key(namespace, key)))
        return int(result or 0) > 0

    async def ping(self) -> None:
        await self._call("ping", self._client.ping())


def build_session_store(*, backend: str, prefix: str, redis_url: str) -> BotSessionStore:
    """Select the store for ``backend`` (``redis`` or ``memory``).

    ``memory`` is refused outside development/test so a misconfigured
    production deployment cannot silently run with process-local state.
    """
    normalised = (backend or "").strip().lower()
    if normalised == "redis":
        from cloud_platform.core.redis import create_redis_client

        return RedisBotSessionStore(create_redis_client(redis_url), prefix=prefix)
    if normalised == "memory":
        from cloud_platform.core.config import get_settings

        environment = (get_settings().app_env or "").strip().lower()
        if environment not in ("development", "test", "local", "testing"):
            raise ValueError(
                "the in-memory Telegram session backend is only allowed in "
                f"development/test (APP_ENV={environment!r}); configure "
                "[telegram.sessions] backend = 'redis'"
            )
        return InMemoryBotSessionStore(prefix=prefix)
    raise ValueError(f"unknown Telegram session backend {backend!r}")
