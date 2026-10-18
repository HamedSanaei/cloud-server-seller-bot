"""Bounded provider-observation buffering; commit before any reconciliation.

A rollback precedes per-row compatibility recovery. Failure results are explicit
so an incomplete observation can never authorize pricing or retirement.
"""

from __future__ import annotations

from typing import Any

from cloud_platform.observability.catalog import stage


class ObservationBuffer:
    def __init__(self, repository: Any, *, enabled: bool = True) -> None:
        self.repository = repository
        self.enabled = enabled and callable(
            getattr(type(repository), "upsert_provider_batch", None)
        )
        self.pending: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.repository, name)

    async def upsert_from_provider(self, **observation: Any) -> Any:
        if not self.enabled:
            return await self.repository.upsert_from_provider(**observation)
        self.pending.append(observation)
        return None

    async def flush(self) -> tuple[int, list[str]]:
        with stage("observation_persistence"):
            return await self._flush()

    async def _flush(self) -> tuple[int, list[str]]:
        failures: list[str] = []
        succeeded = 0
        for offset in range(0, len(self.pending), 100):
            chunk = self.pending[offset : offset + 100]
            try:
                await self.repository.upsert_provider_batch(chunk)
                succeeded += len(chunk)
            except Exception:
                # Repository context exit rolled the ENTIRE chunk back.
                for observation in chunk:
                    try:
                        await self.repository.upsert_from_provider(**observation)
                        succeeded += 1
                    except Exception as exc:
                        failures.append(
                            f"upsert {observation['product_id']}/{observation['location_id']}: "
                            f"{type(exc).__name__}"
                        )
        self.pending.clear()
        return succeeded, failures
