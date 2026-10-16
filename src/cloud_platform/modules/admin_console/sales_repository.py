"""Configured-RBAC sales controls and their receipt commit in one transaction."""

from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import select, text

from cloud_platform.db.base import AuditEvent, MaintenanceBlock, SellableOffer
from cloud_platform.modules.fx.domain import minor_to_major
from cloud_platform.modules.offers.domain import (
    has_valid_pricing_provenance,
    required_selling_currency,
    requires_currency_normalization,
)
from cloud_platform.modules.offers.repository import _to_domain
from cloud_platform.modules.users.superadmin_repository import authorize_superadmin_transaction


class SqlAlchemyAdminSalesRepository:
    def __init__(self, session_factory, *, catalog_currency="USD"):
        self._sessions = session_factory
        self._currency = catalog_currency

    async def apply(self, *, actor_id: UUID, configured_ids, facts: dict, operation_key: str):
        action, reason = facts["action"], facts["reason"]
        if (
            action
            not in {
                "show",
                "hide",
                "set_price",
                "block_location",
                "unblock_location",
                "block_provider",
                "unblock_provider",
            }
            or not 1 <= len(reason.strip()) <= 500
        ):
            raise ValueError("invalid sales action or reason")
        async with self._sessions() as session, session.begin():
            await authorize_superadmin_transaction(session, actor_id, configured_ids)
            key = int.from_bytes(sha256(operation_key.encode()).digest()[:8], "big", signed=True)
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
            prior = (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.actor_id == actor_id,
                        AuditEvent.action == "admin.sales.changed",
                        AuditEvent.event_metadata["operation_key"].astext == operation_key,
                    )
                )
            ).scalar_one_or_none()
            if prior is not None:
                if prior.event_metadata != {"operation_key": operation_key, "facts": facts}:
                    raise ValueError("sales idempotency facts changed")
                return action
            # Different offers can share a maintenance scope. A row lock on
            # the selected offer cannot serialize creation of a missing block.
            scope = (
                facts["provider"],
                facts["location"] if action.endswith("location") else "",
            )
            scope_key = int.from_bytes(
                sha256(repr(("admin-sales-scope", scope)).encode()).digest()[:8],
                "big",
                signed=True,
            )
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": scope_key})
            model = (
                await session.execute(
                    select(SellableOffer)
                    .where(SellableOffer.id == UUID(facts["offer_id"]))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if model is None:
                raise ValueError("offer unavailable")
            offer = _to_domain(model)
            if offer.provider_key != facts["provider"] or offer.location_id != facts["location"]:
                raise ValueError("confirmed sales scope changed")
            if action in {"show", "hide"}:
                if action == "show" and (
                    requires_currency_normalization(offer, self._currency)
                    or not offer.provider_available
                    or offer.selling_price_minor <= 0
                    or bool((offer.pricing_metadata or {}).get("fx_repricing_pending"))
                    or offer.technical_metadata.get("deprecated")
                    or not has_valid_pricing_provenance(offer, self._currency)
                ):
                    raise ValueError("offer needs current valid pricing and availability")
                model.enabled, model.operator_disabled = action == "show", action != "show"
            elif action == "set_price":
                amount, currency = facts["price_minor"], facts["currency"]
                if type(amount) is not int or not 0 < amount <= 2**63 - 1:
                    raise ValueError("price must be a positive integer minor amount")
                if currency != required_selling_currency(offer, self._currency):
                    raise ValueError("price currency must match the catalog contract")
                if offer.provider_cost_currency not in {self._currency, "IRT", "IRR"}:
                    raise ValueError("normalize foreign cost through audited catalog FX first")
                if (
                    offer.provider_cost_minor != facts["cost_minor"]
                    or offer.provider_cost_currency != facts["cost_currency"]
                ):
                    raise ValueError("provider cost changed since price confirmation")
                model.selling_price_minor, model.selling_currency = amount, currency
                model.auto_priced = False
                model.pricing_metadata = {
                    "pricing_schema_version": 1,
                    "price_source": "manual",
                    "pricing_mode": "manual",
                    "source_currency": currency,
                    "source_amount": str(minor_to_major(amount, currency)),
                    "target_currency": currency,
                    "provider_cost_minor": offer.provider_cost_minor,
                    "provider_cost_currency": offer.provider_cost_currency,
                    "markup_percent": "0",
                    "rounding": "operator_minor_units",
                    "final_selling_price_minor": amount,
                }
            else:
                location = offer.location_id if action.endswith("location") else ""
                block = (
                    await session.execute(
                        select(MaintenanceBlock)
                        .where(
                            MaintenanceBlock.provider_key == offer.provider_key,
                            MaintenanceBlock.location_id == location,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if action.startswith("unblock"):
                    if block is not None:
                        await session.delete(block)
                elif block is None:
                    session.add(
                        MaintenanceBlock(
                            id=uuid4(),
                            provider_key=offer.provider_key,
                            location_id=location,
                            reason=reason,
                            created_by=actor_id,
                        )
                    )
                else:
                    block.reason, block.created_by = reason, actor_id
            session.add(
                AuditEvent(
                    actor_type="admin",
                    actor_id=actor_id,
                    action="admin.sales.changed",
                    resource_type="sellable_offer",
                    resource_id=str(offer.id),
                    reason=reason,
                    event_metadata={"operation_key": operation_key, "facts": facts},
                )
            )
            await session.flush()
            return action
