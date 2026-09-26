"""Application dependency container and factories.

This module provides the central dependency container that wires together
all application components (database, providers, services) for the API,
worker, and bot entry points.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from cloud_platform.core.config import get_settings
from cloud_platform.core.session_store import BotSessionStore, build_session_store
from cloud_platform.db.session import SessionFactory, get_session
from cloud_platform.modules.audit.repository import SqlAlchemyAuditRepository
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.backups.repository import SqlAlchemyBackupSettingsRepository
from cloud_platform.modules.backups.service import BackupsToggleService
from cloud_platform.modules.catalog.repository import (
    SqlAlchemyCatalogRepository,
    SqlAlchemyLocationRepository,
)
from cloud_platform.modules.catalog.service import (
    BuyFlowViewService,
    CatalogViewService,
    OsSelectionService,
    PurchaseConfirmationService,
)
from cloud_platform.modules.compute.service import CreateServerService
from cloud_platform.modules.credentials.domain import (
    CredentialHolderLike,
)
from cloud_platform.modules.credentials.service import CredentialRotationService
from cloud_platform.modules.firewalls.repository import SqlAlchemyFirewallRepository
from cloud_platform.modules.firewalls.service import FirewallService
from cloud_platform.modules.networking.ip_service import IpService
from cloud_platform.modules.networking.network_service import NetworkService
from cloud_platform.modules.networking.volume_service import VolumeService
from cloud_platform.modules.operations.service import PowerCommandService
from cloud_platform.modules.payments.repository import SqlAlchemyPaymentSessionRepository
from cloud_platform.modules.payments.service import PaymentWebhookService
from cloud_platform.modules.pricing.service import PriceBookService, ServerPriceSnapshotService
from cloud_platform.modules.sshkeys.repository import SqlAlchemySshKeyRepository
from cloud_platform.modules.sshkeys.service import SshKeyService
from cloud_platform.modules.tokens.repository import SqlAlchemyApiTokenRepository
from cloud_platform.modules.tokens.service import TokenService
from cloud_platform.modules.users.repository import SqlAlchemyUserRepository
from cloud_platform.modules.wallet.domain import WalletRepository
from cloud_platform.providers.allocator import BaseProviderAllocator, CompositeAllocator
from cloud_platform.providers.arvancloud.client import ArvanCloudProvider
from cloud_platform.providers.arvancloud.sync import ArvanCloudCatalogSyncer
from cloud_platform.providers.credentials import CredentialHolder
from cloud_platform.providers.hetzner.client import HetznerCloudProvider
from cloud_platform.providers.hetzner.sync import HetznerCatalogSyncer
from cloud_platform.providers.leaseweb.client import LeaseWebProvider
from cloud_platform.providers.leaseweb.ordering import (
    LeaseWebOrderingProvider,
)
from cloud_platform.providers.leaseweb.ordering_sync import LeaseWebOrderingCatalogSyncer
from cloud_platform.providers.leaseweb.sync import LeaseWebCatalogSyncer
from cloud_platform.providers.registry import ProviderRegistry

# Re-export for convenience
__all__ = [
    "Container",
    "SessionFactory",
    "create_container",
    "get_container",
    "get_session",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _CredentialHolderRegistry:
    """Per-process credential holders (M10-008).

    Keyed by ``(provider_key, credential_account_id)`` so a provider served by
    MULTIPLE credential accounts (LEASEWEB-MULTIACCOUNT) keeps one holder per
    account: rotating ``lw-eu``'s key provably cannot touch ``lw-asia``'s.
    A single-credential provider registers with an account id of ``None`` and
    is still addressed by provider key alone.
    """

    _holders: dict[tuple[str, str | None], CredentialHolder] = field(
        default_factory=dict, init=False, repr=False
    )

    def register(
        self,
        provider_key: str,
        holder: CredentialHolder,
        credential_account_id: str | None = None,
    ) -> None:
        account_id = (credential_account_id or "").strip() or None
        self._holders[(provider_key, account_id)] = holder

    def get_holder(
        self, provider_key: str, credential_account_id: str | None = None
    ) -> CredentialHolderLike | None:
        account_id = (credential_account_id or "").strip() or None
        holder = self._holders.get((provider_key, account_id))
        if holder is None and account_id is None:
            # Legacy callers may address a multi-account provider without an
            # account id; resolve it deterministically to an enabled account
            # rather than failing outright (reads only — never used to decide
            # WHICH account a billable call goes to).
            for (key, _account), candidate in sorted(
                self._holders.items(), key=lambda item: (item[0][0], item[0][1] or "")
            ):
                if key == provider_key:
                    return candidate
        return holder

    def account_ids(self, provider_key: str) -> tuple[str, ...]:
        """Every account id registered for one provider (sorted, no secrets)."""
        return tuple(
            sorted(
                account_id or "default" for key, account_id in self._holders if key == provider_key
            )
        )


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
    leaseweb_syncer: LeaseWebCatalogSyncer | None = None
    arvancloud_syncers: tuple[ArvanCloudCatalogSyncer, ...] = ()
    leaseweb_ordering_syncer: LeaseWebOrderingCatalogSyncer | None = None
    leaseweb_ordering_provider: LeaseWebOrderingProvider | None = None
    #: LEASEWEB-MULTIACCOUNT: the per-credential-account adapters. Present only
    #: when several Leaseweb API keys are configured; ``None`` means the legacy
    #: single-credential deployment, where the logical adapter is the only one.
    leaseweb_account_router: Any | None = None
    #: Hourly-cloud per-credential-account adapters (Public Cloud is account
    #: scoped like ordering). ``None`` means no hourly credential is
    #: configured; callers skip the hourly product instead of failing.
    leaseweb_cloud_account_router: Any | None = None
    credential_holders: _CredentialHolderRegistry | None = None
    credential_rotation_service: CredentialRotationService | None = None
    # PROD-HARDENING §2: the process-wide Telegram transient-state backend.
    # Selected once in :func:`create_container` from ``[telegram.sessions]``
    # (Redis in production, in-memory for tests/development). Containers
    # built directly (tests) leave it ``None`` and get a lazily built one.
    bot_session_store: BotSessionStore | None = None
    engine: AsyncEngine | None = None
    #: Transports/adapters created solely for the catalog syncers. They are
    #: not provider-registry routes, but they are still process-owned and must
    #: be closed by :meth:`close` (the manual catalog scripts expose the
    #: syncers through these fields).
    owned_resources: tuple[Any, ...] = field(default=(), repr=False)
    _global_fx_resolver: Any | None = field(default=None, init=False, repr=False, compare=False)
    _domestic_fx_resolver: Any | None = field(default=None, init=False, repr=False, compare=False)
    # Lifecycle guard: :meth:`initialize` registers provider adapters, and
    # the registry rejects duplicates. The flag makes a second initialize a
    # no-op instead of a double registration; it is set only after a fully
    # successful registration, so a failed initialize can be retried.
    # Initialized once per instance: after :meth:`close`, build a new
    # container (what :func:`get_container`/:func:`close_container` do)
    # rather than re-initializing a closed one.
    _initialized: bool = field(default=False, init=False, repr=False, compare=False)
    _closed: bool = field(default=False, init=False, repr=False, compare=False)
    _closing: bool = field(default=False, init=False, repr=False, compare=False)
    # Successful resource closures are remembered across a retry.  A failed
    # resource must remain eligible for a second attempt without re-closing
    # transports that already completed successfully.
    _closed_resource_ids: set[int] = field(
        default_factory=set, init=False, repr=False, compare=False
    )

    @property
    def catalog_currency(self) -> str:
        return get_settings().fx_catalog_pricing_currency

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

    def catalog_view_service(self) -> CatalogViewService:
        """Customer-facing catalog browsing by country (M08-002)."""
        return CatalogViewService(
            self.catalog_repository(),
            SqlAlchemyLocationRepository(self.session_factory),
        )

    def buy_flow_service(self) -> BuyFlowViewService:
        """Buy-flow screens (locations/plans) with signed callbacks (M08-003)."""
        return BuyFlowViewService(
            self.catalog_repository(),
            SqlAlchemyLocationRepository(self.session_factory),
            get_settings().callback_signing_key,
        )

    def os_selection_service(self) -> OsSelectionService:
        """Architecture-gated OS selection screen (M08-004)."""
        return OsSelectionService(
            self.catalog_repository(),
            self.provider_registry,
            get_settings().callback_signing_key,
        )

    def price_book_service(self) -> PriceBookService:
        """The versioned price book the selling price derives from (M06)."""
        from cloud_platform.modules.pricing.repository import SqlAlchemyPriceBookRepository

        return PriceBookService(
            SqlAlchemyPriceBookRepository(self.session_factory),
            _audit_repository(self.session_factory),
        )

    def server_price_snapshot_service(self) -> ServerPriceSnapshotService:
        """Immutable per-server price snapshots (M06-003)."""
        from cloud_platform.modules.pricing.repository import (
            SqlAlchemyServerPriceSnapshotRepository,
        )

        return ServerPriceSnapshotService(
            SqlAlchemyServerPriceSnapshotRepository(self.session_factory),
            _audit_repository(self.session_factory),
        )

    def purchase_confirmation_service(self) -> PurchaseConfirmationService:
        """The buy.confirm screen: exact price policy + wallet impact (M08-005)."""
        from cloud_platform.modules.wallet.repository import SqlAlchemyWalletRepository

        settings = get_settings()
        return PurchaseConfirmationService(
            catalog_repo=self.catalog_repository(),
            price_book_service=self.price_book_service(),
            wallet_repo=SqlAlchemyWalletRepository(self.session_factory),
            signing_key=settings.callback_signing_key,
            book_name=settings.price_book_name,
        )

    def create_server_service(self) -> CreateServerService:
        """The idempotent create-server command (M06-002/M08 order path)."""
        from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository
        from cloud_platform.modules.provider_accounts.repository import (
            SqlAlchemyProviderAccountRepository,
        )
        from cloud_platform.modules.wallet.repository import (
            SqlAlchemyHoldRepository,
            SqlAlchemyWalletRepository,
        )

        settings = get_settings()
        return CreateServerService(
            server_repo=SqlAlchemyServerRepository(self.session_factory),
            account_repo=SqlAlchemyProviderAccountRepository(self.session_factory),
            catalog_repo=self.catalog_repository(),
            price_book_service=self.price_book_service(),
            snapshot_service=self.server_price_snapshot_service(),
            wallet_repo=SqlAlchemyWalletRepository(self.session_factory),
            hold_repo=SqlAlchemyHoldRepository(self.session_factory),
            audit_repo=self.audit_repository(),
            book_name=settings.price_book_name,
        )

    def user_repository(self) -> SqlAlchemyUserRepository:
        """User persistence (Telegram onboarding lookups, M02-002)."""
        return SqlAlchemyUserRepository(self.session_factory)

    def wallet_repository(self) -> WalletRepository:
        """Wallet persistence (bot confirmation screen, M08-005)."""
        from cloud_platform.modules.wallet.repository import SqlAlchemyWalletRepository

        return SqlAlchemyWalletRepository(self.session_factory)

    def audit_repository(self) -> Any:
        """Request-scoped audit log repository (web panel feeds)."""
        return _audit_repository(self.session_factory)

    def ip_service(self) -> IpService:
        """Ownership-scoped floating-IP lifecycle service (M13-008)."""
        from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository
        from cloud_platform.modules.networking.ip_repository import (
            SqlAlchemyIpAddressRepository,
        )

        return IpService(
            repo=SqlAlchemyIpAddressRepository(self.session_factory),
            audit_repo=_audit_repository(self.session_factory),
            server_repo=SqlAlchemyServerRepository(self.session_factory),
        )

    def volume_service(self) -> VolumeService:
        """Ownership-scoped volume lifecycle with reconciliation (M13-009)."""
        from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository
        from cloud_platform.modules.networking.volume_repository import (
            SqlAlchemyVolumeRepository,
        )

        return VolumeService(
            repo=SqlAlchemyVolumeRepository(self.session_factory),
            audit_repo=_audit_repository(self.session_factory),
            server_repo=SqlAlchemyServerRepository(self.session_factory),
        )

    def network_service(self) -> NetworkService:
        """Ownership-scoped private-network lifecycle service (M13-010)."""
        from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository
        from cloud_platform.modules.networking.network_repository import (
            SqlAlchemyNetworkRepository,
        )

        return NetworkService(
            repo=SqlAlchemyNetworkRepository(self.session_factory),
            audit_repo=_audit_repository(self.session_factory),
            server_repo=SqlAlchemyServerRepository(self.session_factory),
        )

    def sellable_offer_repository(self) -> Any:
        """Sellable-offer price book repository (LEASEWEB-MVP)."""
        from cloud_platform.modules.offers.repository import SqlAlchemySellableOfferRepository

        settings = get_settings()
        return SqlAlchemySellableOfferRepository(
            self.session_factory,
            catalog_currency=settings.fx_catalog_pricing_currency,
            catalog_stale_limit_seconds=settings.fx_frankfurter_catalog_max_stale_seconds,
        )

    def provider_order_repository(self) -> Any:
        """Provider-order repository (LEASEWEB-MVP)."""
        from cloud_platform.modules.orders.repository import (
            SqlAlchemyProviderOrderRepository,
        )

        return SqlAlchemyProviderOrderRepository(self.session_factory)

    def renewal_repository(self) -> Any:
        """Renewal records + notification log (LEASEWEB-MVP)."""
        from cloud_platform.modules.renewals.repository import (
            SqlAlchemyRenewalRepository,
        )

        return SqlAlchemyRenewalRepository(self.session_factory)

    def renewal_notification_repository(self) -> Any:
        from cloud_platform.modules.renewals.repository import (
            SqlAlchemyRenewalNotificationRepository,
        )

        return SqlAlchemyRenewalNotificationRepository(self.session_factory)

    def ledger_repository(self) -> Any:
        from cloud_platform.modules.wallet.repository import SqlAlchemyLedgerRepository

        return SqlAlchemyLedgerRepository(self.session_factory)

    def hold_service(self) -> Any:
        from cloud_platform.modules.wallet.repository import (
            HoldService,
            SqlAlchemyHoldRepository,
            SqlAlchemyLedgerRepository,
            SqlAlchemyWalletRepository,
        )

        return HoldService(
            SqlAlchemyWalletRepository(self.session_factory),
            SqlAlchemyHoldRepository(self.session_factory),
            SqlAlchemyLedgerRepository(self.session_factory),
        )

    def monthly_checkout_service(self) -> Any:
        """The financially safe monthly checkout command (LEASEWEB-MVP)."""
        from cloud_platform.modules.checkout.service import MonthlyCheckoutService
        from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository
        from cloud_platform.modules.operations.repository import SqlAlchemyOperationRepository
        from cloud_platform.modules.provider_accounts.repository import (
            SqlAlchemyProviderAccountRepository,
        )
        from cloud_platform.modules.wallet.repository import (
            SqlAlchemyHoldRepository,
            SqlAlchemyWalletRepository,
        )

        return MonthlyCheckoutService(
            server_repo=SqlAlchemyServerRepository(self.session_factory),
            offers_repo=self.sellable_offer_repository(),
            account_repo=SqlAlchemyProviderAccountRepository(self.session_factory),
            wallet_repo=SqlAlchemyWalletRepository(self.session_factory),
            hold_repo=SqlAlchemyHoldRepository(self.session_factory),
            orders_repo=self.provider_order_repository(),
            operation_repo=SqlAlchemyOperationRepository(self.session_factory),
            audit_repo=_audit_repository(self.session_factory),
            provider_registry=self.provider_registry,
            event_sink=self.business_event_sink(),
            event_market_lookup=self.provider_market,
            # LEASEWEB-MULTIACCOUNT: resolve and PIN the fulfillment credential
            # account before anything is persisted, so the worker never has to
            # re-decide whose API key to bill for this order.
            fulfillment_routes=self.provider_route_selector(),
        )

    def provider_route_selector(self) -> Any:
        """Durable, provider-neutral fulfillment-account resolver."""
        from cloud_platform.modules.provider_routes.service import ProviderRouteSelector

        return ProviderRouteSelector(self.session_factory)

    def offer_catalog_view_service(self) -> Any:
        """Customer-facing monthly offer screens (LEASEWEB-MVP)."""
        from cloud_platform.modules.checkout.service import OfferCatalogViewService
        from cloud_platform.modules.wallet.repository import SqlAlchemyWalletRepository

        return OfferCatalogViewService(
            offers_repo=self.sellable_offer_repository(),
            provider_registry=self.provider_registry,
            wallet_repo=SqlAlchemyWalletRepository(self.session_factory),
            signing_key=get_settings().callback_signing_key,
            market_catalog=self.market_catalog(),
            # Display names for the product card's availability list; optional.
            location_repo=SqlAlchemyLocationRepository(self.session_factory),
            cloud_providers=self.hourly_cloud_providers(),
            cloud_resolver=self.hourly_cloud_resolver(),
        )

    def hourly_cloud_resolver(self) -> Any | None:
        """Provider-neutral (provider_key, account_id) -> cloud adapter dispatch.

        Built from the Leaseweb cloud account router so hourly image reads
        and creation resolve the exact credential that owns the offer.
        ``None`` means no hourly credential is configured (legacy dict
        fallback still applies in the services). No provider-name
        branching: the account id selects the adapter.
        """
        router = self.leaseweb_cloud_account_router
        if router is None:
            return None

        class _Resolver:
            def __init__(self, cloud_router: Any, fallback: dict[str, Any]) -> None:
                self._router = cloud_router
                self._fallback = dict(fallback)

            def adapter_for(
                self, provider_key: str, credential_account_id: str | None = None
            ) -> Any | None:
                if provider_key != "leaseweb":
                    return self._fallback.get(provider_key)
                try:
                    return self._router.client_for(credential_account_id)
                except Exception:
                    # Fail closed to the logical default only for legacy
                    # rows without an account; a pinned unknown account
                    # must not silently fall back to another credential.
                    if not (credential_account_id or "").strip():
                        providers = getattr(self._router, "providers", {}) or {}
                        if isinstance(providers, dict) and providers:
                            ordered = getattr(self._router, "new_cloud_clients", None)
                            if callable(ordered):
                                try:
                                    clients = ordered()
                                    if clients:
                                        return clients[0][1]
                                except Exception:
                                    pass
                            return next(iter(providers.values()))
                    return None

        return _Resolver(router, self.hourly_cloud_providers())

    def hourly_cloud_provider(self) -> Any | None:
        """Hourly cloud adapter for live image reads (None when unconfigured).

        Account-aware: the default is the first ACTIVE cloud account in
        deterministic (priority, id) order — never an arbitrary first
        configured key. Legacy single-credential deployments fall back to
        the shared settings constructor.
        """
        router = self.leaseweb_cloud_account_router
        if router is not None:
            try:
                clients = router.new_cloud_clients()
            except Exception:
                clients = ()
            if clients:
                return clients[0][1]
            providers = dict(getattr(router, "providers", {}) or {})
            if providers:
                return next(iter(providers.values()))
            return None
        from cloud_platform.providers.leaseweb.cloud import hourly_provider_from_settings

        return hourly_provider_from_settings(get_settings())

    def hourly_cloud_providers(self) -> dict[str, Any]:
        """Hourly adapters keyed by provider (the storefront image screens)."""
        provider = self.hourly_cloud_provider()
        return {"leaseweb": provider} if provider is not None else {}

    def capacity_republisher(self) -> Any | None:
        """Reacts to a NEW capacity refusal by refreshing future publication.

        Built from the same Leaseweb cloud account router the hourly service
        uses, so the alternate account is selected with the SAME read-only
        proof the catalog sync demands. ``None`` when no Cloud router is
        configured: the periodic catalog walk remains the only refresher.
        """
        router = self.leaseweb_cloud_account_router
        if router is None:
            return None
        from cloud_platform.modules.provider_capacity.repository import (
            SqlAlchemyAccountCapacityRepository,
        )
        from cloud_platform.providers.leaseweb.capacity_republish import (
            LeasewebCapacityRepublisher,
        )

        return LeasewebCapacityRepublisher(
            session_factory=self.session_factory,
            router=router,
            capacity_repo=SqlAlchemyAccountCapacityRepository(self.session_factory),
        )

    def leaseweb_capacity_reconciliation(self) -> Any | None:
        """Idempotent recovery of historical PC-2031 refusals (read-only).

        ``None`` when no Leaseweb Cloud scope exists, so the worker startup can
        simply skip it instead of inventing evidence.
        """
        router = self.leaseweb_cloud_account_router
        if router is None and not get_settings().leaseweb_api_key:
            return None
        from cloud_platform.modules.provider_capacity.reconciliation import (
            CapacityReconciliationService,
        )
        from cloud_platform.modules.provider_capacity.repository import (
            SqlAlchemyAccountCapacityRepository,
        )
        from cloud_platform.providers.leaseweb.capacity_evidence import (
            SqlAlchemyHistoricalCapacityEvidenceSource,
        )

        return CapacityReconciliationService(
            capacity_repo=SqlAlchemyAccountCapacityRepository(self.session_factory),
            evidence_source=SqlAlchemyHistoricalCapacityEvidenceSource(self.session_factory),
            ttl_seconds=get_settings().leaseweb_cloud_account_limit_ttl_seconds,
        )

    def hourly_cloud_service(self) -> Any:
        """The hourly instance creation command (no provider calls, no charge)."""
        from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository
        from cloud_platform.modules.hourly.service import HourlyCloudService
        from cloud_platform.modules.operations.repository import SqlAlchemyOperationRepository
        from cloud_platform.modules.pricing.repository import (
            SqlAlchemyServerPriceSnapshotRepository,
        )
        from cloud_platform.modules.pricing.service import ServerPriceSnapshotService
        from cloud_platform.modules.provider_accounts.repository import (
            SqlAlchemyProviderAccountRepository,
        )
        from cloud_platform.modules.provider_capacity.repository import (
            SqlAlchemyAccountCapacityRepository,
        )
        from cloud_platform.modules.wallet.repository import SqlAlchemyWalletRepository

        return HourlyCloudService(
            server_repo=SqlAlchemyServerRepository(self.session_factory),
            offers_repo=self.sellable_offer_repository(),
            account_repo=SqlAlchemyProviderAccountRepository(self.session_factory),
            wallet_repo=SqlAlchemyWalletRepository(self.session_factory),
            snapshot_service=ServerPriceSnapshotService(
                SqlAlchemyServerPriceSnapshotRepository(self.session_factory),
                _audit_repository(self.session_factory),
            ),
            operation_repo=SqlAlchemyOperationRepository(self.session_factory),
            audit_repo=_audit_repository(self.session_factory),
            cloud_providers=self.hourly_cloud_providers(),
            cloud_resolver=self.hourly_cloud_resolver(),
            # Pre-checkout capacity gate: an offer pinned to an account whose
            # provider instance limit was definitively refused is not sellable
            # until the signal expires — the catalog stops publishing through
            # it too, but this catches an offer published before the refusal.
            capacity_repo=SqlAlchemyAccountCapacityRepository(self.session_factory),
            capacity_ttl_seconds=get_settings().leaseweb_cloud_account_limit_ttl_seconds,
            # The moment a refusal is recorded, future-order publication is
            # refreshed so the storefront stops advertising the limited
            # account instead of waiting for the next catalog walk.
            capacity_republisher=self.capacity_republisher(),
            catalog_currency=get_settings().fx_catalog_pricing_currency,
            catalog_stale_limit_seconds=(get_settings().fx_frankfurter_catalog_max_stale_seconds),
            # Operator business feed: the hourly lifecycle cards ride the SAME
            # durable outbox as the monthly/payment flows (the worker owns
            # Telegram delivery, never this service). Disabled configuration
            # yields the null sink, so no financial path gains a dependency.
            event_sink=self.business_event_sink(),
            user_repo=self.user_repository(),
        )

    def order_worker(self, delivery_notifier: Any | None = None) -> Any:
        """The PENDING_SUBMIT -> provider order worker (LEASEWEB-MVP)."""
        from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository
        from cloud_platform.modules.operations.repository import SqlAlchemyOperationRepository
        from cloud_platform.modules.orders.service import OrderWorker
        from cloud_platform.modules.wallet.repository import (
            SqlAlchemyHoldRepository,
            SqlAlchemyWalletRepository,
        )

        return OrderWorker(
            server_repo=SqlAlchemyServerRepository(self.session_factory),
            offers_repo=self.sellable_offer_repository(),
            orders_repo=self.provider_order_repository(),
            operation_repo=SqlAlchemyOperationRepository(self.session_factory),
            wallet_repo=SqlAlchemyWalletRepository(self.session_factory),
            hold_repo=SqlAlchemyHoldRepository(self.session_factory),
            hold_service=self.hold_service(),
            ledger_repo=self.ledger_repository(),
            audit_repo=_audit_repository(self.session_factory),
            provider_registry=self.provider_registry,
            renewal_repo=self.renewal_repository(),
            delivery_notifier=delivery_notifier,
            event_sink=self.business_event_sink(),
            user_repo=self.user_repository(),
        )

    def order_reconciler(self, delivery_notifier: Any | None = None) -> Any:
        """The read-only order reconciler (LEASEWEB-MVP)."""
        from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository
        from cloud_platform.modules.operations.repository import SqlAlchemyOperationRepository
        from cloud_platform.modules.orders.service import OrderReconciler
        from cloud_platform.modules.wallet.repository import (
            SqlAlchemyHoldRepository,
            SqlAlchemyWalletRepository,
        )

        return OrderReconciler(
            server_repo=SqlAlchemyServerRepository(self.session_factory),
            offers_repo=self.sellable_offer_repository(),
            orders_repo=self.provider_order_repository(),
            operation_repo=SqlAlchemyOperationRepository(self.session_factory),
            renewal_repo=self.renewal_repository(),
            wallet_repo=SqlAlchemyWalletRepository(self.session_factory),
            hold_repo=SqlAlchemyHoldRepository(self.session_factory),
            hold_service=self.hold_service(),
            ledger_repo=self.ledger_repository(),
            audit_repo=_audit_repository(self.session_factory),
            provider_registry=self.provider_registry,
            delivery_notifier=delivery_notifier,
            event_sink=self.business_event_sink(),
            user_repo=self.user_repository(),
        )

    def order_recovery(self) -> Any:
        """READ-ONLY recovery of OUTCOME_UNKNOWN orders (LEASEWEB-MVP)."""
        from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository
        from cloud_platform.modules.operations.repository import SqlAlchemyOperationRepository
        from cloud_platform.modules.orders.service import OrderRecoveryService
        from cloud_platform.modules.wallet.repository import (
            SqlAlchemyHoldRepository,
            SqlAlchemyWalletRepository,
        )

        return OrderRecoveryService(
            server_repo=SqlAlchemyServerRepository(self.session_factory),
            offers_repo=self.sellable_offer_repository(),
            orders_repo=self.provider_order_repository(),
            operation_repo=SqlAlchemyOperationRepository(self.session_factory),
            wallet_repo=SqlAlchemyWalletRepository(self.session_factory),
            hold_repo=SqlAlchemyHoldRepository(self.session_factory),
            hold_service=self.hold_service(),
            audit_repo=_audit_repository(self.session_factory),
            provider_registry=self.provider_registry,
            event_sink=self.business_event_sink(),
            user_repo=self.user_repository(),
        )

    def order_manual_resolution(self) -> Any:
        """Operator-driven manual resolution of provider orders (LEASEWEB-MVP).

        ``retry_failed`` (definitive FAILED only), ``resolve_existing`` and
        ``resolve_not_created`` (ambiguous OUTCOME_UNKNOWN/NEEDS_REVIEW only)
        — all manual-only, audited, and NEVER POSTing.
        """
        from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository
        from cloud_platform.modules.operations.repository import SqlAlchemyOperationRepository
        from cloud_platform.modules.orders.service import OrderManualResolutionService
        from cloud_platform.modules.wallet.repository import (
            SqlAlchemyHoldRepository,
            SqlAlchemyWalletRepository,
        )

        return OrderManualResolutionService(
            server_repo=SqlAlchemyServerRepository(self.session_factory),
            offers_repo=self.sellable_offer_repository(),
            orders_repo=self.provider_order_repository(),
            operation_repo=SqlAlchemyOperationRepository(self.session_factory),
            renewal_repo=self.renewal_repository(),
            wallet_repo=SqlAlchemyWalletRepository(self.session_factory),
            hold_repo=SqlAlchemyHoldRepository(self.session_factory),
            hold_service=self.hold_service(),
            ledger_repo=self.ledger_repository(),
            audit_repo=_audit_repository(self.session_factory),
            provider_registry=self.provider_registry,
            event_sink=self.business_event_sink(),
            user_repo=self.user_repository(),
        )

    def renewal_checker(
        self, user_notifier: Any | None = None, admin_notifier: Any | None = None
    ) -> Any:
        """The daily renewal checker (LEASEWEB-MVP)."""
        from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository
        from cloud_platform.modules.renewals.service import RenewalChecker
        from cloud_platform.modules.wallet.repository import (
            SqlAlchemyHoldRepository,
            SqlAlchemyWalletRepository,
        )

        return RenewalChecker(
            renewals_repo=self.renewal_repository(),
            notification_repo=self.renewal_notification_repository(),
            server_repo=SqlAlchemyServerRepository(self.session_factory),
            wallet_repo=SqlAlchemyWalletRepository(self.session_factory),
            hold_repo=SqlAlchemyHoldRepository(self.session_factory),
            hold_service=self.hold_service(),
            audit_repo=_audit_repository(self.session_factory),
            user_notifier=user_notifier,
            admin_notifier=admin_notifier,
            # Commercial notices ride the same durable outbox as every other
            # business event, so the operator channel and the money never
            # depend on each other's availability.
            event_sink=self.business_event_sink(),
        )

    def offer_admin_service(self) -> Any:
        """Application service for operator-owned offer prices/visibility."""
        from cloud_platform.modules.offers.service import OfferAdminService

        return OfferAdminService(
            self.sellable_offer_repository(),
            _audit_repository(self.session_factory),
            catalog_currency=get_settings().fx_catalog_pricing_currency,
        )

    def wallet_admin_service(self) -> Any:
        """Admin wallet adjustments with mandatory audit trail."""
        from cloud_platform.modules.wallet.repository import SqlAlchemyWalletRepository
        from cloud_platform.modules.wallet.service import WalletAdminService

        return WalletAdminService(
            SqlAlchemyWalletRepository(self.session_factory),
            self.ledger_repository(),
            _audit_repository(self.session_factory),
            event_sink=self.business_event_sink(),
            user_repo=self.user_repository(),
        )

    # -- storefront markets ------------------------------------------------

    def market_catalog(self) -> Any:
        """Provider metadata for the Iran/Foreign storefront (from the TOML)."""
        from cloud_platform.modules.markets.domain import ProviderCatalog

        settings = get_settings()
        return ProviderCatalog(
            markets=settings.provider_markets,
            display_names=settings.provider_display_names,
            enabled=settings.providers_enabled,
            families=settings.provider_families,
        )

    def provider_market(self, provider_key: str) -> str:
        """The configured market of a provider (``iran``/``foreign``/``""``)."""
        market = self.market_catalog().market_of(provider_key)
        return market.value if market is not None else ""

    # -- business log (private operator Telegram channel) -------------------

    def business_log_policy(self) -> Any:
        """The operator logger-channel policy (TOML ``[telegram.logger]``)."""
        from cloud_platform.modules.businesslog.domain import BusinessLogPolicy

        return BusinessLogPolicy.from_settings(get_settings())

    def business_log_repository(self) -> Any:
        """Durable business-log outbox storage."""
        from cloud_platform.modules.businesslog.repository import (
            SqlAlchemyBusinessLogRepository,
        )

        return SqlAlchemyBusinessLogRepository(self.session_factory)

    def business_event_sink(self) -> Any:
        """The business-event sink to inject into application services.

        Disabled configuration yields the null sink, so call sites stay
        unconditional and no financial path gains a Telegram dependency.
        """
        from cloud_platform.modules.businesslog.domain import (
            NullBusinessEventSink,
            OutboxBusinessEventSink,
        )

        policy = self.business_log_policy()
        if not policy.active:
            return NullBusinessEventSink()
        return OutboxBusinessEventSink(self.business_log_repository(), policy)

    def business_log_dispatcher(self, bot: Any) -> Any:
        """The delivery worker for the operator channel (worker process only)."""
        from cloud_platform.modules.businesslog.domain import BusinessLogDispatcher
        from cloud_platform.modules.businesslog.telegram import TelegramBusinessLogChannel

        policy = self.business_log_policy()
        return BusinessLogDispatcher(
            self.business_log_repository(),
            TelegramBusinessLogChannel(bot, policy.chat_id),
            policy,
        )

    def payment_gateways(self) -> dict[str, Any]:
        """Every configured + enabled payment gateway, keyed by gateway key.

        Built fresh per call (each adapter owns an HTTP client); the caller
        owns closing them (see :meth:`aclose_gateways`). Construction lives
        here — in infrastructure — so domain/application code never branches
        on gateway keys.
        """
        from cloud_platform.providers.tetraminator.client import TetraminatorGateway
        from cloud_platform.providers.zarinpal.client import ZarinPalGateway

        settings = get_settings()
        gateways: dict[str, Any] = {}
        if settings.zarinpal_enabled and settings.zarinpal_merchant_id:
            gateways[ZarinPalGateway.key] = ZarinPalGateway(
                merchant_id=settings.zarinpal_merchant_id,
                base_url=settings.zarinpal_base_url,
                sandbox=settings.zarinpal_sandbox,
                callback_url=settings.zarinpal_callback_url,
            )
        if settings.tetraminator_enabled and settings.tetraminator_api_key:
            gateways[TetraminatorGateway.key] = TetraminatorGateway(
                api_key=settings.tetraminator_api_key,
                base_url=settings.tetraminator_base_url,
                callback_url=settings.tetraminator_callback_url,
                timeout_seconds=settings.tetraminator_timeout_seconds,
                require_https_callback=(settings.app_env or "").strip().lower() == "production",
            )
        return gateways

    @staticmethod
    async def aclose_gateways(gateways: dict[str, Any]) -> None:
        """Best-effort close of gateway HTTP clients (never raises)."""
        for gateway in gateways.values():
            close = getattr(gateway, "close", None)
            if not callable(close):
                continue
            try:
                await close()
            except Exception:
                logger.warning(
                    "gateway client close failed for %s",
                    getattr(gateway, "key", type(gateway).__name__),
                )

    def payment_gateway(self, key: str | None = None) -> Any | None:
        """Select one configured gateway: explicit key, or the single one.

        Returns None when nothing (or nothing unambiguous) is configured.
        """
        gateways = self.payment_gateways()
        if key is not None:
            return gateways.get(key)
        if len(gateways) == 1:
            return next(iter(gateways.values()))
        return None

    def wallet_recharge_service(
        self,
        gateway: Any | None = None,
        gateways: dict[str, Any] | None = None,
        fx_resolver: Any | None = None,
    ) -> Any:
        """Creates pending top-up sessions (and logs ``recharge.created``).

        Pass one gateway (legacy single-gateway call sites) or rely on the
        configured collection: with no argument every enabled gateway is
        offered and the customer picks among the compatible ones.

        ``gateways`` is the reuse path for a process that ALREADY built the
        collection: the very same adapter instances are handed over, so the
        bot never opens two sets of HTTP clients (one of which nobody would
        ever close). The gateway collection belongs to the process owner — the
        caller that built it closes it — never to this service.

        ``fx_resolver`` is the same reuse path for currency conversion: the
        process owner builds ONE resolver (one AbanTether + one cache client)
        and shares it; when omitted a resolver is built on demand and the
        service never closes it (the process owner still owns its lifecycle
        via :meth:`aclose_fx`).
        """
        from cloud_platform.modules.payments.recharge import WalletRechargeService

        if gateways is None:
            gateways = self.payment_gateways()
        if fx_resolver is None:
            fx_resolver = self.fx_resolver_or_none()
        return WalletRechargeService(
            payments_repo=SqlAlchemyPaymentSessionRepository(self.session_factory),
            gateway=gateway,
            gateways=gateways,
            event_sink=self.business_event_sink(),
            user_repo=self.user_repository(),
            fx_resolver=fx_resolver,
        )

    def fx_config(self) -> Any:
        """Domestic/payment FX policy from server-owned configuration."""
        from cloud_platform.modules.fx.service import FxConfig

        settings = get_settings()
        return FxConfig(
            enabled=(
                getattr(settings, "fx_domestic_enabled", settings.fx_enabled)
                and settings.fx_enabled
            ),
            provider=settings.fx_domestic_provider or settings.fx_provider,
            default_display_currency=settings.fx_default_display_currency,
            quote_ttl_seconds=settings.fx_quote_ttl_seconds,
            max_stale_seconds=settings.fx_max_stale_seconds,
            charge_max_stale_seconds=settings.fx_charge_max_stale_seconds,
            request_timeout_seconds=int(settings.fx_request_timeout_seconds),
            allow_usdt_proxy_for_display=settings.fx_allow_usdt_proxy_for_display,
            allow_usdt_proxy_for_settlement=settings.fx_allow_usdt_proxy_for_settlement,
        )

    def global_fx_config(self) -> Any:
        """Global provider-cost normalization policy."""
        from cloud_platform.modules.fx.service import GlobalFiatFxConfig

        settings = get_settings()
        return GlobalFiatFxConfig(
            enabled=(
                getattr(settings, "fx_global_enabled", settings.fx_enabled) and settings.fx_enabled
            ),
            catalog_currency=settings.fx_catalog_pricing_currency,
            quote_ttl_seconds=settings.fx_frankfurter_quote_ttl_seconds,
            max_stale_seconds=settings.fx_frankfurter_max_stale_seconds,
            catalog_max_stale_seconds=settings.fx_frankfurter_catalog_max_stale_seconds,
        )

    @staticmethod
    def _fx_cache_backend() -> tuple[str, str]:
        settings = get_settings()
        backend = "redis" if (settings.app_env or "").strip().lower() == "production" else "memory"
        return backend, settings.redis_url

    def global_fx_resolver(self) -> Any:
        """Return the process-owned global resolver, preserving outage memo."""
        from cloud_platform.modules.fx.cache import build_fx_cache
        from cloud_platform.modules.fx.service import GlobalFiatFxResolver
        from cloud_platform.providers.frankfurter_fx.client import FrankfurterFxClient

        if self._global_fx_resolver is not None:
            return self._global_fx_resolver
        settings = get_settings()
        source = FrankfurterFxClient(
            base_url=settings.fx_frankfurter_base_url,
            timeout_seconds=settings.fx_frankfurter_request_timeout_seconds,
            ttl_seconds=settings.fx_frankfurter_quote_ttl_seconds,
        )
        backend, redis_url = self._fx_cache_backend()
        config = self.global_fx_config()
        cache = build_fx_cache(
            backend=backend,
            redis_url=redis_url,
            retention_seconds=config.max_stale_seconds,
        )
        resolver = GlobalFiatFxResolver(source=source, cache=cache, config=config)
        object.__setattr__(resolver, "_container_owned", True)
        object.__setattr__(self, "_global_fx_resolver", resolver)
        return resolver

    def fx_resolver(self) -> Any:
        """Return the process-owned domestic/payment resolver."""
        from cloud_platform.modules.fx.cache import build_fx_cache
        from cloud_platform.modules.fx.service import FxResolver
        from cloud_platform.providers.abantether_fx.client import AbanTetherFxClient

        if self._domestic_fx_resolver is not None:
            return self._domestic_fx_resolver
        settings = get_settings()
        domestic_source = AbanTetherFxClient(
            base_url=settings.fx_abantether_base_url,
            timeout_seconds=int(settings.fx_request_timeout_seconds),
            ttl_seconds=settings.fx_quote_ttl_seconds,
            eur_symbol=settings.fx_abantether_eur_symbol,
            usd_proxy_symbol=settings.fx_abantether_usd_proxy_symbol,
        )
        backend, redis_url = self._fx_cache_backend()
        domestic_config = self.fx_config()
        domestic_cache = build_fx_cache(
            backend=backend,
            redis_url=redis_url,
            retention_seconds=domestic_config.max_stale_seconds,
        )
        domestic = FxResolver(
            source=domestic_source,
            cache=domestic_cache,
            config=domestic_config,
        )
        object.__setattr__(domestic, "_container_owned", True)
        object.__setattr__(self, "_domestic_fx_resolver", domestic)
        return domestic

    def global_fx_resolver_or_none(self) -> Any | None:
        """Global FX resolver for catalog sync, or None when disabled."""
        try:
            settings = get_settings()
            if not (
                settings.fx_enabled and getattr(settings, "fx_global_enabled", settings.fx_enabled)
            ):
                return None
            return self.global_fx_resolver()
        except Exception:
            logger.warning("global FX resolver unavailable; continuing without FX", exc_info=True)
            return None

    def fx_resolver_or_none(self) -> Any | None:
        """Routed FX resolver for payment/recharge callers, or None when disabled."""
        try:
            settings = get_settings()
            if not (
                settings.fx_enabled
                and getattr(settings, "fx_domestic_enabled", settings.fx_enabled)
            ):
                return None
            return self.fx_resolver()
        except Exception:
            logger.warning("FX resolver unavailable; continuing without FX", exc_info=True)
            return None

    @staticmethod
    async def aclose_fx(resolver: Any | None) -> None:
        """Best-effort close of the FX resolver (never raises)."""
        if resolver is None or getattr(resolver, "_container_owned", False):
            return
        try:
            await resolver.close()
        except Exception:
            logger.warning("fx resolver close failed")

    def wallet_history_service(self) -> Any:
        """User wallet balance + ledger history."""
        from cloud_platform.modules.wallet.repository import SqlAlchemyWalletRepository
        from cloud_platform.modules.wallet.service import WalletHistoryService

        return WalletHistoryService(
            SqlAlchemyWalletRepository(self.session_factory),
            self.ledger_repository(),
        )

    def server_repository(self) -> Any:
        """Cloud server persistence."""
        from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository

        return SqlAlchemyServerRepository(self.session_factory)

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

    def session_store(self) -> BotSessionStore:
        """The Telegram transient-state backend (Redis in production).

        Callback references, pending confirmations and text prompts live here
        so they survive a bot restart and mean the same thing on every replica.
        """
        if self.bot_session_store is not None:
            return self.bot_session_store
        from cloud_platform.core.session_store import build_session_store

        settings = get_settings()
        return build_session_store(
            backend=settings.telegram_sessions_backend,
            prefix=settings.telegram_sessions_namespace,
            redis_url=settings.redis_url,
        )

    def server_sessions(self) -> Any:
        """Shared presentation state for the My Servers flow."""
        from cloud_platform.bot.sessions import ServerSessions

        settings = get_settings()
        return ServerSessions(
            self.session_store(),
            reference_ttl_seconds=settings.telegram_sessions_reference_ttl_seconds,
            prompt_ttl_seconds=settings.telegram_sessions_prompt_ttl_seconds,
        )

    def confirmation_verifier(self) -> Any:
        """One-time confirmation tokens for destructive customer operations.

        Consumption is atomic in the SHARED store, so a token survives a bot
        restart and two replicas cannot both consume it. When the store cannot
        be reached the verifier reports ``UNAVAILABLE`` and the caller refuses
        the operation (fail closed) rather than falling back to local state.
        """
        from cloud_platform.modules.servers.confirmations import (
            ConfirmationVerifier,
            SharedConfirmationStore,
        )

        settings = get_settings()
        policy = self.server_management_policy()
        return ConfirmationVerifier(
            settings.callback_signing_key or "unset-callback-key",
            store=SharedConfirmationStore(
                self.session_store(),
                replay_ttl_seconds=settings.telegram_sessions_replay_ttl_seconds,
            ),
            # The operator's server-management TTL stays authoritative for the
            # token itself; the telegram session TTL only bounds the replay
            # marker, so an unrelated setting can never shorten a confirmation.
            ttl_seconds=policy.confirmation_ttl_seconds,
        )

    def server_management_policy(self) -> Any:
        """The customer capability policy for the deployment."""
        from cloud_platform.modules.servers.policies import ServerManagementPolicy

        return ServerManagementPolicy.from_settings(get_settings())

    def server_management_service(self) -> Any:
        """Customer-facing, ownership-safe server management (Telegram).

        Power keeps flowing through :class:`PowerCommandService` (the existing
        operation ledger), while every other provider call goes through the
        provider-neutral VPS ports.
        """
        from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository
        from cloud_platform.modules.servers.service import ServerManagementService

        return ServerManagementService(
            servers=SqlAlchemyServerRepository(self.session_factory),
            registry=self.provider_registry,
            policy=self.server_management_policy(),
            confirmations=self.confirmation_verifier(),
            audit_repo=_audit_repository(self.session_factory),
            power=self.power_command_service(),
            event_sink=self.business_event_sink(),
            # Commercial status + manual renewal come from the SAME checker the
            # worker runs, so the customer's "renew now" and the automatic pass
            # can never disagree about the price or the idempotency key.
            renewal_collector=self.renewal_checker(),
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Create a new database session."""
        async with self.session_factory() as session:
            yield session

    async def initialize(self) -> None:
        """Register provider adapters (idempotent at the lifecycle level).

        A second call is a no-op: the adapters are already registered and
        the registry would reject them as duplicates. The initialized flag
        is set only after registration fully succeeds, so a failed
        initialize leaves the container uninitialized and retryable.
        """
        # Database tables are created via Alembic migrations
        # Here we just register providers
        if self._initialized:
            return
        self._register_providers()
        # Frozen dataclass: bypass the frozen __setattr__ for lifecycle state.
        object.__setattr__(self, "_initialized", True)

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
        # LEASEWEB-MULTIACCOUNT: ONE logical provider key (``leaseweb``) served
        # by N credential accounts. Each account registers as a ROUTE under that
        # key, and each keeps its OWN transport/holder/throttle, so a request
        # for one account can never carry another's X-LSW-Auth header.
        router = self.leaseweb_account_router
        if router is not None:
            if holders is not None:
                for account_id, holder in router.credential_holders.items():
                    holders.register("leaseweb", holder, account_id)
            registered: set[str] = set()
            # Pass 1: accounts that may take NEW orders, in deterministic
            # (priority, account_id) order — the first becomes the logical
            # default adapter used by credential-agnostic catalog reads.
            for account_id, provider in router.new_order_clients():
                self.provider_registry.register_route("leaseweb", account_id, provider)
                registered.add(account_id)
            # Pass 2: draining accounts still address the resources they own,
            # so they must stay resolvable even though they take no new orders.
            for account_id, provider in router.ordered_providers.items():
                if account_id in registered:
                    continue
                self.provider_registry.register_route("leaseweb", account_id, provider)
                registered.add(account_id)
            self.provider_registry.register_account_views("leaseweb", router.views())
        elif settings.leaseweb_api_key:
            leaseweb_holder = CredentialHolder(settings.leaseweb_api_key)
            if holders is not None:
                holders.register("leaseweb", leaseweb_holder)
            if self.leaseweb_ordering_provider is not None:
                # LEASEWEB-MVP: the ordering adapter IS the runtime provider
                # (monthly products); the public-cloud adapter remains only
                # for the legacy hourly catalog sync via its own instance.
                self.leaseweb_ordering_provider._credential_source = leaseweb_holder
                self.provider_registry.register(self.leaseweb_ordering_provider)
            else:
                leaseweb = LeaseWebProvider(
                    api_key=settings.leaseweb_api_key,
                    base_url=settings.leaseweb_api_base_url,
                    credential_source=leaseweb_holder,
                )
                self.provider_registry.register(leaseweb)

    async def close(self) -> None:
        """Close all process-owned transports, providers, and database state.

        Catalog syncers create a few adapters that are not provider-registry
        routes (legacy catalog syncers and account routers).  They are owned
        by this container too; keeping that ownership explicit prevents a
        scheduled sync or a container that was never initialized from leaking
        HTTP clients.  The operation is idempotent for callers that already
        closed a manually-used syncer before calling ``container.close()``.
        """
        if self._closed:
            return
        object.__setattr__(self, "_closing", True)
        seen: set[int] = set(self._closed_resource_ids)
        close_failed = False
        closed_resource_ids = self._closed_resource_ids

        async def close_resource(resource: Any) -> None:
            nonlocal close_failed
            if resource is None or id(resource) in seen:
                return
            seen.add(id(resource))
            closer = getattr(resource, "aclose", None) or getattr(resource, "close", None)
            closed = False
            if callable(closer):
                try:
                    await closer()
                    closed = True
                except Exception:
                    close_failed = True
                    logger.warning("owned resource close failed", exc_info=True)
            else:
                # A resource without a closer is nevertheless fully processed;
                # remembering it keeps repeated shutdown calls cheap.
                closed = True
            if closed:
                closed_resource_ids.add(id(resource))
                # Routers close their account adapters internally.  Only mark
                # children after a successful router close; otherwise the registry
                # pass remains available to retry those routes.  A mapping-only
                # shim without its own closer must leave children for the
                # registry pass.
                if callable(closer):
                    for attribute in ("providers", "ordered_providers"):
                        mapping = getattr(resource, attribute, None)
                        if isinstance(mapping, Mapping):
                            for child in mapping.values():
                                child_id = id(child)
                                seen.add(child_id)
                                closed_resource_ids.add(child_id)

        # Close syncer-only transports first.  A manually closed Hetzner
        # syncer may be encountered here as well; httpx close is idempotent,
        # and the object-identity guard prevents duplicate closure for the
        # normal container path.
        for resource in self.owned_resources:
            await close_resource(resource)
        await close_resource(self._global_fx_resolver)
        await close_resource(self._domestic_fx_resolver)
        # Account routers may be present on directly constructed test
        # containers without being listed in ``owned_resources``.
        await close_resource(self.leaseweb_account_router)
        await close_resource(self.leaseweb_cloud_account_router)

        if self.engine is not None and id(self.engine) not in seen:
            engine_id = id(self.engine)
            seen.add(engine_id)
            try:
                await self.engine.dispose()
                closed_resource_ids.add(engine_id)
            except Exception:
                close_failed = True
                logger.warning("database engine dispose failed", exc_info=True)

        # Close provider connections. A multi-account provider has one adapter
        # per credential account, each with its own HTTP client, so the routes
        # must be closed too — the logical map only holds the default one.
        candidates = list(self.provider_registry._providers.values())
        for routes in self.provider_registry._routes.values():
            candidates.extend(routes.values())
        # Hourly cloud adapters are not in the provider registry (they serve a
        # distinct product); their router is normally in ``owned_resources``,
        # but retain this fallback for manually constructed containers.
        cloud_router = self.leaseweb_cloud_account_router
        if cloud_router is not None:
            providers = getattr(cloud_router, "providers", None)
            if isinstance(providers, Mapping):
                candidates.extend(providers.values())
        for provider in candidates:
            provider_id = id(provider)
            if provider_id in seen:
                continue
            seen.add(provider_id)
            closer = getattr(provider, "close", None)
            if callable(closer):
                try:
                    await closer()
                    closed_resource_ids.add(provider_id)
                except Exception:
                    close_failed = True
                    logger.warning("provider client close failed", exc_info=True)
            else:
                closed_resource_ids.add(provider_id)

        # Release the shared Telegram session/confirmation connection too, so a
        # graceful shutdown leaves no half-open Redis client behind.
        store = self.bot_session_store
        client = getattr(store, "client", None)
        if client is not None and id(client) not in seen:
            client_id = id(client)
            seen.add(client_id)
            from cloud_platform.core.redis import close_redis_client

            try:
                await close_redis_client(client)
                closed_resource_ids.add(client_id)
            except Exception:
                close_failed = True
                logger.warning("bot session store close failed", exc_info=True)

        if not close_failed:
            object.__setattr__(self, "_closed", True)
            object.__setattr__(self, "_closing", False)
        else:
            # Keep the reference globally reachable for an explicit retry, but
            # make it unusable for new callers until cleanup succeeds.
            object.__setattr__(self, "_closing", True)


