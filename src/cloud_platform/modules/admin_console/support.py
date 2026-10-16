"""Real administrator support operations through the existing durable executors."""

from __future__ import annotations

import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from uuid import UUID

from cloud_platform.core.session_store import BotSessionStore
from cloud_platform.modules.admin_console.auth import authorize
from cloud_platform.modules.audit.domain import ActorType
from cloud_platform.modules.compute.domain import CloudServer
from cloud_platform.modules.operations.domain import OperationStatus, OperationType
from cloud_platform.modules.operations.service import (
    DELETE_PRECONDITION_STATES,
    available_power_actions,
)
from cloud_platform.modules.users.superadmins import validate_telegram_user_ids
from cloud_platform.providers.routing import provider_for
from cloud_platform.providers.vps_ports import vps_capabilities_of

ACTION_TYPES = {
    "power_on": OperationType.POWER_ON,
    "power_off": OperationType.POWER_OFF,
    "reboot": OperationType.REBOOT,
    "delete": OperationType.SERVER_DELETE,
}


def server_binding(server: CloudServer) -> str:
    return sha256(
        f"{server.user_id}:{server.provider_key}:{server.provider_server_id}:{server.credential_account_id}".encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class AdminSupportView:
    server_id: UUID
    owner_id: UUID
    provider_key: str
    state: str
    ipv4: str | None
    actions: tuple[str, ...]


class AdminServerSupportService:
    def __init__(
        self,
        users: Any,
        servers: Any,
        operations: Any,
        registry: Any,
        store: BotSessionStore,
        enqueue: Any,
        *,
        power_executor: Any,
        delete_executor: Any,
        configured_ids: Iterable[int] = (),
        ttl_seconds: int = 900,
    ):
        self._users, self._servers, self._ops, self._registry, self._store = (
            users,
            servers,
            operations,
            registry,
            store,
        )
        self._enqueue, self._power, self._delete = enqueue, power_executor, delete_executor
        self._ids, self._ttl = validate_telegram_user_ids(tuple(configured_ids)), ttl_seconds

    async def detail(self, actor: Any, server_id: UUID) -> AdminSupportView:
        await authorize(self._users, actor, self._ids)
        server = await self._servers.get(server_id)
        if server is None:
            raise ValueError("server unavailable")
        actions = []
        if server.provider_server_id:
            try:
                provider = provider_for(
                    self._registry, server.provider_key, server.credential_account_id
                )
            except KeyError:
                provider = None
            if provider is not None:
                if self._power is not None:
                    actions.extend(
                        action.value
                        for action in available_power_actions(server.state, provider.capabilities)
                    )
                if (
                    self._delete is not None
                    and vps_capabilities_of(provider).deletion
                    and server.state in DELETE_PRECONDITION_STATES
                ):
                    actions.append("delete")
        return AdminSupportView(
            server.id,
            server.user_id,
            server.provider_key,
            server.state.value,
            server.ipv4,
            tuple(actions),
        )

    async def clear(self, actor: Any) -> None:
        admin = await authorize(self._users, actor, self._ids)
        await self._store.delete("admin-support-confirm", str(admin.id))

    async def prepare(self, actor: Any, server_id: UUID, action: str, reason: str) -> str:
        admin = await authorize(self._users, actor, self._ids)
        view = await self.detail(admin, server_id)
        if action not in view.actions or not 1 <= len(reason.strip()) <= 500:
            raise ValueError("unsupported support action or missing reason")
        server = await self._servers.get(server_id)
        nonce = secrets.token_urlsafe(6)
        await self._store.put(
            "admin-support-confirm",
            str(admin.id),
            {
                "nonce": nonce,
                "server_id": str(server_id),
                "action": action,
                "reason": reason.strip(),
                "binding": server_binding(server),
                "state": server.state.value,
            },
            ttl_seconds=self._ttl,
        )
        return nonce

    async def apply(self, actor: Any, nonce: str) -> OperationStatus:
        admin = await authorize(self._users, actor, self._ids)
        record = await self._store.get("admin-support-confirm", str(admin.id))
        if record is None or record.get("nonce") != nonce:
            raise ValueError("support confirmation expired")
        server_id, action = UUID(record["server_id"]), record["action"]
        key = "admin-support:" + sha256(f"{admin.id}:{nonce}".encode()).hexdigest()
        existing = await self._ops.get_by_key(key)
        if existing is not None and existing.status is not OperationStatus.PENDING:
            return existing.status
        server = await self._servers.get(server_id)
        if server is None or server_binding(server) != record["binding"]:
            raise ValueError("support server identity changed")
        if existing is None:
            if action not in (await self.detail(admin, server_id)).actions:
                raise ValueError("support capability changed")
            operation = await self._enqueue.enqueue(
                actor_id=admin.id,
                server_id=server_id,
                action=action,
                operation_key=key,
                reason=record["reason"],
                expected_binding=record["binding"],
                expected_state=record["state"],
                configured_ids=self._ids,
            )
        else:
            operation = existing
        claimed = await self._ops.claim(operation.id)
        if claimed is None:
            current = await self._ops.get_by_key(key)
            return current.status if current is not None else OperationStatus.PENDING
        executor = self._delete if action == "delete" else self._power
        await executor.execute(claimed, actor_type=ActorType.ADMIN, actor_id=admin.id)
        current = await self._ops.get_by_key(key)
        return current.status
