"""SQLAlchemy adapter for versioned price books (M06-001)."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import PriceBookVersion as _PriceBookVersionModel
from cloud_platform.db.base import ServerPriceSnapshot as _ServerPriceSnapshotModel
from cloud_platform.modules.pricing.domain import (
    DuplicateBookVersionError,
    OfferCost,
    PriceBookVersion,
    ServerPriceSnapshot,
    SnapshotAlreadyExistsError,
    rule_from_dict,
    rule_to_dict,
)


def _attr(row: Any, name: str) -> Any:
    """Read a legacy-style Column attribute; typed as Any at the boundary."""
    return getattr(row, name)


def _aware(value: Any) -> datetime:
    """Normalize a stored timestamp to tz-aware UTC."""
    if not isinstance(value, datetime):
        raise ValueError(f"expected a datetime, got {type(value).__name__}")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _to_domain(row: _PriceBookVersionModel) -> PriceBookVersion:
    raw_rules = _attr(row, "rules") or []
    return PriceBookVersion(
        book_name=str(_attr(row, "book_name")),
        version=int(_attr(row, "version")),
        effective_at=_aware(_attr(row, "effective_at")),
        rules=tuple(rule_from_dict(rule) for rule in raw_rules),
        id=_attr(row, "id"),
        created_at=_aware(_attr(row, "created_at"))
        if _attr(row, "created_at") is not None
        else None,
    )


class SqlAlchemyPriceBookRepository:
    """Durable storage for price book versions."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def create_version(self, version: PriceBookVersion) -> PriceBookVersion:
        """Persist a validated version.

        Raises:
            DuplicateBookVersionError: If (book_name, version) already exists.
        """
        row = _PriceBookVersionModel(
            book_name=version.book_name,
            version=version.version,
            effective_at=version.effective_at,
            rules=[rule_to_dict(rule) for rule in version.rules],
        )
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise DuplicateBookVersionError(
                    f"price book {version.book_name!r} already has version {version.version}"
                ) from exc
            await session.refresh(row)
            return _to_domain(row)

    async def list_versions(self, book_name: str) -> list[PriceBookVersion]:
        """All versions of a book, newest first."""
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(_PriceBookVersionModel)
                        .where(_PriceBookVersionModel.book_name == book_name)
                        .order_by(_PriceBookVersionModel.version.desc())
                    )
                )
                .scalars()
                .all()
            )
            return [_to_domain(row) for row in rows]

    async def get(self, book_name: str, version: int) -> PriceBookVersion | None:
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(_PriceBookVersionModel).where(
                            _PriceBookVersionModel.book_name == book_name,
                            _PriceBookVersionModel.version == version,
                        )
                    )
                )
                .scalars()
                .first()
            )
            return None if row is None else _to_domain(row)


def _snapshot_to_domain(row: _ServerPriceSnapshotModel) -> ServerPriceSnapshot:
    offer = OfferCost(
        provider_key=str(_attr(row, "provider_key")),
        plan_id=str(_attr(row, "plan_id")),
        location_id=str(_attr(row, "location_id")),
        cost_minor=int(_attr(row, "cost_minor")),
        currency=str(_attr(row, "currency")),
    )
    return ServerPriceSnapshot(
        server_id=_attr(row, "server_id"),
        offer=offer,
        selling_minor=int(_attr(row, "selling_minor")),
        book_name=str(_attr(row, "book_name")),
        book_version=int(_attr(row, "book_version")),
        rule=rule_from_dict(_attr(row, "margin_rule")),
        priced_at=_aware(_attr(row, "priced_at")),
        id=_attr(row, "id"),
        created_at=_aware(_attr(row, "created_at"))
        if _attr(row, "created_at") is not None
        else None,
    )


class SqlAlchemyServerPriceSnapshotRepository:
    """Durable, immutable per-server price snapshots."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def create(self, snapshot: ServerPriceSnapshot) -> ServerPriceSnapshot:
        """Persist a snapshot.

        Raises:
            SnapshotAlreadyExistsError: If the server already has a snapshot.
        """
        row = _ServerPriceSnapshotModel(
            server_id=snapshot.server_id,
            provider_key=snapshot.offer.provider_key,
            plan_id=snapshot.offer.plan_id,
            location_id=snapshot.offer.location_id,
            currency=snapshot.offer.currency,
            cost_minor=snapshot.offer.cost_minor,
            selling_minor=snapshot.selling_minor,
            book_name=snapshot.book_name,
            book_version=snapshot.book_version,
            margin_rule=rule_to_dict(snapshot.rule),
            priced_at=snapshot.priced_at,
        )
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise SnapshotAlreadyExistsError(
                    f"server {snapshot.server_id} already has a price snapshot"
                ) from exc
            await session.refresh(row)
            return _snapshot_to_domain(row)

    async def get(self, server_id: UUID) -> ServerPriceSnapshot | None:
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(_ServerPriceSnapshotModel).where(
                            _ServerPriceSnapshotModel.server_id == server_id
                        )
                    )
                )
                .scalars()
                .first()
            )
            return None if row is None else _snapshot_to_domain(row)
