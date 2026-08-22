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
