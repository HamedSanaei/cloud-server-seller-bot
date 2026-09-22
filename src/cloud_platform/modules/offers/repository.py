"""SQLAlchemy adapter for the sellable-offers port (LEASEWEB-MVP)."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import CatalogSyncState as _CatalogSyncStateModel
from cloud_platform.db.base import SellableOffer as _SellableOfferModel
from cloud_platform.modules.offers.domain import (
    CatalogSyncState,
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
        technical_metadata=dict(_attr(row, "technical_metadata") or {}),
        billing_model=str(_attr(row, "billing_model") or "prepaid_monthly_fixed"),
        provider_available=bool(_attr(row, "provider_available")),
        enabled=bool(_attr(row, "enabled")),
        operator_disabled=bool(_attr(row, "operator_disabled")),
        auto_priced=bool(_attr(row, "auto_priced")),
        created_at=_attr(row, "created_at"),
        updated_at=_attr(row, "updated_at"),
        provider_account_id=_attr(row, "provider_account_id"),
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
        provider_account_id: str | None = None,
    ) -> SellableOffer:
        """Refresh one provider observation (idempotent per product+location).

        ``provider_account_id`` records WHICH credential account supplied this
        observation; omitting it leaves the existing provenance untouched so a
        caller that does not know about credential accounts cannot erase it.

        A provider observation WITHOUT a proven currency is refused here as
        well as at the sync boundary: the columns carry a database default of
        EUR, so storing an observation that omitted its currency would silently
        reprice inventory that bills in GBP (or anything else). Sales
        Organizations bill in different currencies — this fails CLOSED.
        """
        currency = str(update.provider_cost_currency or "").strip()
        if not currency:
            raise ValueError(
                f"refusing to store {provider_key}/{product_id}/{location_id} without a "
                "provider currency: a database default would silently reprice it"
            )
        account_id = provider_account_id or update.provider_account_id
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
                    provider_cost_currency=currency,
                    billing_parameters=update.billing_parameters,
                    technical_metadata=dict(update.technical_metadata or {}),
                    billing_model=update.billing_model or "prepaid_monthly_fixed",
                    provider_available=update.provider_available,
                    selling_currency=currency,
                    provider_account_id=account_id,
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
                # The operator's selling price and currency are NEVER written
                # here: a catalog refresh must not reprice the storefront.
                cast_any.provider_cost_currency = currency
                cast_any.billing_parameters = update.billing_parameters
                if update.technical_metadata is not None:
                    cast_any.technical_metadata = dict(update.technical_metadata)
                if update.billing_model:
                    cast_any.billing_model = update.billing_model
                cast_any.provider_available = update.provider_available
                if account_id:
                    cast_any.provider_account_id = account_id
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def mark_unavailable(
        self,
        provider_key: str,
        available: set[tuple[str, str]],
        billing_model: str | None = None,
    ) -> int:
        async with self._session_factory() as session:
            stmt = select(_SellableOfferModel).where(
                _SellableOfferModel.provider_key == provider_key
            )
            if billing_model is not None:
                stmt = stmt.where(_SellableOfferModel.billing_model == billing_model)
            rows = (await session.execute(stmt)).scalars().all()
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

    async def set_operator_disabled(self, offer_id: UUID, disabled: bool) -> SellableOffer:
        async with self._session_factory() as session:
            row = await session.get(_SellableOfferModel, offer_id)
            if row is None:
                raise OfferNotFoundError(f"offer {offer_id} not found")
            cast_any: Any = row
            cast_any.operator_disabled = disabled
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def set_auto_priced(self, offer_id: UUID, auto_priced: bool) -> SellableOffer:
        async with self._session_factory() as session:
            row = await session.get(_SellableOfferModel, offer_id)
            if row is None:
                raise OfferNotFoundError(f"offer {offer_id} not found")
            cast_any: Any = row
            cast_any.auto_priced = auto_priced
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


class SqlAlchemyCatalogSyncStateRepository:
    """Durable per-provider automatic-sync status (one row per provider)."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _to_domain(row: Any) -> CatalogSyncState:
        from datetime import datetime

        def _when(value: Any) -> datetime | None:
            return value if isinstance(value, datetime) else None

        return CatalogSyncState(
            provider_key=str(_attr(row, "provider_key")),
            last_attempted_at=_when(_attr(row, "last_attempted_at")),
            last_success_at=_when(_attr(row, "last_success_at")),
            discovered=int(_attr(row, "discovered") or 0),
            persisted=int(_attr(row, "persisted") or 0),
            prices_updated=int(_attr(row, "prices_updated") or 0),
            published=int(_attr(row, "published") or 0),
            retired=int(_attr(row, "retired") or 0),
            warnings=tuple(str(w) for w in (_attr(row, "warnings") or [])),
            errors=tuple(str(e) for e in (_attr(row, "errors") or [])),
        )

    async def record_run(
        self,
        *,
        provider_key: str,
        ok: bool,
        discovered: int,
        persisted: int,
        prices_updated: int,
        published: int,
        retired: int,
        warnings: tuple[str, ...],
        errors: tuple[str, ...],
    ) -> CatalogSyncState:
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        async with self._session_factory() as session:
            row = await session.get(_CatalogSyncStateModel, provider_key)
            if row is None:
                row = _CatalogSyncStateModel(provider_key=provider_key)
                session.add(row)
            cast_any: Any = row
            cast_any.last_attempted_at = now
            if ok:
                cast_any.last_success_at = now
            cast_any.discovered = discovered
            cast_any.persisted = persisted
            cast_any.prices_updated = prices_updated
            cast_any.published = published
            cast_any.retired = retired
            cast_any.warnings = list(warnings)
            cast_any.errors = list(errors)
            await session.commit()
            await session.refresh(row)
            return self._to_domain(row)

    async def get(self, provider_key: str) -> CatalogSyncState | None:
        async with self._session_factory() as session:
            row = await session.get(_CatalogSyncStateModel, provider_key)
            return self._to_domain(row) if row is not None else None

    async def list_all(self) -> list[CatalogSyncState]:
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(_CatalogSyncStateModel).order_by(_CatalogSyncStateModel.provider_key)
                    )
                )
                .scalars()
                .all()
            )
            return [self._to_domain(row) for row in rows]
