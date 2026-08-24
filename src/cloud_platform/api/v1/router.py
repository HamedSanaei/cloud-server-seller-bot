"""The /v1 customer surface (M14-001).

Read resources are fully wired to the platform services; mutating server
endpoints require the idempotency key and are served by the same command
services the Telegram bot uses. See ``docs/api/REST_API_V1.md`` for the
frozen contract.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends

from cloud_platform.core.container import get_container
from cloud_platform.modules.catalog.domain import CatalogRepository
from cloud_platform.modules.compute.service import MyServersService
from cloud_platform.modules.sshkeys import SshKeyService

from .dependencies import get_current_user_id, require_idempotency_key
from .errors import ApiError, ErrorCode

router = APIRouter(prefix="/v1", tags=["v1"])

UserId = Annotated[UUID, Depends(get_current_user_id)]


async def _catalog_repo() -> CatalogRepository:
    container = await get_container()
    return container.catalog_repository()


async def _ssh_key_service() -> SshKeyService:
    container = await get_container()
    return container.ssh_key_service()


async def _my_servers() -> MyServersService:
    from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository

    container = await get_container()
    return MyServersService(SqlAlchemyServerRepository(container.session_factory))


# ---------------------------------------------------------------------------
# Catalog (read-only)
# ---------------------------------------------------------------------------


@router.get("/catalog/offers")
async def list_offers(
    _user: UserId,
    repo: Annotated[CatalogRepository, Depends(_catalog_repo)],
) -> dict[str, Any]:
    offers = [
        {
            "id": str(o.id),
            "provider_key": o.provider_key,
            "plan_id": o.plan_id,
            "location_id": o.location_id,
            "name": o.name,
            "architecture": o.architecture,
            "vcpu": o.vcpu,
            "memory_mb": o.memory_mb,
            "disk_gb": o.disk_gb,
            "currency": o.currency,
            "price_per_quantum": o.price_per_quantum,
            "quantum_seconds": o.quantum_seconds,
            "enabled": o.enabled,
        }
        for o in await repo.list_offers()
        if o.enabled
    ]
    return {"offers": offers}


# ---------------------------------------------------------------------------
# Wallet (read-only)
# ---------------------------------------------------------------------------


@router.get("/wallet")
async def get_wallet(user: UserId) -> dict[str, Any]:
    from cloud_platform.modules.wallet.repository import SqlAlchemyWalletRepository

    container = await get_container()
    wallets = SqlAlchemyWalletRepository(container.session_factory)
    wallet = await wallets.get(user)
    if wallet is None or wallet.id is None:
        raise ApiError(ErrorCode.NOT_FOUND, "no wallet for this user")
    return {
        "wallet": {
            "id": str(wallet.id),
            "balance_minor": wallet.balance,
            "currency": wallet.currency,
        }
    }


# ---------------------------------------------------------------------------
# Servers
# ---------------------------------------------------------------------------


@router.get("/servers")
async def list_servers(
    user: UserId,
    servers: Annotated[MyServersService, Depends(_my_servers)],
) -> dict[str, Any]:
    page = await servers.list_servers(user)
    return {
        "servers": [
            {
                "server_id": str(s.server_id),
                "provider_key": s.provider_key,
                "state": s.state,
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in page.items
        ],
        "total": page.total,
        "offset": page.offset,
        "limit": page.limit,
    }


@router.get("/servers/{server_id}")
async def get_server(
    user: UserId,
    server_id: UUID,
    servers: Annotated[MyServersService, Depends(_my_servers)],
) -> dict[str, Any]:
    detail = await servers.get_server(user, server_id)
    if detail is None:
        # ownership-scoped: a foreign server is indistinguishable from missing
        raise ApiError(ErrorCode.NOT_FOUND, f"server {server_id} not found")
    return {
        "server": {
            "server_id": str(detail.server_id),
            "provider_key": detail.provider_key,
            "state": detail.state,
            "provider_server_id": detail.provider_server_id,
            "created_at": detail.created_at.isoformat() if detail.created_at else None,
        }
    }


@router.post("/servers/{server_id}/actions/{action}", status_code=202)
async def server_action(
    user: UserId,
    server_id: UUID,
    action: str,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
) -> dict[str, Any]:
    """power-on / power-off / reboot - one ledger operation per key."""
    from cloud_platform.modules.operations.service import (
        NotServerOwnerError,
        PowerActionNotAllowedError,
        PowerOperationFailedError,
        PowerOperationInProgressError,
    )

    container = await get_container()
    service = container.power_command_service()
    handlers = {
        "power-on": service.power_on,
        "power-off": service.power_off,
        "reboot": service.reboot,
    }
    handler = handlers.get(action)
    if handler is None:
        raise ApiError(ErrorCode.NOT_FOUND, f"unknown action {action!r}")
    try:
        outcome = await handler(user, server_id, idempotency_key)
    except NotServerOwnerError:
        raise ApiError(ErrorCode.NOT_FOUND, f"server {server_id} not found") from None
    except PowerActionNotAllowedError as exc:
        raise ApiError(ErrorCode.ACTION_NOT_ALLOWED, str(exc)) from None
    except PowerOperationInProgressError as exc:
        raise ApiError(ErrorCode.OPERATION_IN_PROGRESS, str(exc)) from None
    except PowerOperationFailedError as exc:
        raise ApiError(ErrorCode.CONFLICT, str(exc)) from None
    return {
        "action": action,
        "server_id": str(server_id),
        "replayed": outcome.replayed,
        "requeued": outcome.requeued,
        "state": outcome.server.state.value,
    }


@router.delete("/servers/{server_id}")
async def delete_server(
    user: UserId,
    server_id: UUID,
    _key: Annotated[str, Depends(require_idempotency_key)],
) -> dict[str, Any]:
    raise ApiError(
        ErrorCode.NOT_IMPLEMENTED,
        "delete via REST ships with the v1 saga endpoints; use the bot flow meanwhile",
    )


# ---------------------------------------------------------------------------
# SSH keys
# ---------------------------------------------------------------------------


@router.get("/ssh-keys")
async def list_ssh_keys(
    user: UserId,
    service: Annotated[SshKeyService, Depends(_ssh_key_service)],
) -> dict[str, Any]:
    keys = await service.list_keys(actor_user_id=user, owner_user_id=user)
    return {
        "keys": [
            {
                "id": str(k.id),
                "name": k.name,
                "fingerprint": k.fingerprint,
                "created_at": k.created_at.isoformat() if k.created_at else None,
            }
            for k in keys
        ]
    }


@router.post("/ssh-keys", status_code=201)
async def register_ssh_key(
    user: UserId,
    body: dict[str, Any],
    service: Annotated[SshKeyService, Depends(_ssh_key_service)],
    _key: Annotated[str, Depends(require_idempotency_key)],
) -> dict[str, Any]:
    name = body.get("name")
    public_key = body.get("public_key")
    if not isinstance(name, str) or not isinstance(public_key, str):
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, "body must be {name: string, public_key: string}"
        )
    created = await service.register(
        actor_user_id=user, owner_user_id=user, name=name, public_key=public_key
    )
    return {
        "key": {
            "id": str(created.id),
            "name": created.name,
            "fingerprint": created.fingerprint,
        }
    }


@router.delete("/ssh-keys/{key_id}", status_code=204)
async def delete_ssh_key(
    user: UserId,
    key_id: UUID,
    service: Annotated[SshKeyService, Depends(_ssh_key_service)],
    _key: Annotated[str, Depends(require_idempotency_key)],
) -> None:
    await service.delete(actor_user_id=user, key_id=key_id)
