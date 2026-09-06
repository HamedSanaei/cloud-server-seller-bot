"""SQLAlchemy adapter for the sellable-offers port (LEASEWEB-MVP)."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import SellableOffer as _SellableOfferModel
from cloud_platform.modules.offers.domain import (
    OfferNotFoundError,
    OfferSpecUpdate,
    SellableOffer,
)


def _attr(row: Any, name: str) -> Any:
    """Read a legacy-style Column attribute; typed as Any at the boundary."""
    return getattr(row, name)


def _to_domain(row: _SellableOfferModel) -> SellableOffer:
    return SellableOffer(
        id=_attr(row, "id"),
        provider_key=str(_attr(row, "provider_key")),
        product_id=str(_attr(row, "product_id")),
        location_id=str(_attr(row, "location_id")),
        name=str(_attr(row, "name")),
        vcpu=int(_attr(row, "vcpu") or 0),
        ram_gb=int(_attr(row, "ram_gb") or 0),
        disk_gb=int(_attr(row, "disk_gb") or 0),
        traffic=_attr(row, "traffic"),
        provider_cost_minor=int(_attr(row, "provider_cost_minor") or 0),
        provider_cost_currency=str(_attr(row, "provider_cost_currency") or "EUR"),
        selling_price_minor=int(_attr(row, "selling_price_minor") or 0),
        selling_currency=str(_attr(row, "selling_currency") or "EUR"),
        billing_parameters=dict(_attr(row, "billing_parameters") or {}),
        provider_available=bool(_attr(row, "provider_available")),
        enabled=bool(_attr(row, "enabled")),
        created_at=_attr(row, "created_at"),
        updated_at=_attr(row, "updated_at"),
    )


class SqlAlchemySellableOfferRepository:
    """Durable storage for sellable offers."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def get(self, offer_id: UUID) -> SellableOffer | None:
        async with self._session_factory() as session:
            row = await session.get(_SellableOfferModel, offer_id)
            return _to_domain(row) if row is not None else None

    async def get_by_ref(
        self, provider_key: str, product_id: str, location_id: str
    ) -> SellableOffer | None:
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(_SellableOfferModel).where(
                            _SellableOfferModel.provider_key == provider_key,
                            _SellableOfferModel.product_id == product_id,
                            _SellableOfferModel.location_id == location_id,
                        )
                    )
                )
                .scalars()
                .first()
            )
            return _to_domain(row) if row is not None else None

    async def list_all(self) -> list[SellableOffer]:
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(_SellableOfferModel).order_by(
                            _SellableOfferModel.provider_key,
                            _SellableOfferModel.location_id,
                            _SellableOfferModel.product_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            return [_to_domain(row) for row in rows]

    async def list_sellable(self, provider_key: str | None = None) -> list[SellableOffer]:
        stmt = select(_SellableOfferModel).where(
            _SellableOfferModel.provider_available.is_(True),
            _SellableOfferModel.enabled.is_(True),
            _SellableOfferModel.selling_price_minor > 0,
        )
        if provider_key:
            stmt = stmt.where(_SellableOfferModel.provider_key == provider_key)
        async with self._session_factory() as session:
            stmt = stmt.order_by(_SellableOfferModel.location_id, _SellableOfferModel.name)
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_domain(row) for row in rows]

    async def list_provider_locations(self) -> list[tuple[str, str]]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        _SellableOfferModel.provider_key,
                        _SellableOfferModel.location_id,
                    )
                    .where(
                        _SellableOfferModel.provider_available.is_(True),
                        _SellableOfferModel.enabled.is_(True),
                        _SellableOfferModel.selling_price_minor > 0,
                    )
                    .distinct()
                    .order_by(_SellableOfferModel.provider_key, _SellableOfferModel.location_id)
                )
            ).all()
            return [(str(pk), str(loc)) for pk, loc in rows]

    async def upsert_from_provider(
        self,
        *,
        provider_key: str,
        product_id: str,
        location_id: str,
        update: OfferSpecUpdate,
    ) -> SellableOffer:
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(_SellableOfferModel).where(
                            _SellableOfferModel.provider_key == provider_key,
                            _SellableOfferModel.product_id == product_id,
                            _SellableOfferModel.location_id == location_id,
                        )
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                row = _SellableOfferModel(
                    provider_key=provider_key,
                    product_id=product_id,
                    location_id=location_id,
                    name=update.name,
                    vcpu=update.vcpu,
                    ram_gb=update.ram_gb,
                    disk_gb=update.disk_gb,
                    traffic=update.traffic,
                    provider_cost_minor=update.provider_cost_minor,
                    provider_cost_currency=update.provider_cost_currency,
                    billing_parameters=update.billing_parameters,
                    provider_available=update.provider_available,
                    selling_currency=update.provider_cost_currency,
                )
                session.add(row)
            else:
                cast_any: Any = row
                cast_any.name = update.name
                cast_any.vcpu = update.vcpu
                cast_any.ram_gb = update.ram_gb
                cast_any.disk_gb = update.disk_gb
                cast_any.traffic = update.traffic
                cast_any.provider_cost_minor = update.provider_cost_minor
                cast_any.provider_cost_currency = update.provider_cost_currency
                cast_any.billing_parameters = update.billing_parameters
                cast_any.provider_available = update.provider_available
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def mark_unavailable(self, provider_key: str, available: set[tuple[str, str]]) -> int:
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(_SellableOfferModel).where(
                            _SellableOfferModel.provider_key == provider_key
                        )
                    )
                )
                .scalars()
                .all()
            )
            changed = 0
            for row in rows:
                if (str(row.product_id), str(row.location_id)) not in available and bool(
                    row.provider_available
                ):
                    cast_any: Any = row
                    cast_any.provider_available = False
                    changed += 1
            if changed:
                await session.commit()
            return changed

    async def set_enabled(self, offer_id: UUID, enabled: bool) -> SellableOffer:
        async with self._session_factory() as session:
            row = await session.get(_SellableOfferModel, offer_id)
            if row is None:
                raise OfferNotFoundError(f"offer {offer_id} not found")
            cast_any: Any = row
            cast_any.enabled = enabled
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def set_selling_price(
        self, offer_id: UUID, selling_price_minor: int, currency: str
    ) -> SellableOffer:
        if selling_price_minor <= 0:
            raise ValueError("selling price must be positive minor units")
        if not currency or len(currency) != 3 or not currency.isupper():
            raise ValueError("currency must be a 3-letter uppercase ISO code")
        async with self._session_factory() as session:
            row = await session.get(_SellableOfferModel, offer_id)
            if row is None:
                raise OfferNotFoundError(f"offer {offer_id} not found")
            cast_any: Any = row
            cast_any.selling_price_minor = selling_price_minor
            cast_any.selling_currency = currency
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)
