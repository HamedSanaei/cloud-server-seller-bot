"""Admin-controlled grants: one transaction for fresh RBAC, grant and audit.

This adapter deliberately has no provider, wallet, order or subscription service.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from cloud_platform.db.base import AuditEvent, Provider, Server, ServerAccessGrant, User
from cloud_platform.db.timestamps import utc_now
from cloud_platform.modules.compute.repository import _to_domain
from cloud_platform.modules.servers.delegation import binding, permissions, validate_operations
from cloud_platform.modules.servers.test_identity import is_test_server
from cloud_platform.modules.users.superadmin_repository import authorize_superadmin_transaction


class ServerAccessRepository:
    def __init__(self, sessions: Any) -> None:
        self._sessions = sessions

    async def permissions(self, user_id: UUID, server: Any) -> frozenset[str]:
        async with self._sessions() as session:
            grant = (
                await session.execute(
                    select(ServerAccessGrant)
                    .join(User, User.id == ServerAccessGrant.delegate_user_id)
                    .where(
                        ServerAccessGrant.server_id == server.id,
                        ServerAccessGrant.delegate_user_id == user_id,
                        User.status == "active",
                    )
                )
            ).scalar_one_or_none()
            return permissions(grant, server, user_id)

    async def server_ids(self, user_id: UUID) -> list[UUID]:
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(ServerAccessGrant, Server, Provider.name)
                    .join(Server, Server.id == ServerAccessGrant.server_id)
                    .join(Provider, Provider.id == Server.provider_id)
                    .join(User, User.id == ServerAccessGrant.delegate_user_id)
                    .where(
                        ServerAccessGrant.delegate_user_id == user_id,
                        ServerAccessGrant.revoked_at.is_(None),
                        User.status == "active",
                    )
                )
            ).all()
            return [
                row.id
                for grant, model, key in rows
                if "view" in permissions(grant, row := _to_domain(model, key), user_id)
            ]

    async def change(
        self,
        *,
        actor_id: UUID,
        server_id: UUID,
        user_id: UUID,
        operations: list[str],
        reason: str,
        key: str,
        configured_ids: list[int],
        revoke: bool = False,
    ) -> dict[str, Any]:
        selected = [] if revoke else validate_operations(operations)
        if not 4 <= len(reason.strip()) <= 500 or not 8 <= len(key) <= 128:
            raise ValueError("reason and idempotency key required")
        digest = sha256(
            json.dumps(
                {
                    "server": str(server_id),
                    "user": str(user_id),
                    "operations": selected,
                    "reason": reason.strip(),
                    "revoke": revoke,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        command = sha256(f"delegation:{actor_id}:{key}".encode()).hexdigest()
        async with self._sessions() as session, session.begin():
            # Existing superadmin reconciliation lock serializes policy changes;
            # revoked administrators cannot commit a privileged grant.
            await authorize_superadmin_transaction(session, actor_id, configured_ids)
            row = (
                await session.execute(
                    select(Server, Provider.name)
                    .join(Provider, Provider.id == Server.provider_id)
                    .where(Server.id == server_id)
                    .with_for_update(of=Server)
                )
            ).one_or_none()
            if row is None:
                raise ValueError("test resource is not registered")
            server = _to_domain(*row)
            if not revoke and (not is_test_server(server) or server.user_id == user_id):
                raise ValueError(
                    "verified reserved operator resource and distinct delegate required"
                )
            user = (
                await session.execute(select(User).where(User.id == user_id).with_for_update())
            ).scalar_one_or_none()
            if user is None or (
                not revoke and (user.status != "active" or user.telegram_user_id is None)
            ):
                raise ValueError("active linked platform identity required")
            grant = (
                await session.execute(
                    select(ServerAccessGrant)
                    .where(
                        ServerAccessGrant.server_id == server_id,
                        ServerAccessGrant.delegate_user_id == user_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if not revoke and grant is not None and grant.owner_user_id != server.user_id:
                raise ValueError("resource ownership changed; operator reconciliation required")
            previous = (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.action == "operator.test_server.delegation",
                        AuditEvent.actor_id == actor_id,
                        AuditEvent.event_metadata["command"].astext == command,
                    )
                )
            ).scalar_one_or_none()
            if previous is not None:
                if previous.event_metadata["digest"] != digest:
                    raise ValueError("idempotency key facts differ")
                return self._view(grant, server, user_id, replayed=True)
            before = sorted(permissions(grant, server, user_id))
            now = utc_now()
            if revoke:
                if grant is not None and grant.revoked_at is None:
                    grant.revoked_at = now
            elif grant is None:
                grant = ServerAccessGrant(
                    id=uuid4(),
                    server_id=server_id,
                    delegate_user_id=user_id,
                    owner_user_id=server.user_id,
                    binding=binding(server),
                    operations=selected,
                    granted_by=actor_id,
                    reason=reason.strip(),
                    created_at=now,
                )
                session.add(grant)
            elif before != selected:
                grant.binding, grant.operations, grant.granted_by = (
                    binding(server),
                    selected,
                    actor_id,
                )
                grant.reason, grant.created_at, grant.revoked_at = reason.strip(), now, None
            session.add(
                AuditEvent(
                    id=uuid4(),
                    actor_type="admin",
                    actor_id=actor_id,
                    action="operator.test_server.delegation",
                    resource_type="server",
                    resource_id=str(server_id),
                    reason=reason.strip(),
                    event_metadata={
                        "command": command,
                        "digest": digest,
                        "delegate_user_id": str(user_id),
                        "owner_id": str(server.user_id),
                        "before": before,
                        "after": sorted(permissions(grant, server, user_id)),
                        "revoked": revoke,
                    },
                )
            )
            await session.flush()
            return self._view(grant, server, user_id, replayed=False)

    @staticmethod
    def _view(grant: Any, server: Any, user_id: UUID, *, replayed: bool) -> dict[str, Any]:
        operations = sorted(permissions(grant, server, user_id))
        return {
            "serverId": str(server.id),
            "platformUserId": str(user_id),
            "operations": operations,
            "active": bool(operations),
            "replayed": replayed,
        }
