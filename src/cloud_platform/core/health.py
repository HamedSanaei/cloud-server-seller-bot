"""Dependency readiness checks.

Provides readiness probes for PostgreSQL and Redis that FAIL when a required
dependency is unavailable. Suitable for ``/ready`` endpoints and worker startup
gating. Check details are sanitized by construction: they contain only the
exception class name and a truncated message, never credentials.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "CheckResult",
    "PostgresReadiness",
    "ReadinessCheck",
    "ReadinessReport",
    "ReadinessStatus",
    "RedisReadiness",
    "check_readiness",
]

_DETAIL_LIMIT = 200


class ReadinessStatus(StrEnum):
    """Outcome of an individual readiness check."""

    READY = "ready"
    UNREADY = "unready"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Result of one readiness probe.

    ``detail`` is safe for logging: exception class name plus a short,
    truncated message only — never connection strings or credentials.
    """

    name: str
    status: ReadinessStatus
    latency_ms: float
    detail: str = ""


class ReadinessCheck(Protocol):
    """Structural port implemented by every readiness probe."""

    @property
    def name(self) -> str:
        """Stable check identifier."""
        ...

    async def check(self) -> CheckResult:
        """Run the probe and return its result."""
        ...


def _sanitize_detail(exc: BaseException) -> str:
    message = str(exc).replace("\n", " ").strip()
    detail = f"{type(exc).__name__}: {message}"
    if len(detail) > _DETAIL_LIMIT:
        detail = detail[:_DETAIL_LIMIT]
    return detail


async def _timed_probe(name: str, probe: Callable[[], Any]) -> CheckResult:
    started = time.perf_counter()
    try:
        await probe()
    except Exception as exc:
        latency_ms = (time.perf_counter() - started) * 1000.0
        return CheckResult(
            name=name,
            status=ReadinessStatus.UNREADY,
            latency_ms=latency_ms,
            detail=_sanitize_detail(exc),
        )
    latency_ms = (time.perf_counter() - started) * 1000.0
    return CheckResult(name=name, status=ReadinessStatus.READY, latency_ms=latency_ms)


class PostgresReadiness:
    """Readiness probe executing ``SELECT 1`` against PostgreSQL."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @property
    def name(self) -> str:
        return "postgres"

    async def check(self) -> CheckResult:
        async def probe() -> None:
            async with self._session_factory() as session:
                await session.execute(text("SELECT 1"))

        return await _timed_probe(self.name, probe)


class _Pingable(Protocol):
    """Minimal structural interface satisfied by redis.asyncio clients."""

    async def ping(self) -> object: ...


class RedisReadiness:
    """Readiness probe issuing ``PING`` against Redis."""

    def __init__(self, redis_client: _Pingable) -> None:
        self._client = redis_client

    @property
    def name(self) -> str:
        return "redis"

    async def check(self) -> CheckResult:
        return await _timed_probe(self.name, self._client.ping)


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """Aggregated readiness results across all probes."""

    results: tuple[CheckResult, ...]

    @property
    def ready(self) -> bool:
        return all(r.status is ReadinessStatus.READY for r in self.results)

    @property
    def status(self) -> ReadinessStatus:
        return ReadinessStatus.READY if self.ready else ReadinessStatus.UNREADY

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe representation suitable for structured logging."""
        return {
            "status": self.status.value,
            "checks": [
                {
                    "name": r.name,
                    "status": r.status.value,
                    "latency_ms": round(r.latency_ms, 3),
                    "detail": r.detail,
                }
                for r in self.results
            ],
        }


async def check_readiness(checks: Sequence[ReadinessCheck]) -> ReadinessReport:
    """Run all probes concurrently; each probe owns its error handling."""
    results = tuple(await asyncio.gather(*(c.check() for c in checks)))
    return ReadinessReport(results=results)
