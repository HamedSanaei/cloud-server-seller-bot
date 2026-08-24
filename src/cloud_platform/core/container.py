"""Application dependency container and factories.

This module provides the central dependency container that wires together
all application components (database, providers, services) for the API,
worker, and bot entry points.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cloud_platform.core.config import get_settings
from cloud_platform.db.session import SessionFactory, get_session
from cloud_platform.modules.audit.repository import SqlAlchemyAuditRepository
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.backups.repository import SqlAlchemyBackupSettingsRepository
from cloud_platform.modules.backups.service import BackupsToggleService
from cloud_platform.modules.catalog.repository import SqlAlchemyCatalogRepository
from cloud_platform.modules.credentials.domain import (
    CredentialHolderLike,
)
from cloud_platform.modules.credentials.service import CredentialRotationService
from cloud_platform.modules.firewalls.repository import SqlAlchemyFirewallRepository
from cloud_platform.modules.firewalls.service import FirewallService
from cloud_platform.modules.operations.service import PowerCommandService
from cloud_platform.modules.payments.service import PaymentWebhookService
from cloud_platform.modules.sshkeys.repository import SqlAlchemySshKeyRepository
from cloud_platform.modules.sshkeys.service import SshKeyService
from cloud_platform.modules.tokens.repository import SqlAlchemyApiTokenRepository
from cloud_platform.modules.tokens.service import TokenService
from cloud_platform.providers.allocator import BaseProviderAllocator, CompositeAllocator
from cloud_platform.providers.arvancloud.client import ArvanCloudProvider
from cloud_platform.providers.arvancloud.sync import ArvanCloudCatalogSyncer
from cloud_platform.providers.credentials import CredentialHolder
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


@dataclass(frozen=True)
class _CredentialHolderRegistry:
    """Per-process credential holders, keyed by provider key (M10-008)."""

    _holders: dict[str, CredentialHolder] = field(default_factory=dict, init=False, repr=False)

    def register(self, provider_key: str, holder: CredentialHolder) -> None:
        self._holders[provider_key] = holder

    def get_holder(self, provider_key: str) -> CredentialHolderLike | None:
        return self._holders.get(provider_key)


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
    arvancloud_syncers: tuple[ArvanCloudCatalogSyncer, ...]
    credential_holders: _CredentialHolderRegistry | None = None
    credential_rotation_service: CredentialRotationService | None = None

    def ssh_key_service(self) -> SshKeyService:
        """Ownership-scoped SSH-key service (M13-001), request-scoped."""
        return SshKeyService(
            SqlAlchemySshKeyRepository(self.session_factory),
            AuditTrail(_audit_repository(self.session_factory)),
        )

    def firewall_service(self) -> FirewallService:
        """Ownership-scoped reusable-firewall service (M13-006)."""
        return FirewallService(
            SqlAlchemyFirewallRepository(self.session_factory),
            AuditTrail(_audit_repository(self.session_factory)),
        )

    def token_service(self) -> TokenService:
        """Revocable hashed API-token service (M14-002), request-scoped."""
        return TokenService(
            SqlAlchemyApiTokenRepository(self.session_factory),
            AuditTrail(_audit_repository(self.session_factory)),
        )

    def backups_toggle_service(self, *, settings: Any = None) -> BackupsToggleService:
        """Two-phase backups toggle with confirmed price impact (M13-005)."""
        from cloud_platform.core.config import get_settings
        from cloud_platform.modules.backups.domain import BackupRateCard
        from cloud_platform.modules.pricing.repository import (
            SqlAlchemyServerPriceSnapshotRepository,
        )
        from cloud_platform.modules.pricing.service import ServerPriceSnapshotService

        cfg = settings or get_settings()
        card_provider = lambda: BackupRateCard(surcharge_bps=cfg.backup_surcharge_bps)  # noqa: E731
        return BackupsToggleService(
            settings_repo=SqlAlchemyBackupSettingsRepository(self.session_factory),
            price_snapshots=ServerPriceSnapshotService(
                SqlAlchemyServerPriceSnapshotRepository(self.session_factory),
                _audit_repository(self.session_factory),
            ),
            audit_repo=_audit_repository(self.session_factory),
            rate_card_provider=card_provider,
        )

    def catalog_repository(self) -> SqlAlchemyCatalogRepository:
        """Read-side catalog repository (REST v1 offers listing)."""
        return SqlAlchemyCatalogRepository(self.session_factory)

    def power_command_service(self) -> PowerCommandService:
        """Idempotent power command service (REST v1 server actions)."""
        from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository
        from cloud_platform.modules.operations.repository import SqlAlchemyOperationRepository

        return PowerCommandService(
            server_repo=SqlAlchemyServerRepository(self.session_factory),
            operation_repo=SqlAlchemyOperationRepository(self.session_factory),
            provider_registry=self.provider_registry,
            audit_repo=_audit_repository(self.session_factory),
        )

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
        """Register provider adapters.

        Each configured provider's credential lives in a runtime
        :class:`CredentialHolder` (M10-008): the adapter resolves it at
        request time, so ``CredentialRotationService.rotate`` can swap it
        without downtime. Containers hand-built without a holder registry
        (tests) still register the adapters; they simply have nothing to
        rotate.
        """
        from cloud_platform.core.config import get_settings

        settings = get_settings()
        holders = self.credential_holders
        if settings.hetzner_api_token:
            hetzner_holder = CredentialHolder(settings.hetzner_api_token)
            if holders is not None:
                holders.register("hetzner", hetzner_holder)
            hetzner = HetznerCloudProvider(
                token=settings.hetzner_api_token,
                base_url=settings.hetzner_api_base_url,
                credential_source=hetzner_holder,
            )
            self.provider_registry.register(hetzner)
        if settings.arvancloud_api_key:
            arvancloud_holder = CredentialHolder(settings.arvancloud_api_key)
            if holders is not None:
                holders.register("arvancloud", arvancloud_holder)
            arvancloud = ArvanCloudProvider(
                api_key=settings.arvancloud_api_key,
                base_url=settings.arvancloud_api_base_url,
                region=settings.arvancloud_region,
                credential_source=arvancloud_holder,
            )
            self.provider_registry.register(arvancloud)

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

    # Create ArvanCloud syncer(s) if key available - one per configured region
    arvancloud_syncers: list[ArvanCloudCatalogSyncer] = []
    if settings.arvancloud_api_key:
        arvancloud_provider = ArvanCloudProvider(
            api_key=settings.arvancloud_api_key,
            base_url=settings.arvancloud_api_base_url,
            region=settings.arvancloud_region,
        )
        for region in _configured_regions(settings.arvancloud_region):
            arvancloud_syncers.append(
                ArvanCloudCatalogSyncer(
                    session_factory=session_factory,
                    provider=arvancloud_provider,
                    region=region,
                )
            )

    # M10-008: runtime-rotatable provider credentials - one holder per
    # configured provider, plus the verify-then-swap rotation service.
    holders = _CredentialHolderRegistry()
    container = Container(
        session_factory=session_factory,
        provider_registry=registry,
        provider_allocator=allocator,
        hetzner_syncer=hetzner_syncer,
        arvancloud_syncers=tuple(arvancloud_syncers),
        credential_holders=holders,
        credential_rotation_service=CredentialRotationService(
            holders,
            registry,
            _audit_repository(session_factory),
        ),
    )

    return container


def _audit_repository(
    session_factory: async_sessionmaker[AsyncSession],
) -> SqlAlchemyAuditRepository:
    return SqlAlchemyAuditRepository(session_factory)


def _configured_regions(region_setting: str) -> list[str]:
    """The ArvanCloud regions to sync (comma-separated setting)."""
    regions = [r.strip() for r in (region_setting or "").split(",") if r.strip()]
    return regions or []


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


async def get_credential_rotation_service() -> CredentialRotationService:
    """FastAPI dependency for the provider-credential rotation service."""
    container = await get_container()
    assert container.credential_rotation_service is not None
    return container.credential_rotation_service


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
