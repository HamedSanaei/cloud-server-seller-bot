"""Application dependency container and factories.

This module provides the central dependency container that wires together
all application components (database, providers, services) for the API,
worker, and bot entry points.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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
    leaseweb_syncer: LeaseWebCatalogSyncer | None = None
    arvancloud_syncers: tuple[ArvanCloudCatalogSyncer, ...] = ()
    leaseweb_ordering_syncer: LeaseWebOrderingCatalogSyncer | None = None
    leaseweb_ordering_provider: LeaseWebOrderingProvider | None = None
    credential_holders: _CredentialHolderRegistry | None = None
    credential_rotation_service: CredentialRotationService | None = None
    # PROD-HARDENING §2: the process-wide Telegram transient-state backend.
    # Selected once in :func:`create_container` from ``[telegram.sessions]``
    # (Redis in production, in-memory for tests/development). Containers
    # built directly (tests) leave it ``None`` and get a lazily built one.
    bot_session_store: BotSessionStore | None = None
    # Lifecycle guard: :meth:`initialize` registers provider adapters, and
    # the registry rejects duplicates. The flag makes a second initialize a
    # no-op instead of a double registration; it is set only after a fully
    # successful registration, so a failed initialize can be retried.
    # Initialized once per instance: after :meth:`close`, build a new
    # container (what :func:`get_container`/:func:`close_container` do)
    # rather than re-initializing a closed one.
    _initialized: bool = field(default=False, init=False, repr=False, compare=False)

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

        return SqlAlchemySellableOfferRepository(self.session_factory)

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
        )

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

    def wallet_recharge_service(self, gateway: Any | None = None) -> Any:
        """Creates pending top-up sessions (and logs ``recharge.created``).

        Pass one gateway (legacy single-gateway call sites) or rely on the
        configured collection: with no argument every enabled gateway is
        offered and the customer picks among the compatible ones.
        """
        from cloud_platform.modules.payments.recharge import WalletRechargeService

        gateways = self.payment_gateways()
        return WalletRechargeService(
            payments_repo=SqlAlchemyPaymentSessionRepository(self.session_factory),
            gateway=gateway,
            gateways=gateways,
            event_sink=self.business_event_sink(),
            user_repo=self.user_repository(),
        )

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
        if settings.leaseweb_api_key:
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
        """Close all resources."""
        # Close provider connections
        for provider in self.provider_registry._providers.values():
            if hasattr(provider, "close"):
                await provider.close()
        # Release the shared Telegram session/confirmation connection too, so a
        # graceful shutdown leaves no half-open Redis client behind.
        store = self.bot_session_store
        client = getattr(store, "client", None)
        if client is not None:
            from cloud_platform.core.redis import close_redis_client

            await close_redis_client(client)


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

    # LeaseWeb syncer (EU second provider next to Hetzner)
    leaseweb_syncer = None
    if settings.leaseweb_api_key:
        leaseweb_syncer = LeaseWebCatalogSyncer(
            session_factory=session_factory,
            provider=LeaseWebProvider(
                api_key=settings.leaseweb_api_key,
                base_url=settings.leaseweb_api_base_url,
            ),
        )

    # LEASEWEB-MVP: ordering-VPS provider + catalog syncer (monthly
    # products). The ordering provider is the runtime "leaseweb" adapter.
    leaseweb_ordering_provider = None
    leaseweb_ordering_syncer = None
    if settings.leaseweb_api_key:
        from cloud_platform.providers.leaseweb.ordering_sync import ordering_provider_from_settings

        leaseweb_ordering_provider = ordering_provider_from_settings(settings)
        leaseweb_ordering_syncer = LeaseWebOrderingCatalogSyncer(
            session_factory=session_factory,
            provider=leaseweb_ordering_provider,
        )

    # M10-008: runtime-rotatable provider credentials - one holder per
    # configured provider, plus the verify-then-swap rotation service.
    holders = _CredentialHolderRegistry()
    container = Container(
        session_factory=session_factory,
        provider_registry=registry,
        provider_allocator=allocator,
        hetzner_syncer=hetzner_syncer,
        leaseweb_syncer=leaseweb_syncer,
        arvancloud_syncers=tuple(arvancloud_syncers),
        leaseweb_ordering_syncer=leaseweb_ordering_syncer,
        leaseweb_ordering_provider=leaseweb_ordering_provider,
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