_container: Container | None = None
_container_lock = asyncio.Lock()


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
    # Syncer-only transports are not registry routes, but they are still owned
    # by this process.  Keep explicit ownership so normal shutdown and the
    # manual catalog scripts release them as well.
    owned_resources: list[Any] = []

    # Create Hetzner syncer if token available
    hetzner_syncer = None
    if settings.hetzner_api_token:
        hetzner_syncer = HetznerCatalogSyncer(
            session_factory=session_factory,
            token=settings.hetzner_api_token,
            base_url=settings.hetzner_api_base_url,
        )
        owned_resources.append(hetzner_syncer)

    # Create ArvanCloud syncer(s) if key available - one per configured region
    arvancloud_syncers: list[ArvanCloudCatalogSyncer] = []
    if settings.arvancloud_api_key:
        arvancloud_provider = ArvanCloudProvider(
            api_key=settings.arvancloud_api_key,
            base_url=settings.arvancloud_api_base_url,
            region=settings.arvancloud_region,
        )
        # All regional syncers share this one transport; register it once.
        owned_resources.append(arvancloud_provider)
        for region in _configured_regions(settings.arvancloud_region):
            arvancloud_syncers.append(
                ArvanCloudCatalogSyncer(
                    session_factory=session_factory,
                    provider=arvancloud_provider,
                    region=region,
                )
            )

    # LeaseWeb syncer (EU second provider next to Hetzner)
    leaseweb_syncer = None
    if settings.leaseweb_api_key:
        leaseweb_legacy_provider = LeaseWebProvider(
            api_key=settings.leaseweb_api_key,
            base_url=settings.leaseweb_api_base_url,
        )
        owned_resources.append(leaseweb_legacy_provider)
        leaseweb_syncer = LeaseWebCatalogSyncer(
            session_factory=session_factory,
            provider=leaseweb_legacy_provider,
        )

    # LEASEWEB-MVP: ordering-VPS provider + catalog syncer (monthly products).
    # LEASEWEB-MULTIACCOUNT: when credential accounts are configured, EVERY
    # account gets its own adapter/transport and the syncer runs per account,
    # merging the observations into ONE customer-facing catalog. The logical
    # "leaseweb" adapter stays the first account in (priority, id) order.
    leaseweb_ordering_provider = None
    leaseweb_ordering_syncer = None
    leaseweb_account_router = None
    if settings.leaseweb_accounts:
        from cloud_platform.providers.leaseweb.accounts import (
            build_leaseweb_account_router,
        )

        leaseweb_account_router = build_leaseweb_account_router(settings)
        assert leaseweb_account_router is not None  # accounts are configured
        # The router owns every per-account ordering transport, including when
        # the container is never initialized and no registry routes exist.
        owned_resources.append(leaseweb_account_router)
        new_order_clients = leaseweb_account_router.new_order_clients()
        ordered = leaseweb_account_router.ordered_providers
        if new_order_clients:
            leaseweb_ordering_provider = new_order_clients[0][1]
        elif ordered:
            # Every account is draining: the provider can still manage what it
            # owns but takes no new orders.
            leaseweb_ordering_provider = next(iter(ordered.values()))
        # The syncer aggregates accounts in deterministic (priority, id) order
        # and records each account's lifecycle so draining accounts stop taking
        # new orders without losing the locations they already serve.
        leaseweb_ordering_syncer = LeaseWebOrderingCatalogSyncer(
            session_factory=session_factory,
            accounts=ordered,
            account_priorities=leaseweb_account_router.priorities,
            account_states=leaseweb_account_router.account_states,
        )
    elif settings.leaseweb_api_key:
        from cloud_platform.providers.leaseweb.ordering_sync import ordering_provider_from_settings

        leaseweb_ordering_provider = ordering_provider_from_settings(settings)
        # In the single-credential form there is no router to own this
        # adapter, so the container owns it explicitly.
        owned_resources.append(leaseweb_ordering_provider)
        leaseweb_ordering_syncer = LeaseWebOrderingCatalogSyncer(
            session_factory=session_factory,
            provider=leaseweb_ordering_provider,
        )

    # Hourly Public Cloud is credential/account scoped like ordering: every
    # ACTIVE account gets its own adapter and the sync/creation paths
    # resolve the exact account that owns each region/type. Never the
    # first configured key by accident — the router owns discovery.
    leaseweb_cloud_account_router = None
    try:
        from cloud_platform.providers.leaseweb.cloud_accounts import (
            build_cloud_account_router,
        )

        leaseweb_cloud_account_router = build_cloud_account_router(settings)
        if leaseweb_cloud_account_router is not None:
            # The hourly router is not registered as a normal cloud route;
            # retain explicit ownership for both initialized and uninitialized
            # containers.
            owned_resources.append(leaseweb_cloud_account_router)
    except Exception:
        leaseweb_cloud_account_router = None

    # M10-008: runtime-rotatable provider credentials - one holder per
    # configured provider, plus the verify-then-swap rotation service.
    holders = _CredentialHolderRegistry()
    container = Container(
        engine=engine,
        session_factory=session_factory,
        provider_registry=registry,
        provider_allocator=allocator,
        hetzner_syncer=hetzner_syncer,
        leaseweb_syncer=leaseweb_syncer,
        arvancloud_syncers=tuple(arvancloud_syncers),
        leaseweb_ordering_syncer=leaseweb_ordering_syncer,
        leaseweb_ordering_provider=leaseweb_ordering_provider,
        leaseweb_account_router=leaseweb_account_router,
        leaseweb_cloud_account_router=leaseweb_cloud_account_router,
        owned_resources=tuple(owned_resources),
        credential_holders=holders,
        credential_rotation_service=CredentialRotationService(
            holders,
            registry,
            _audit_repository(session_factory),
        ),
        bot_session_store=build_session_store(
            backend=settings.telegram_sessions_backend,
            prefix=settings.telegram_sessions_namespace,
            redis_url=settings.redis_url,
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

    The returned container is fully initialized (providers registered).
    Initialization and shutdown share a single-flight lock so concurrent
    first callers cannot create competing engines.
    """
    global _container
    async with _container_lock:
        if _container is not None:
            if _container._closing:
                raise RuntimeError("application container is currently closing")
            if _container._closed:
                _container = None
            else:
                return _container
        candidate = create_container()
        try:
            await candidate.initialize()
        except BaseException:
            # ``create_container`` may already have opened syncer/router
            # transports before registration fails.  Do not strand them when
            # process startup is retried.
            await candidate.close()
            raise
        _container = candidate
        return _container


async def close_container() -> None:
    """Close the global container and release resources."""
    global _container
    async with _container_lock:
        if _container is not None:
            container = _container
            await container.close()
            if container._closed:
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
        event_sink=container.business_event_sink(),
        user_repo=container.user_repository(),
    )
