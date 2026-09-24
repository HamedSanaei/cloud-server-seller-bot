"""SQLAlchemy adapter for the sellable-offers port (LEASEWEB-MVP)."""

from __future__ import annotations

from collections.abc import Callable, Collection
from contextlib import AbstractAsyncContextManager
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import CatalogSyncState as _CatalogSyncStateModel
from cloud_platform.db.base import SellableOffer as _SellableOfferModel
from cloud_platform.modules.fx.domain import SUPPORTED_CURRENCIES
from cloud_platform.modules.offers.domain import (
    DOMESTIC_PROVIDER_COST_CURRENCIES,
    CatalogSyncState,
    OfferNotFoundError,
    OfferSpecUpdate,
    SellableOffer,
    has_valid_pricing_provenance,
)


def _attr(row: Any, name: str) -> Any:
    """Read a legacy-style Column attribute; typed as Any at the boundary."""
    return getattr(row, name)


def _rate_equal(left: object, right: object) -> bool:
    """Compare exact rate text numerically without binary-float coercion."""
    if left is None or right is None:
        return False
    try:
        a = Decimal(str(left).strip())
        b = Decimal(str(right).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return False
    return a.is_finite() and b.is_finite() and a == b


def _pricing_rate(row: Any, metadata: dict[str, object] | None = None) -> object:
    """Return the exact provider rate belonging to an offer observation."""
    params = dict(_attr(row, "billing_parameters") or {})
    model = str(_attr(row, "billing_model") or "")
    key = "provider_hourly_rate" if model == "hourly" else "provider_monthly_rate"
    if metadata is not None:
        candidate = metadata.get(key)
        if candidate is not None:
            return candidate
    return params.get(key)


def _metadata_rate(row: Any, metadata: dict[str, object] | None) -> object:
    model = str(_attr(row, "billing_model") or "")
    key = "provider_hourly_rate" if model == "hourly" else "provider_monthly_rate"
    return (metadata or {}).get(key)


def _positive_int64(value: object, *, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > 9_223_372_036_854_775_807
    ):
        raise ValueError(f"{name} must be a positive signed int64 integer")
    return value


def _nonnegative_int64(value: object, *, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > 9_223_372_036_854_775_807
    ):
        raise ValueError(f"{name} must be a non-negative signed int64 integer")
    return value


def _audited_currency(value: object) -> str:
    code = str(value or "").strip().upper()
    if code not in SUPPORTED_CURRENCIES:
        raise ValueError("currency must be an audited uppercase code")
    return code


def _to_domain(row: _SellableOfferModel) -> SellableOffer:
    values: dict[str, object] = {
        "id": _attr(row, "id"),
        "provider_key": str(_attr(row, "provider_key")),
        "product_id": str(_attr(row, "product_id")),
        "location_id": str(_attr(row, "location_id")),
        "name": str(_attr(row, "name")),
        "vcpu": int(_attr(row, "vcpu") or 0),
        "ram_gb": int(_attr(row, "ram_gb") or 0),
        "disk_gb": int(_attr(row, "disk_gb") or 0),
        "traffic": _attr(row, "traffic"),
        "provider_cost_minor": int(_attr(row, "provider_cost_minor") or 0),
        "provider_cost_currency": str(_attr(row, "provider_cost_currency") or "").strip().upper(),
        "selling_price_minor": int(_attr(row, "selling_price_minor") or 0),
        "selling_currency": str(_attr(row, "selling_currency") or "").strip().upper(),
        "billing_parameters": dict(_attr(row, "billing_parameters") or {}),
        "technical_metadata": dict(_attr(row, "technical_metadata") or {}),
        "pricing_metadata": dict(_attr(row, "pricing_metadata") or {}),
        "billing_model": str(_attr(row, "billing_model") or "prepaid_monthly_fixed"),
        "provider_available": bool(_attr(row, "provider_available")),
        "enabled": bool(_attr(row, "enabled")),
        "operator_disabled": bool(_attr(row, "operator_disabled")),
        "auto_priced": bool(_attr(row, "auto_priced")),
        "created_at": _attr(row, "created_at"),
        "updated_at": _attr(row, "updated_at"),
        "provider_account_id": _attr(row, "provider_account_id"),
    }
    try:
        return SellableOffer(**values)  # type: ignore[arg-type]
    except ValueError:
        # Do not relabel a corrupt legacy row as USD.  Preserve its raw fields
        # for diagnostics and make the domain object permanently unsellable.
        values["legacy_invalid"] = True
        values["provider_available"] = False
        values["enabled"] = False
        return SellableOffer(**values)  # type: ignore[arg-type]


class SqlAlchemySellableOfferRepository:
    """Durable storage for sellable offers."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        *,
        catalog_currency: str = "USD",
        catalog_stale_limit_seconds: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        target = str(catalog_currency or "").strip().upper()
        if target not in SUPPORTED_CURRENCIES:
            raise ValueError("catalog_currency must be an audited currency code")
        if catalog_stale_limit_seconds is not None and catalog_stale_limit_seconds <= 0:
            raise ValueError("catalog_stale_limit_seconds must be positive")
        self._catalog_currency = target
        self._catalog_stale_limit_seconds = catalog_stale_limit_seconds

    async def get(self, offer_id: UUID) -> SellableOffer | None:
        async with self._session_factory() as session:
            row = await session.get(_SellableOfferModel, offer_id)
            return _to_domain(row) if row is not None else None

    async def get_by_ref(
        self,
        provider_key: str,
        product_id: str,
        location_id: str,
        provider_account_id: str | None = None,
    ) -> SellableOffer | None:
        async with self._session_factory() as session:
            statement = select(_SellableOfferModel).where(
                _SellableOfferModel.provider_key == provider_key,
                _SellableOfferModel.product_id == product_id,
                _SellableOfferModel.location_id == location_id,
            )
            if provider_account_id is not None:
                statement = statement.where(
                    _SellableOfferModel.provider_account_id == provider_account_id
                )
            else:
                statement = statement.where(_SellableOfferModel.provider_account_id.is_(None))
            rows = (await session.execute(statement.with_for_update())).scalars().all()
            if len(rows) > 1:
                raise ValueError(
                    "provider/product/location is ambiguous; credential account is required"
                )
            row = rows[0] if rows else None
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
            _SellableOfferModel.operator_disabled.is_(False),
            _SellableOfferModel.selling_price_minor > 0,
        )
        if provider_key:
            stmt = stmt.where(_SellableOfferModel.provider_key == provider_key)
        async with self._session_factory() as session:
            stmt = stmt.order_by(_SellableOfferModel.location_id, _SellableOfferModel.name)
            rows = (await session.execute(stmt)).scalars().all()
            return [
                offer
                for row in rows
                if (offer := _to_domain(row)).sellable
                and has_valid_pricing_provenance(
                    offer,
                    self._catalog_currency,
                    catalog_stale_limit_seconds=self._catalog_stale_limit_seconds,
                )
            ]

    async def list_provider_locations(self) -> list[tuple[str, str]]:
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(_SellableOfferModel)
                        .where(
                            _SellableOfferModel.provider_available.is_(True),
                            _SellableOfferModel.enabled.is_(True),
                            _SellableOfferModel.operator_disabled.is_(False),
                            _SellableOfferModel.selling_price_minor > 0,
                        )
                        .order_by(_SellableOfferModel.provider_key, _SellableOfferModel.location_id)
                    )
                )
                .scalars()
                .all()
            )
            pairs = {
                (offer.provider_key, offer.location_id)
                for row in rows
                if (offer := _to_domain(row)).sellable
                and has_valid_pricing_provenance(
                    offer,
                    self._catalog_currency,
                    catalog_stale_limit_seconds=self._catalog_stale_limit_seconds,
                )
            }
            return sorted(pairs)

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
        currency = str(update.provider_cost_currency or "").strip().upper()
        if not currency:
            raise ValueError(
                f"refusing to store {provider_key}/{product_id}/{location_id} without a "
                "provider currency: a database default would silently reprice it"
            )
        supplied_accounts = [
            value
            for value in (provider_account_id, update.provider_account_id)
            if value is not None
        ]
        if any(not isinstance(value, str) or not value.strip() for value in supplied_accounts):
            raise ValueError("provider_account_id must be non-empty when supplied")
        if len({str(value).strip().casefold() for value in supplied_accounts}) > 1:
            raise ValueError("provider account arguments disagree")
        account_id = str(supplied_accounts[0]).strip().casefold() if supplied_accounts else None
        async with self._session_factory() as session:
            candidates = (
                (
                    await session.execute(
                        select(_SellableOfferModel)
                        .where(
                            _SellableOfferModel.provider_key == provider_key,
                            _SellableOfferModel.product_id == product_id,
                            _SellableOfferModel.location_id == location_id,
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            if account_id is None:
                if len(candidates) > 1:
                    raise ValueError(
                        "provider observation is ambiguous; credential account is required"
                    )
                if candidates and candidates[0].provider_account_id:
                    raise ValueError("provider observation already belongs to a credential account")
                row = candidates[0] if candidates else None
            else:
                matching = [
                    candidate
                    for candidate in candidates
                    if str(candidate.provider_account_id or "").strip().casefold()
                    == str(account_id).strip().casefold()
                ]
                if any(candidate.provider_account_id is None for candidate in candidates):
                    raise ValueError(
                        "legacy unscoped offer conflicts with an account-scoped observation"
                    )
                if len(matching) > 1:
                    raise ValueError("duplicate account-scoped provider observations exist")
                row = matching[0] if matching else None
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
                    pricing_metadata={},
                    billing_model=update.billing_model or "prepaid_monthly_fixed",
                    provider_available=(
                        False
                        if update.provider_observation_inconclusive
                        else update.provider_available
                    ),
                    selling_currency=currency,
                    provider_account_id=account_id,
                )
                session.add(row)
            else:
                cast_any: Any = row
                # Capture every prior pricing fact before writing the new
                # observation.  Comparing after assignment loses same-minor
                # exact-rate changes and can leave a stale customer price live.
                prior_cost = int(cast_any.provider_cost_minor or 0)
                prior_currency = str(cast_any.provider_cost_currency or "").strip().upper()
                prior_rate = _pricing_rate(cast_any)
                next_rate = _pricing_rate(
                    cast_any,
                    dict(update.billing_parameters or {}),
                )
                cost_changed = (
                    prior_cost != update.provider_cost_minor or prior_currency != currency
                )
                rate_changed = not _rate_equal(prior_rate, next_rate)
                if bool(cast_any.auto_priced) and (cost_changed or rate_changed):
                    pending = dict(cast_any.pricing_metadata or {})
                    pending["fx_repricing_pending"] = True
                    pending["catalog_observation_pending"] = True
                    cast_any.pricing_metadata = pending
                # The operator's selling price and currency are NEVER written
                # here: a catalog refresh must not reprice the storefront.
                cast_any.name = update.name
                cast_any.vcpu = update.vcpu
                cast_any.ram_gb = update.ram_gb
                cast_any.disk_gb = update.disk_gb
                cast_any.traffic = update.traffic
                cast_any.provider_cost_minor = update.provider_cost_minor
                cast_any.provider_cost_currency = currency
                cast_any.billing_parameters = update.billing_parameters
                if update.technical_metadata is not None:
                    cast_any.technical_metadata = dict(update.technical_metadata)
                if update.billing_model:
                    cast_any.billing_model = update.billing_model
                if not update.provider_observation_inconclusive:
                    cast_any.provider_available = update.provider_available
                if account_id:
                    cast_any.provider_account_id = account_id
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def retire_nonactive_accounts(
        self,
        provider_key: str,
        *,
        billing_model: str | None,
        active_account_ids: Collection[str],
        retire_unscoped: bool = True,
    ) -> int:
        """Retire rows that cannot receive new business from a credential.

        Catalog refreshes preserve last-known state for transient active-account
        failures, but a DRAINING/disabled/removed credential is a durable
        routing fact: its rows must not remain sellable even when the broader
        catalog view is incomplete. This narrow pass changes only those rows;
        active rows are left untouched.
        """
        active = {
            str(account_id).strip().casefold()
            for account_id in active_account_ids
            if str(account_id or "").strip()
        }
        if not isinstance(retire_unscoped, bool):
            raise ValueError("retire_unscoped must be boolean")
        async with self._session_factory() as session:
            stmt = select(_SellableOfferModel).where(
                _SellableOfferModel.provider_key == provider_key
            )
            if billing_model is not None:
                stmt = stmt.where(_SellableOfferModel.billing_model == billing_model)
            rows = (await session.execute(stmt.with_for_update())).scalars().all()
            changed = 0
            for row in rows:
                account = str(_attr(row, "provider_account_id") or "").strip().casefold()
                should_retire = (not account and retire_unscoped) or (
                    bool(account) and account not in active
                )
                if not should_retire:
                    continue
                if not bool(_attr(row, "provider_available")):
                    continue
                cast_any: Any = row
                cast_any.provider_available = False
                changed += 1
            if changed:
                await session.commit()
            else:
                await session.rollback()
            return changed

    async def mark_unavailable(
        self,
        provider_key: str,
        available: Collection[tuple[str, ...]],
        billing_model: str | None = None,
        provider_account_id: str | None = None,
    ) -> int:
        async with self._session_factory() as session:
            stmt = select(_SellableOfferModel).where(
                _SellableOfferModel.provider_key == provider_key
            )
            if billing_model is not None:
                stmt = stmt.where(_SellableOfferModel.billing_model == billing_model)
            if provider_account_id is not None:
                stmt = stmt.where(_SellableOfferModel.provider_account_id == provider_account_id)
            rows = (await session.execute(stmt)).scalars().all()
            changed = 0
            qualified = {tuple(item) for item in available if len(item) == 3}
            legacy = {tuple(item) for item in available if len(item) == 2}
            for row in rows:
                pair = (str(row.product_id), str(row.location_id))
                account_key = (str(row.provider_account_id or ""), *pair)
                if str(row.provider_account_id or "").strip():
                    # A scoped observation may never be preserved by another
                    # account's product/location pair.
                    is_available = account_key in qualified
                else:
                    # Legacy NULL-account rows retain the historical pair
                    # semantics, including compatibility with mixed sync
                    # reports; they are never used to qualify a scoped row.
                    is_available = account_key in qualified or pair in legacy
                if not is_available and bool(row.provider_available):
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
            operator_disabled = bool(row.operator_disabled)
        return await self.set_visibility_state(
            offer_id, enabled=enabled, operator_disabled=operator_disabled
        )

    async def set_operator_disabled(self, offer_id: UUID, disabled: bool) -> SellableOffer:
        if not isinstance(disabled, bool):
            raise ValueError("disabled must be boolean")
        async with self._session_factory() as session:
            row = await session.get(_SellableOfferModel, offer_id)
            if row is None:
                raise OfferNotFoundError(f"offer {offer_id} not found")
            if not disabled:
                # Re-enabling is a visibility transition, not a way to bypass
                # the provenance/availability gate.
                enabled = bool(row.enabled)
                if not enabled:
                    raise ValueError(
                        "a disabled offer must be explicitly enabled before unblocking"
                    )
                await session.rollback()
            else:
                cast_any: Any = row
                cast_any.operator_disabled = True
                await session.commit()
                await session.refresh(row)
                return _to_domain(row)
        return await self.set_visibility_state(
            offer_id,
            enabled=True,
            operator_disabled=False,
        )

    async def set_visibility_state(
        self, offer_id: UUID, *, enabled: bool, operator_disabled: bool
    ) -> SellableOffer:
        """Write both visibility fields under one row lock."""
        if not isinstance(enabled, bool) or not isinstance(operator_disabled, bool):
            raise ValueError("visibility flags must be boolean")
        if not enabled and not operator_disabled:
            raise ValueError("a disabled offer must carry the operator-disabled block")
        async with self._session_factory() as session:
            statement = (
                select(_SellableOfferModel)
                .where(_SellableOfferModel.id == offer_id)
                .with_for_update()
            )
            row = (await session.execute(statement)).scalar_one_or_none()
            if row is None:
                raise OfferNotFoundError(f"offer {offer_id} not found")
            pricing_metadata = dict(row.pricing_metadata or {})
            technical_metadata = dict(row.technical_metadata or {})
            if enabled and (
                not bool(row.provider_available)
                or int(row.selling_price_minor or 0) <= 0
                or bool(pricing_metadata.get("fx_repricing_pending"))
                or bool(technical_metadata.get("deprecated"))
                or not has_valid_pricing_provenance(
                    _to_domain(row),
                    self._catalog_currency,
                    catalog_stale_limit_seconds=self._catalog_stale_limit_seconds,
                )
            ):
                raise ValueError("offer is not publishable under its current pricing snapshot")
            cast_any: Any = row
            cast_any.enabled = enabled
            cast_any.operator_disabled = operator_disabled
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def set_auto_priced(self, offer_id: UUID, auto_priced: bool) -> SellableOffer:
        if not isinstance(auto_priced, bool):
            raise ValueError("auto_priced must be boolean")
        async with self._session_factory() as session:
            row = await session.get(_SellableOfferModel, offer_id)
            if row is None:
                raise OfferNotFoundError(f"offer {offer_id} not found")
            if auto_priced and not has_valid_pricing_provenance(
                _to_domain(row),
                self._catalog_currency,
                catalog_stale_limit_seconds=self._catalog_stale_limit_seconds,
            ):
                raise ValueError("automatic pricing requires valid pricing provenance")
            cast_any: Any = row
            cast_any.auto_priced = auto_priced
            if not auto_priced:
                metadata = dict(row.pricing_metadata or {})
                metadata["pricing_mode"] = "manual"
                cast_any.pricing_metadata = metadata
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def set_selling_price(
        self, offer_id: UUID, selling_price_minor: int, currency: str
    ) -> SellableOffer:
        """Return an already-persisted price; reject provenance-free writes.

        The former direct setter could attach an arbitrary canonical price to a
        foreign observation without proving its native rate/FX/markup. Price
        mutations now go through :meth:`set_manual_price` or the automatic
        audited pipeline. Keep this compatibility method idempotent only.
        """
        selling_price_minor = _positive_int64(selling_price_minor, name="selling price")
        code = _audited_currency(currency)
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(_SellableOfferModel)
                    .where(_SellableOfferModel.id == offer_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise OfferNotFoundError(f"offer {offer_id} not found")
            if (
                int(row.selling_price_minor or 0) == selling_price_minor
                and str(row.selling_currency or "").strip().upper() == code
            ):
                return _to_domain(row)
            raise ValueError(
                "direct selling-price writes require audited pricing provenance; "
                "use set_manual_price or the automatic pricing pipeline"
            )

    async def set_manual_price(
        self,
        offer_id: UUID,
        selling_price_minor: int,
        currency: str,
        pricing_metadata: dict[str, object],
        *,
        expected_cost_minor: int,
        expected_cost_currency: str,
        expected_updated_at: object | None = None,
    ) -> SellableOffer | None:
        """Atomically persist an operator price and its manual intent."""
        selling_price_minor = _positive_int64(selling_price_minor, name="selling price")
        code = _audited_currency(currency)
        if not isinstance(pricing_metadata, dict):
            raise ValueError("pricing_metadata must be a mapping")
        async with self._session_factory() as session:
            statement = (
                select(_SellableOfferModel)
                .where(_SellableOfferModel.id == offer_id)
                .with_for_update()
            )
            row = (await session.execute(statement)).scalar_one_or_none()
            if row is None:
                raise OfferNotFoundError(f"offer {offer_id} not found")
            source = str(row.provider_cost_currency or "").strip().upper()
            required_currency = (
                source if source in DOMESTIC_PROVIDER_COST_CURRENCIES else self._catalog_currency
            )
            if code != required_currency:
                raise ValueError(f"selling currency must be {required_currency!r} for this offer")
            if (
                int(row.provider_cost_minor or 0) != expected_cost_minor
                or str(row.provider_cost_currency or "").strip().upper()
                != str(expected_cost_currency or "").strip().upper()
                or (
                    expected_updated_at is not None
                    and getattr(row, "updated_at", None) != expected_updated_at
                )
            ):
                return None
            cast_any: Any = row
            cast_any.selling_price_minor = selling_price_minor
            cast_any.selling_currency = code
            cast_any.auto_priced = False
            manual_metadata = dict(pricing_metadata)
            manual_metadata.pop("fx_repricing_pending", None)
            manual_metadata.pop("catalog_observation_pending", None)
            cast_any.pricing_metadata = manual_metadata
            if not has_valid_pricing_provenance(
                _to_domain(row),
                self._catalog_currency,
                catalog_stale_limit_seconds=self._catalog_stale_limit_seconds,
            ):
                raise ValueError("manual price lacks valid pricing provenance")
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def set_auto_price_if_current(
        self,
        offer_id: UUID,
        *,
        expected_cost_minor: int,
        expected_cost_currency: str,
        selling_price_minor: int,
        selling_currency: str,
        pricing_metadata: dict[str, object],
        expected_provider_rate: str | None = None,
    ) -> SellableOffer | None:
        """Lock and conditionally write one auto-price plus its audit snapshot.

        Manual pricing and operator disable take the same row lock. Whichever
        wins is authoritative; automation never overwrites it.
        """
        selling_price_minor = _positive_int64(selling_price_minor, name="selling price")
        code = _audited_currency(selling_currency)
        if not isinstance(pricing_metadata, dict):
            raise ValueError("pricing_metadata must be a mapping")
        expected_currency = _audited_currency(expected_cost_currency)
        async with self._session_factory() as session:
            statement = (
                select(_SellableOfferModel)
                .where(_SellableOfferModel.id == offer_id)
                .with_for_update()
            )
            row = (await session.execute(statement)).scalar_one_or_none()
            if row is None:
                raise OfferNotFoundError(f"offer {offer_id} not found")
            if (
                not bool(row.auto_priced)
                or bool(row.operator_disabled)
                or int(row.provider_cost_minor or 0) != expected_cost_minor
                or str(row.provider_cost_currency or "").strip().upper() != expected_currency
                or not _rate_equal(
                    _pricing_rate(row),
                    expected_provider_rate or _metadata_rate(row, pricing_metadata),
                )
            ):
                return None
            source = str(row.provider_cost_currency or "").strip().upper()
            required_currency = (
                source if source in DOMESTIC_PROVIDER_COST_CURRENCIES else self._catalog_currency
            )
            if code != required_currency:
                raise ValueError(f"selling currency must be {required_currency!r} for this offer")
            cast_any: Any = row
            cast_any.selling_price_minor = selling_price_minor
            cast_any.selling_currency = code
            cast_any.pricing_metadata = dict(pricing_metadata)
            if not has_valid_pricing_provenance(
                _to_domain(row),
                self._catalog_currency,
                catalog_stale_limit_seconds=self._catalog_stale_limit_seconds,
            ):
                raise ValueError("automatic price lacks valid pricing provenance")
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def publish_if_current(
        self,
        offer_id: UUID,
        *,
        expected_price_minor: int,
        expected_currency: str,
        expected_cost_minor: int,
        expected_cost_currency: str,
        expected_provider_rate: str | None = None,
    ) -> SellableOffer | None:
        """Atomically publish without overwriting an operator disable race."""
        _positive_int64(expected_price_minor, name="expected_price_minor")
        _nonnegative_int64(expected_cost_minor, name="expected_cost_minor")
        code = _audited_currency(expected_currency)
        _audited_currency(expected_cost_currency)
        async with self._session_factory() as session:
            statement = (
                select(_SellableOfferModel)
                .where(_SellableOfferModel.id == offer_id)
                .with_for_update()
            )
            row = (await session.execute(statement)).scalar_one_or_none()
            if row is None:
                raise OfferNotFoundError(f"offer {offer_id} not found")
            metadata: dict[str, object] = dict(row.pricing_metadata or {})
            if (
                bool(row.operator_disabled)
                or bool(metadata.get("fx_repricing_pending"))
                or not bool(row.provider_available)
                or not bool(row.auto_priced)
                or int(row.provider_cost_minor or 0) != expected_cost_minor
                or str(row.provider_cost_currency or "").strip().upper()
                != str(expected_cost_currency or "").strip().upper()
                or str(metadata.get("provider_cost_minor")) != str(expected_cost_minor)
                or int(row.selling_price_minor or 0) != expected_price_minor
                or str(row.selling_currency or "").strip().upper() != code
                or not _rate_equal(
                    _pricing_rate(row),
                    expected_provider_rate or _metadata_rate(row, metadata),
                )
            ):
                return None
            if not has_valid_pricing_provenance(
                _to_domain(row),
                self._catalog_currency,
                catalog_stale_limit_seconds=self._catalog_stale_limit_seconds,
            ):
                return None
            cast_any: Any = row
            cast_any.enabled = True
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def record_auto_pricing_failure_if_current(
        self,
        offer_id: UUID,
        *,
        expected_cost_minor: int,
        expected_cost_currency: str,
        expected_price_minor: int,
        expected_selling_currency: str,
        expected_pricing_metadata: dict[str, object],
        pricing_metadata: dict[str, object],
        preserve_valid_price: bool,
        expected_provider_rate: str | None = None,
    ) -> SellableOffer | None:
        """Record failure and optionally clear the same auto-owned row atomically."""
        _nonnegative_int64(expected_cost_minor, name="expected_cost_minor")
        _nonnegative_int64(expected_price_minor, name="expected_price_minor")
        expected_currency = _audited_currency(expected_cost_currency)
        _audited_currency(expected_selling_currency)
        if not isinstance(expected_pricing_metadata, dict) or not isinstance(
            pricing_metadata, dict
        ):
            raise ValueError("pricing metadata must be a mapping")
        if not isinstance(preserve_valid_price, bool):
            raise ValueError("preserve_valid_price must be boolean")
        async with self._session_factory() as session:
            statement = (
                select(_SellableOfferModel)
                .where(_SellableOfferModel.id == offer_id)
                .with_for_update()
            )
            row = (await session.execute(statement)).scalar_one_or_none()
            if row is None:
                raise OfferNotFoundError(f"offer {offer_id} not found")
            if (
                not bool(row.auto_priced)
                or bool(row.operator_disabled)
                or int(row.provider_cost_minor or 0) != expected_cost_minor
                or str(row.provider_cost_currency or "").strip().upper() != expected_currency
                or int(row.selling_price_minor or 0) != expected_price_minor
                or str(row.selling_currency or "").strip().upper()
                != str(expected_selling_currency or "").strip().upper()
                or not _rate_equal(
                    _pricing_rate(row),
                    expected_provider_rate or _pricing_rate(row, expected_pricing_metadata),
                )
                or dict(row.pricing_metadata or {}) != expected_pricing_metadata
            ):
                return None
            cast_any: Any = row
            if not preserve_valid_price:
                cast_any.selling_price_minor = 0
                required_currency = (
                    str(row.provider_cost_currency or "").strip().upper()
                    if str(row.provider_cost_currency or "").strip().upper()
                    in DOMESTIC_PROVIDER_COST_CURRENCIES
                    else self._catalog_currency
                )
                cast_any.selling_currency = required_currency
            cast_any.pricing_metadata = dict(pricing_metadata)
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def clear_auto_price_if_current(
        self,
        offer_id: UUID,
        *,
        expected_cost_minor: int,
        expected_cost_currency: str,
        pricing_metadata: dict[str, object],
        expected_provider_rate: str | None = None,
    ) -> SellableOffer | None:
        """Unprice only the same auto-owned native-cost observation."""
        _nonnegative_int64(expected_cost_minor, name="expected_cost_minor")
        expected_currency = _audited_currency(expected_cost_currency)
        if not isinstance(pricing_metadata, dict):
            raise ValueError("pricing metadata must be a mapping")
        async with self._session_factory() as session:
            statement = (
                select(_SellableOfferModel)
                .where(_SellableOfferModel.id == offer_id)
                .with_for_update()
            )
            row = (await session.execute(statement)).scalar_one_or_none()
            if row is None:
                raise OfferNotFoundError(f"offer {offer_id} not found")
            if (
                not bool(row.auto_priced)
                or bool(row.operator_disabled)
                or int(row.provider_cost_minor or 0) != expected_cost_minor
                or str(row.provider_cost_currency or "").strip().upper() != expected_currency
                or not _rate_equal(
                    _pricing_rate(row),
                    expected_provider_rate or _metadata_rate(row, pricing_metadata),
                )
            ):
                return None
            cast_any: Any = row
            cast_any.selling_price_minor = 0
            required_currency = (
                str(row.provider_cost_currency or "").strip().upper()
                if str(row.provider_cost_currency or "").strip().upper()
                in DOMESTIC_PROVIDER_COST_CURRENCIES
                else self._catalog_currency
            )
            cast_any.selling_currency = required_currency
            cast_any.pricing_metadata = dict(pricing_metadata)
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def set_pricing_metadata(
        self, offer_id: UUID, pricing_metadata: dict[str, object]
    ) -> SellableOffer:
        if not isinstance(pricing_metadata, dict):
            raise ValueError("pricing_metadata must be a mapping")
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(_SellableOfferModel)
                    .where(_SellableOfferModel.id == offer_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise OfferNotFoundError(f"offer {offer_id} not found")
            cast_any: Any = row
            cast_any.pricing_metadata = dict(pricing_metadata)
            provisional = _to_domain(row)
            valid = has_valid_pricing_provenance(
                provisional,
                self._catalog_currency,
                catalog_stale_limit_seconds=self._catalog_stale_limit_seconds,
            )
            quarantined = bool(
                pricing_metadata.get("fx_repricing_pending")
                or pricing_metadata.get("catalog_observation_pending")
            )
            if not valid and not quarantined:
                raise ValueError("pricing metadata lacks valid canonical provenance")
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
            return SqlAlchemyCatalogSyncStateRepository._to_domain(row)

    async def get(self, provider_key: str) -> CatalogSyncState | None:
        async with self._session_factory() as session:
            row = await session.get(_CatalogSyncStateModel, provider_key)
            return SqlAlchemyCatalogSyncStateRepository._to_domain(row) if row is not None else None

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
            return [SqlAlchemyCatalogSyncStateRepository._to_domain(row) for row in rows]
