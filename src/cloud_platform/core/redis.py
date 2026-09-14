"""The ONE Redis client factory for the platform.

Every Redis consumer in the codebase (readiness probes, the bot session store,
the durable confirmation store) obtains its client here, so connection
configuration, decode settings and shutdown behaviour are defined once.

Redis holds only *disposable transient* state: Telegram references, pending
confirmations and replay markers. Financial correctness never depends on it
(that lives in PostgreSQL), but a Redis outage must **fail closed** for
security-sensitive flows — see
:class:`cloud_platform.core.session_store.SessionStoreUnavailable`.
"""

from __future__ import annotations

from typing import Any, Protocol

__all__ = [
    "RedisCommands",
    "close_redis_client",
    "create_redis_client",
]


class RedisCommands(Protocol):
    """The narrow command surface this codebase actually uses.

    Declaring it structurally keeps the session/confirmation stores testable
    with a plain fake (no server, no ``fakeredis`` dependency) while
    ``redis.asyncio.Redis`` satisfies it as-is.
    """

    async def get(self, name: str) -> Any: ...

    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> Any: ...

    async def delete(self, *names: str) -> Any: ...

    async def exists(self, *names: str) -> Any: ...

    async def expire(self, name: str, time: int) -> Any: ...

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any: ...

    async def ping(self) -> Any: ...

    async def aclose(self) -> Any: ...


def create_redis_client(url: str) -> Any:
    """Create a decoded-string ``redis.asyncio`` client for ``url``.

    ``decode_responses=True`` is mandatory: every stored value is JSON we wrote
    ourselves, and the stores compare/merge those documents as text.
    """
    from redis.asyncio import from_url

    return from_url(url, decode_responses=True)  # type: ignore[no-untyped-call]


async def close_redis_client(client: Any) -> None:
    """Close ``client`` if it exposes ``aclose`` (never raises to the caller)."""
    closer = getattr(client, "aclose", None)
    if closer is None:
        return
    try:
        await closer()
    except Exception:  # pragma: no cover - shutdown must never raise
        pass
