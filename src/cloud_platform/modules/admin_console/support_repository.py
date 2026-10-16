"""Privileged support intents: fresh auth, server lock, immutable reason audit."""

from __future__ import annotations

from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import select, text

from cloud_platform.db.base import AuditEvent, Operation, Provider, Server
from cloud_platform.modules.admin_console.support import ACTION_TYPES, server_binding
from cloud_platform.modules.compute.domain import ServerLifecycleState
from cloud_platform.modules.compute.repository import _to_domain as server_from_row
from cloud_platform.modules.operations.repository import _to_domain as operation_from_row
from cloud_platform.modules.users.domain import PermissionDeniedError
from cloud_platform.modules.users.superadmin_repository import authorize_superadmin_transaction


class SqlAlchemyAdminSupportRepository:
    def __init__(self, session_factory):
        self._sessions = session_factory

    async def enqueue(
        self,
        *,
        actor_id,
        server_id,
        action,
        operation_key,
        reason,
        expected_binding,
        expected_state,
        configured_ids,
    ):
        if action not in ACTION_TYPES or not 1 <= len(reason.strip()) <= 500:
            raise ValueError("support request is invalid")
        async with self._sessions() as session, session.begin():
            await authorize_superadmin_transaction(session, actor_id, configured_ids)
            context = {
                "actor_id": str(actor_id),
                "action": action,
                "reason": reason.strip(),
                "binding": expected_binding,
                "expected_state": expected_state,
            }
            lock = int.from_bytes(sha256(operation_key.encode()).digest()[:8], "big", signed=True)
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
            existing = (
                await session.execute(
                    select(Operation).where(Operation.operation_key == operation_key)
                )
            ).scalar_one_or_none()
            if existing is not None:
                if (
                    existing.resource_id != server_id
                    or existing.operation_type != ACTION_TYPES[action].value
                    or (existing.provider_response or {}).get("admin_support") != context
                ):
                    raise ValueError("support intent facts differ")
                return operation_from_row(existing)
            row = (
                await session.execute(
                    select(Server, Provider.name)
                    .join(Provider, Provider.id == Server.provider_id)
                    .where(Server.id == server_id)
                    .with_for_update(of=Server)
                )
            ).one_or_none()
            if row is None:
                raise ValueError("support server unavailable")
            model, provider_key = row
            server = server_from_row(model, provider_key)
            if server_binding(server) != expected_binding or server.state.value != expected_state:
                raise ValueError("support server changed since confirmation")
            operation = Operation(
                id=uuid4(),
                operation_key=operation_key,
                operation_type=ACTION_TYPES[action].value,
                resource_type="server",
                resource_id=server_id,
                provider_key=provider_key,
                status="pending",
                attempts=0,
                provider_response={"admin_support": context},
            )
            if action == "delete":
                server.transition_to(ServerLifecycleState.DELETE_REQUESTED)
                model.state = server.state.value
            session.add(operation)
            session.add(
                AuditEvent(
                    actor_type="admin",
                    actor_id=actor_id,
                    action=f"admin.support.{action}.requested",
                    resource_type="server",
                    resource_id=str(server_id),
                    reason=reason.strip(),
                    event_metadata={
                        "operation_key": operation_key,
                        "owner_id": str(server.user_id),
                        "admin_support": context,
                    },
                )
            )
            await session.flush()
            return operation_from_row(operation)

    async def authorize_dispatch(self, operation, configured_ids):
        """Revalidate durable actor attribution before a queued external mutation."""
        context = (operation.provider_response or {}).get("admin_support")
        if not isinstance(context, dict):
            raise PermissionDeniedError("administrator intent proof is missing")
        try:
            actor_id = UUID(context["actor_id"])
        except (KeyError, ValueError, TypeError) as exc:
            raise PermissionDeniedError("administrator intent identity is invalid") from exc
        async with self._sessions() as session, session.begin():
            await authorize_superadmin_transaction(session, actor_id, configured_ids)
            proof = (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.actor_id == actor_id,
                        AuditEvent.action == f"admin.support.{context.get('action')}.requested",
                        AuditEvent.resource_id == str(operation.resource_id),
                        AuditEvent.event_metadata["operation_key"].astext
                        == operation.operation_key,
                    )
                )
            ).scalar_one_or_none()
            if proof is None or proof.event_metadata.get("admin_support") != context:
                raise PermissionDeniedError("administrator intent audit differs")
            record = (
                await session.execute(
                    select(Server, Provider.name)
                    .join(Provider, Provider.id == Server.provider_id)
                    .where(Server.id == operation.resource_id)
                )
            ).one_or_none()
            if record is None or server_binding(server_from_row(*record)) != context["binding"]:
                raise PermissionDeniedError("administrator intent resource identity changed")
        return actor_id
