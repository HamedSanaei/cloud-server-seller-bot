"""Freshly authorized sales controls and local operational health reads."""

from __future__ import annotations

import secrets
from typing import Any
from uuid import UUID

from cloud_platform.core.session_store import BotSessionStore
from cloud_platform.modules.admin_console.auth import authorize
from cloud_platform.modules.users.superadmins import validate_telegram_user_ids


class AdminSalesService:
    def __init__(
        self,
        users: Any,
        offers: Any,
        offer_admin: Any,
        maintenance: Any,
        reader: Any,
        store: BotSessionStore,
        *,
        configured_ids=(),
        ttl_seconds=900,
        mutations=None,
    ):
        self._users, self._offers, self._admin, self._maintenance = (
            users,
            offers,
            offer_admin,
            maintenance,
        )
        self._reader, self._store, self._ids, self._ttl = (
            reader,
            store,
            validate_telegram_user_ids(tuple(configured_ids)),
            ttl_seconds,
        )
        self._mutations = mutations

    async def list_offers(self, actor, offset=0):
        await authorize(self._users, actor, self._ids)
        if type(offset) is not int or not 0 <= offset <= 10000:
            raise ValueError("offset out of bounds")
        return await self._reader.list_offers(offset=offset, limit=10)

    async def detail(self, actor, offer_id: UUID):
        await authorize(self._users, actor, self._ids)
        offer = await self._offers.get(offer_id)
        if offer is None:
            raise ValueError("offer unavailable")
        return offer

    async def clear(self, actor):
        admin = await authorize(self._users, actor, self._ids)
        await self._store.delete("admin-sales-confirm", str(admin.id))

    async def prepare(self, actor, offer_id: UUID, action: str, reason: str, *, price_minor=None):
        admin = await authorize(self._users, actor, self._ids)
        offer = await self.detail(admin, offer_id)
        if (
            action
            not in {
                "show",
                "hide",
                "block_location",
                "unblock_location",
                "block_provider",
                "unblock_provider",
                "set_price",
            }
            or not 1 <= len(reason.strip()) <= 500
        ):
            raise ValueError("invalid sales action or reason")
        if action == "set_price" and (type(price_minor) is not int or not 0 < price_minor < 2**63):
            raise ValueError("a positive integer minor-unit price is required")
        nonce = secrets.token_urlsafe(6)
        await self._store.put(
            "admin-sales-confirm",
            str(admin.id),
            {
                "nonce": nonce,
                "offer_id": str(offer_id),
                "action": action,
                "reason": reason.strip(),
                "provider": offer.provider_key,
                "location": offer.location_id,
                "price_minor": price_minor,
                "currency": offer.selling_currency,
                "cost_minor": offer.provider_cost_minor,
                "cost_currency": offer.provider_cost_currency,
            },
            ttl_seconds=self._ttl,
        )
        return nonce

    async def apply(self, actor, nonce: str):
        admin = await authorize(self._users, actor, self._ids)
        record = await self._store.get("admin-sales-confirm", str(admin.id))
        if record is None or record.get("nonce") != nonce:
            raise ValueError("sales confirmation expired")
        offer = await self.detail(admin, UUID(record["offer_id"]))
        if offer.provider_key != record["provider"] or offer.location_id != record["location"]:
            raise ValueError("offer scope changed")
        if self._mutations is None:
            raise RuntimeError("transactional sales controls are unavailable")
        facts = {key: value for key, value in record.items() if key != "nonce"}
        return await self._mutations.apply(
            actor_id=admin.id,
            configured_ids=self._ids,
            facts=facts,
            operation_key=f"admin-sales:{admin.id}:{nonce}",
        )


class AdminHealthService:
    def __init__(self, users, operations, reader, *, configured_ids=()):
        self._users, self._operations, self._reader = users, operations, reader
        self._ids = validate_telegram_user_ids(tuple(configured_ids))

    async def snapshot(self, actor):
        await authorize(self._users, actor, self._ids)
        return await self._reader.health()

    async def failed(self, actor):
        await authorize(self._users, actor, self._ids)
        rows = await self._operations.list_failed(limit=20)
        # Raw provider errors can contain account data: the UI gets safe facts.
        return tuple(
            (row.id, row.resource_id, row.operation_type.value, row.status.value, row.attempts)
            for row in rows
        )
