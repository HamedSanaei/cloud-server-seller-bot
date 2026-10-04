"""Tests for application dependency container."""

from __future__ import annotations

import pytest

from cloud_platform.core.container import (
    Container,
    close_container,
    create_container,
    get_container,
)


# Clear the global container before/after tests
@pytest.fixture(autouse=True)
async def reset_container():
    await close_container()
    yield
    await close_container()


def test_create_container():
    """Test container creation."""
    container = create_container()

    assert isinstance(container, Container)
    assert container.session_factory is not None
    assert container.provider_registry is not None
    assert container.provider_allocator is not None


@pytest.mark.asyncio
async def test_get_container_singleton():
    """Test that get_container returns the same instance."""
    container1 = await get_container()
    container2 = await get_container()
    assert container1 is container2


@pytest.mark.asyncio
async def test_container_session():
    """Test container session context manager."""
    container = await get_container()
    async with container.session() as session:
        assert session is not None


@pytest.mark.asyncio
async def test_close_container():
    """Test container cleanup."""
    await get_container()
    await close_container()

    # After closing, next get_container should create new instance
    container = await get_container()
    assert container is not None


async def test_explicit_hetzner_accounts_manage_their_own_resources(monkeypatch):
    import httpx

    from cloud_platform.core import container as container_module
    from cloud_platform.core.config import Settings
    from cloud_platform.providers.routing import UnknownCredentialAccountError

    settings = Settings(
        _env_file=None,
        providers_enabled={"hetzner": True},
        hetzner_accounts=[
            {"id": "preferred", "api_token": "fixture-a", "priority": 1},
            {"id": "owner", "api_token": "fixture-b", "state": "draining", "server_limit": 5},
        ],
    )
    monkeypatch.setattr(container_module, "get_settings", lambda: settings)
    container = create_container()
    try:
        await container.initialize()
        addresses = []

        def respond(request):
            addresses.append(request.headers["Authorization"])
            return httpx.Response(
                200,
                json={
                    "server": {
                        "id": 42,
                        "name": "owned",
                        "status": "off",
                        "server_type": {"name": "cx-test"},
                        "location": {"name": "fsn1"},
                        "image": {"id": 7},
                    }
                },
            )

        owner = container.provider_registry.get_for("hetzner", "owner")
        await owner._client.aclose()
        owner._client = httpx.AsyncClient(
            base_url=settings.hetzner_api_base_url,
            transport=httpx.MockTransport(respond),
        )
        monthly = await owner.get_server("42")
        hourly = (
            await container.hourly_cloud_resolver()
            .adapter_for("hetzner", "owner")
            .get_instance("42")
        )
        assert monthly.id == hourly.provider_server_id == "42"
        assert hourly.account_id == "owner"
        assert addresses == ["Bearer fixture-b", "Bearer fixture-b"]
        with pytest.raises(UnknownCredentialAccountError):
            container.provider_registry.get_for("hetzner", None)
        assert container.hourly_cloud_resolver().adapter_for("hetzner", "removed") is None
    finally:
        await container.close()
