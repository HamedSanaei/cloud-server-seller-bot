"""Assemble the REAL v1 ASGI stack over fake-backed ports (M16-002).

``build_probe_app()`` returns a FastAPI application containing the real
health + v1 routers (routing, bearer-auth dependency chain, scope
checks, the sliding-window rate limiter, the one error envelope) with
the DATA PORTS replaced by in-memory fakes. That keeps measurements
reproducible on any machine while still exercising the full request
pipeline - only Postgres/Redis/providers are cut out.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI

from cloud_platform.api.routes.health import router as health_router
from cloud_platform.api.v1 import router as v1_router
from cloud_platform.api.v1.dependencies import get_token_authentication
from cloud_platform.api.v1.ratelimit import SlidingWindowRateLimiter
from cloud_platform.api.v1.router import _catalog_repo, _my_servers, _ssh_key_service
from cloud_platform.modules.compute.service import MyServersService
from cloud_platform.modules.tokens.domain import TokenAuthentication, TokenScope


def _auth() -> TokenAuthentication:
    return _AUTH


_AUTH = TokenAuthentication(user_id=uuid4(), scopes=frozenset(TokenScope), token_id=uuid4())

_OFFERS = [
    type(
        "Offer",
        (),
        {
            "id": uuid4(),
            "provider_key": "hetzner",
            "plan_id": f"cx{i}",
            "location_id": "fsn1",
            "name": f"CX{i}",
            "architecture": "x86",
            "vcpu": 2,
            "memory_mb": 4096,
            "disk_gb": 40,
            "currency": "EUR",
            "price_per_quantum": "11",
            "quantum_seconds": 3600,
            "enabled": True,
            "description": "",
        },
    )()
    for i in range(50)
]


class FakeCatalogRepo:
    async def list_offers(self) -> list[Any]:
        return [o for o in _OFFERS if o.enabled]


class FakeServersService(MyServersService):
    """Same interface, zero persistence."""

    def __init__(self) -> None:
        pass

    async def list_servers(self, user_id: UUID, *, offset: int = 0, limit: int = 20) -> Any:
        del user_id
        items = [
            type(
                "S",
                (),
                {
                    "server_id": uuid4(),
                    "provider_key": "hetzner",
                    "state": "running",
                    "created_at": datetime.now(UTC),
                },
            )()
            for _ in range(20)
        ]
        return type(
            "Page",
            (),
            {"items": items, "total": len(items), "offset": offset, "limit": limit},
        )()


class FakeSshKeyService:
    async def register(self, **_kw: Any) -> Any:  # pragma: no cover - unused here
        raise NotImplementedError

    async def list_keys(self, *, actor_user_id: UUID, owner_user_id: UUID) -> list[Any]:
        del actor_user_id, owner_user_id
        return []

    async def delete(self, **_kw: Any) -> None:  # pragma: no cover - unused here
        raise NotImplementedError


def build_probe_app(rate_limit_per_minute: int = 100_000) -> FastAPI:
    """The real v1 stack; data ports faked; auth pinned to one identity."""
    from cloud_platform.api.v1 import install_error_handlers

    app = FastAPI(title="cloud-platform-load-probe")
    install_error_handlers(app)
    app.include_router(health_router)
    app.include_router(v1_router)
    app.state.rate_limiter = SlidingWindowRateLimiter(rate_limit_per_minute)
    app.dependency_overrides[get_token_authentication] = _auth
    app.dependency_overrides[_catalog_repo] = lambda: FakeCatalogRepo()
    app.dependency_overrides[_my_servers] = lambda: FakeServersService()
    app.dependency_overrides[_ssh_key_service] = lambda: FakeSshKeyService()
    return app
