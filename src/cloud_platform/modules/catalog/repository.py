"""SQLAlchemy adapter for location-aware catalog rows (M04-005).

The catalog table's ``uq_catalog_provider_plan_location`` unique constraint
makes upserts structurally idempotent. Providers are resolved by name and,
when missing, created with a **deterministic** UUID (uuid5 of the provider
key) so ingestion is repeatable without a separate provider-provisioning
step.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import Catalog as _CatalogModel
from cloud_platform.db.base import Provider as _ProviderModel
from cloud_platform.modules.catalog.domain import CatalogEntrySpec, OfferRef, OfferState

#: Namespace for deriving stable provider UUIDs from provider keys.
_PROVIDER_UUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "cloud-platform:provider")

#: Stable big-int key for the catalog sync advisory lock (Postgres 64-bit).
_CATALOG_SYNC_LOCK_KEY = int.from_bytes(
    uuid.uuid5(uuid.NAMESPACE_URL, "cloud-platform:catalog-sync-lock").bytes[:8],
    "big",
)


def provider_key_to_uuid(provider_key: str) -> uuid.UUID:
    """Deterministic UUID for a provider key (stable across runs and DBs)."""
    return uuid.uuid5(_PROVIDER_UUID_NAMESPACE, provider_key)


def _attr(row: Any, name: str) -> Any:
    """Read a legacy-style Column attribute; typed as Any at the boundary."""
    return getattr(row, name)


class SqlAlchemyCatalogRepository:
    """Persists location-aware catalog rows for one provider."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def _provider_id(
        self, session: AsyncSession, provider_key: str, *, create: bool = True
    ) -> uuid.UUID | None:
        provider = (
            (
                await session.execute(
                    select(_ProviderModel).where(_ProviderModel.name == provider_key)
                )
            )
            .scalars()
            .first()
        )
        if provider is not None:
            provider_id = _attr(provider, "id")
            if not isinstance(provider_id, uuid.UUID):
                raise LookupError(f"provider {provider_key!r} has a non-UUID id")
            return provider_id
        if not create:
            return None
        provider_id = provider_key_to_uuid(provider_key)
        session.add(_ProviderModel(id=provider_id, name=provider_key))
        await session.flush()
        return provider_id

    async def _offer_row(self, session: AsyncSession, ref: OfferRef) -> Any:
        """Fetch the catalog row for an offer ref, or None if unknown."""
        provider_id = await self._provider_id(session, ref.provider_key, create=False)
        if provider_id is None:
            return None
        return (
            (
                await session.execute(
                    select(_CatalogModel).where(
                        _CatalogModel.provider_id == provider_id,
                        _CatalogModel.provider_plan_id == ref.plan_id,
                        _CatalogModel.provider_location_id == ref.location_id,
                    )
                )
            )
            .scalars()
            .first()
        )

    async def get_offer(self, ref: OfferRef) -> OfferState | None:
        async with self._session_factory() as session:
            row = await self._offer_row(session, ref)
            if row is None:
                return None
            return OfferState(
                id=_attr(row, "id"),
                ref=ref,
                name=str(_attr(row, "name")),
                enabled=bool(_attr(row, "enabled")),
                price_per_quantum=int(_attr(row, "price_per_quantum")),
                currency=str(_attr(row, "currency")),
            )

    async def set_offer_enabled(self, ref: OfferRef, enabled: bool) -> None:
        async with self._session_factory() as session:
            row = await self._offer_row(session, ref)
            if row is None:
                raise LookupError(f"offer {ref.key} not found")
            cast_any: Any = row
            cast_any.enabled = enabled
            await session.commit()

    async def upsert_entry(self, spec: CatalogEntrySpec) -> bool:
        async with self._session_factory() as session:
            provider_id = await self._provider_id(session, spec.provider_key)
            if provider_id is None:  # pragma: no cover - create=True always resolves
                raise LookupError(f"provider {spec.provider_key!r} could not be resolved")

            row = (
                (
                    await session.execute(
                        select(_CatalogModel).where(
                            _CatalogModel.provider_id == provider_id,
                            _CatalogModel.provider_plan_id == spec.plan_id,
                            _CatalogModel.provider_location_id == spec.location_id,
                        )
                    )
                )
                .scalars()
                .first()
            )

            if row is None:
                row = _CatalogModel(
                    provider_id=provider_id,
                    provider_plan_id=spec.plan_id,
                    provider_location_id=spec.location_id,
                    name=spec.name,
                    description=spec.description,
                    architecture=spec.architecture,
                    vcpu=spec.vcpu,
                    memory_mb=spec.memory_mb,
                    disk_gb=spec.disk_gb,
                    currency=spec.currency,
                    price_per_quantum=spec.price_per_quantum,
                    quantum_seconds=spec.quantum_seconds,
                    extra_metadata=spec.extra_metadata or {},
                )
                session.add(row)
                created = True
            else:
                cast_any: Any = row
                cast_any.name = spec.name
                cast_any.description = spec.description
                cast_any.architecture = spec.architecture
                cast_any.vcpu = spec.vcpu
                cast_any.memory_mb = spec.memory_mb
                cast_any.disk_gb = spec.disk_gb
                cast_any.currency = spec.currency
                cast_any.price_per_quantum = spec.price_per_quantum
                cast_any.quantum_seconds = spec.quantum_seconds
                cast_any.extra_metadata = spec.extra_metadata or {}
                created = False

            await session.commit()
            return created


class PostgresAdvisoryCatalogSyncLock:
    """Catalog sync lock backed by a Postgres session-level advisory lock.

    ``pg_try_advisory_lock`` is non-blocking: it returns True when this
    backend acquires the lock and False when another backend holds it, which
    serializes concurrent sync jobs across the whole process pool. The
    advisory lock is bound to the backend (connection) that acquires it, so
    the SAME session acquires and releases it and stays open for the whole
    guarded interval; closing the session is a backstop that releases any
    lock a crashed run left behind.
    """

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        key: int = _CATALOG_SYNC_LOCK_KEY,
    ) -> None:
        self._session_factory = session_factory
        self._key = key

    @asynccontextmanager
    async def guard(self) -> AsyncIterator[bool]:
        """Yield True if the lock was acquired; always releases on exit."""
        async with self._session_factory() as session:
            result = await session.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": self._key}
            )
            acquired = bool(result.scalar_one())
            try:
                yield acquired
            finally:
                if acquired:
                    await session.execute(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": self._key}
                    )
