"""Durable, administrator-controlled policy for starting payment orders.

Missing overrides preserve configured gateways' enabled default. Adapters stay
registered when switched off so existing invoices remain reconcilable.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from cloud_platform.modules.users.domain import PermissionDeniedError, UserRepository
from cloud_platform.modules.users.superadmins import require_superadmin, validate_telegram_user_ids


class GatewaySettingsRepository(Protocol):
    async def get(self, key: str) -> bool | None: ...

    async def set_enabled(
        self,
        key: str,
        enabled: bool,
        actor_id: UUID,
        *,
        configured_ids: tuple[int, ...] | None = None,
    ) -> None: ...

    async def list(self) -> dict[str, bool]: ...


@dataclass(frozen=True, slots=True)
class GatewayAvailability:
    key: str
    enabled: bool


class GatewayManagementService:
    def __init__(
        self,
        repository: GatewaySettingsRepository,
        configured_keys: Iterable[str],
        *,
        configured_ids: Iterable[int] = (),
        user_repo: UserRepository | None = None,
    ) -> None:
        self._repository = repository
        self._keys = tuple(dict.fromkeys(configured_keys))
        self._configured_ids = validate_telegram_user_ids(tuple(configured_ids))
        self._users = user_repo

    async def _require_admin(self, admin: object) -> UUID:
        actor = require_superadmin(admin, self._configured_ids)
        assert actor.id is not None
        if self._users is not None:
            fresh = require_superadmin(await self._users.get(actor.id), self._configured_ids)
            if fresh.id != actor.id or fresh.telegram_user_id != actor.telegram_user_id:
                raise PermissionDeniedError("persisted Telegram identity does not match actor")
            actor = fresh
        assert actor.id is not None
        return actor.id

    async def list(self, admin: object) -> list[GatewayAvailability]:
        await self._require_admin(admin)
        overrides = await self._repository.list()
        return [GatewayAvailability(key, overrides.get(key, True)) for key in self._keys]

    async def set_enabled(self, admin: object, key: str, enabled: bool) -> None:
        actor_id = await self._require_admin(admin)
        if key not in self._keys:
            raise ValueError("gateway is not configured")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        await self._repository.set_enabled(
            key, enabled, actor_id, configured_ids=self._configured_ids
        )
