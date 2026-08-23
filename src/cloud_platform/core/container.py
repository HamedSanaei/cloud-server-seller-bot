"""Application dependency container and factories.

This module provides the central dependency container that wires together
all application components (database, providers, services) for the API,
worker, and bot entry points.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cloud_platform.core.config import get_settings
from cloud_platform.db.session import SessionFactory, get_session
from cloud_platform.modules.payments.service import PaymentWebhookService
from cloud_platform.providers.allocator import BaseProviderAllocator, CompositeAllocator
from cloud_platform.providers.hetzner.client import HetznerCloudProvider
from cloud_platform.providers.hetzner.sync import HetznerCatalogSyncer
from cloud_platform.providers.registry import ProviderRegistry

# Re-export for convenience
__all__ = [
    "Container",
    "SessionFactory",
    "create_container",
    "get_container",
    "get_session",
]


@dataclass(frozen=True, slots=True)
class Container:
    """Application dependency container.

    Holds all shared dependencies and provides factory methods for
    creating request-scoped services.
    """

    session_factory: async_sessionmaker[AsyncSession]
    provider_registry: ProviderRegistry
    provider_allocator: BaseProviderAllocator
    hetzner_syncer: HetznerCatalogSyncer | None

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Create a new database session."""
        async with self.session_factory() as session:
            yield session

    async def initialize(self) -> None:
        """Initialize container (create database tables, register providers)."""
        # Database tables are created via Alembic migrations
        # Here we just register providers
        self._register_providers()

    def _register_providers(self) -> None:
        """Register provider adapters."""
        from cloud_platform.core.config import get_settings

        settings = get_settings()
        if settings.hetzner_api_token:
            hetzner = HetznerCloudProvider(
                token=settings.hetzner_api_token,
                base_url=settings.hetzner_api_base_url,
            )
            self.provider_registry.register(hetzner)

    async def close(self) -> None:
        """Close all resources."""
        # Close provider connections
        for provider in self.provider_registry._providers.values():
            if hasattr(provider, "close"):
                await provider.close()


_container: Container | None = None


def create_container() -> Container:
    """Create and configure the application container.

    Returns:
        A fully configured Container instance.
    """
    settings = get_settings()

    # Create database engine and session factory
    engine = create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        echo=settings.log_level == "DEBUG",
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # Create provider registry and allocator
    registry = ProviderRegistry()
    allocator = CompositeAllocator([])  # Providers registered later

    # Create Hetzner syncer if token available
    hetzner_syncer = None
    if settings.hetzner_api_token:
        hetzner_syncer = HetznerCatalogSyncer(
            session_factory=session_factory,
            token=settings.hetzner_api_token,
            base_url=settings.hetzner_api_base_url,
        )

    container = Container(
        session_factory=session_factory,
        provider_registry=registry,
        provider_allocator=allocator,
        hetzner_syncer=hetzner_syncer,
    )

    return container


async def get_container() -> Container:
    """Get the global container instance, creating it if needed.

    Returns:
        The global Container instance.
    """
    global _container
    if _container is None:
        _container = create_container()
        await _container.initialize()
    return _container


async def close_container() -> None:
    """Close the global container and release resources."""
    global _container
    if _container is not None:
        await _container.close()
        _container = None


# FastAPI dependency injection helpers
async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency for database session."""
    container = await get_container()
    async with container.session() as session:
        yield session


async def get_provider_registry() -> ProviderRegistry:
    """FastAPI dependency for provider registry."""
    container = await get_container()
    return container.provider_registry


async def get_provider_allocator() -> CompositeAllocator:
    """FastAPI dependency for provider allocator."""
    container = await get_container()
    allocator = container.provider_allocator
    assert isinstance(allocator, CompositeAllocator)
    return allocator


async def get_payment_webhook_service() -> PaymentWebhookService:
    """FastAPI dependency for the replay-safe payment webhook service."""
    from cloud_platform.modules.payments.repository import SqlAlchemyPaymentSessionRepository
    from cloud_platform.modules.payments.service import PaymentWebhookService
    from cloud_platform.modules.wallet.repository import (
        SqlAlchemyLedgerRepository,
        SqlAlchemyWalletRepository,
    )

    container = await get_container()
    return PaymentWebhookService(
        payments_repo=SqlAlchemyPaymentSessionRepository(container.session_factory),
        wallet_repo=SqlAlchemyWalletRepository(container.session_factory),
        ledger_repo=SqlAlchemyLedgerRepository(container.session_factory),
    )
