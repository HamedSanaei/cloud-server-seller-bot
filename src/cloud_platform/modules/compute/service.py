"""Create-server application command (M07-001).

Validates user, offer and balance, then persists the intent: a ``REQUESTED``
server row pinned to the exact catalog offer and a unique idempotency key,
an immutable price snapshot (the single source of truth for billing), and a
wallet hold reserving the first quantum of funds. The command never calls a
provider — provisioning is the worker's job (M07-002).

Authorization/ownership is enforced here in the application layer: the
requesting user must be active, and the idempotency key makes retries
replay the original outcome instead of creating a second server or a second
hold.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol
from uuid import UUID, uuid4

from cloud_platform.modules.audit.domain import ActorType, AuditRepository
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.catalog.domain import (
    CatalogRepository,
    OfferNotFoundError,
    OfferRef,
)
from cloud_platform.modules.compute.domain import (
    CloudServer,
    CostCircuitBreaker,
    CostLimit,
    CostLimitRepository,
    CostLimitScopeKind,
    CostLimitTrigger,
    MaintenanceBlock,
    MaintenanceScope,
    MaintenanceSwitchRepository,
    QuotaPolicy,
    ServerCreateError,
    ServerCreateIntent,
    ServerLifecycleState,
    ServerRepository,
)
from cloud_platform.modules.pricing.domain import (
    OfferCost,
    SellingPrice,
    ServerPriceSnapshot,
)
from cloud_platform.modules.pricing.service import (
    PriceBookService,
    ServerPriceSnapshotService,
)
from cloud_platform.modules.provider_accounts.domain import (
    NoProviderAccountError,
    ProviderAccountRepository,
)
from cloud_platform.modules.users.domain import (
    Permission,
    PermissionChecker,
    User,
    UserStatus,
)
from cloud_platform.modules.wallet.domain import (
    Hold,
    HoldRepository,
    WalletRepository,
)
from cloud_platform.observability.metrics import metrics

if TYPE_CHECKING:
    from cloud_platform.modules.billing.service import AccrualPeriodRepository

logger = logging.getLogger(__name__)


class CreateServerCommandError(Exception):
    """Base error for the create-server command."""


class UserNotActiveError(CreateServerCommandError):
    """Raised when the requesting user is not active (frozen/banned)."""


class OfferDisabledError(CreateServerCommandError):
    """Raised when the requested offer is hidden/disabled."""


class NoWalletError(CreateServerCommandError):
    """Raised when the requesting user has no wallet."""


class QuotaExceededError(CreateServerCommandError):
    """Raised when the user's provisioning quota would be exceeded."""


class MaintenanceBlockedError(CreateServerCommandError):
    """Raised when a maintenance switch blocks new orders for the scope."""


class CostLimitReachedError(CreateServerCommandError):
    """Raised when a daily cost circuit breaker halts new spend for the scope."""


class MaintenanceSwitchError(Exception):
    """Base error for the maintenance switch service."""


class TermsGate(Protocol):
    """Port for the terms-acceptance check (implemented by the users module)."""

    async def require_latest(self, user: User) -> None:
        """Raise TermsAcceptanceRequiredError when the user's terms are stale."""
        ...


@dataclass(frozen=True, slots=True)
class CreateServerResult:
    """Outcome of the create-server command.

    ``replayed`` is True when this call was an idempotent replay of an
    earlier command with the same idempotency key.
    """

    server: CloudServer
    snapshot: ServerPriceSnapshot | None
    hold: Hold | None
    replayed: bool


class CreateServerService:
    """The create-server application command handler."""

    def __init__(
        self,
        *,
        server_repo: ServerRepository,
        account_repo: ProviderAccountRepository,
        catalog_repo: CatalogRepository,
        price_book_service: PriceBookService,
        snapshot_service: ServerPriceSnapshotService,
        wallet_repo: WalletRepository,
        hold_repo: HoldRepository,
        audit_repo: AuditRepository,
        book_name: str,
        quota: QuotaPolicy | None = None,
        maintenance: MaintenanceSwitchService | None = None,
        terms: TermsGate | None = None,
        cost_breaker: CostCircuitBreaker | None = None,
    ) -> None:
        if not book_name or not book_name.strip():
            raise ValueError("book_name must not be empty")
        self._servers = server_repo
        self._accounts = account_repo
        self._catalog = catalog_repo
        self._books = price_book_service
        self._snapshots = snapshot_service
        self._wallets = wallet_repo
        self._holds = hold_repo
        self._audit = AuditTrail(audit_repo)
        self._book_name = book_name
        self._quota = quota or QuotaPolicy()
        self._maintenance = maintenance
        self._terms = terms
        self._cost_breaker = cost_breaker

    @staticmethod
    def _hold_key(idempotency_key: str) -> str:
        """Deterministic hold idempotency key derived from the command key."""
        return f"server-create:{idempotency_key}"

    async def create_server(
        self,
        *,
        user: User,
        offer_ref: OfferRef,
        idempotency_key: str,
        at: datetime | None = None,
    ) -> CreateServerResult:
        """Validate user/offer/balance and persist the create intent.

        Steps:
        1. User must be ACTIVE (frozen/banned users are rejected).
        1.5 The terms gate (M02-004), when wired, must see the user on the
            latest published terms (TermsAcceptanceRequiredError otherwise).
        2. Offer must exist in the catalog and be enabled.
        2.5 The maintenance switch (M10-005), when wired, must not block
            new orders for the offer's provider/location.
        3. The user must have an ACTIVE account with the offer's provider.
        4. The selling price is derived from the versioned price book.
        5. Replay check: the same idempotency key returns the original
           outcome without reserving funds again.
        6. Quota: the user's active/lifetime server counts must leave room for one
            more (QuotaExceededError).
        7. The wallet must exist and cover the first quantum: a hold
           reserves the funds (idempotent per command key).
        8. A REQUESTED server row is persisted with the unique
           idempotency key and the pinned catalog offer.
        9. An immutable price snapshot fixes the server's price.
        10. The mutation is audited (USER actor).

        On any failure after the hold is placed, the hold is released
        (best effort) and the intent is marked ERROR, so a failed command
        never leaves an orphaned reservation or a live intent.

        Raises:
            UserNotActiveError: Frozen or banned user.
            TermsAcceptanceRequiredError: User's accepted terms are not the
                latest published version.
            OfferNotFoundError: Unknown offer (from the catalog port).
            OfferDisabledError: Offer hidden/disabled.
            MaintenanceBlockedError: A maintenance switch blocks the scope.
            CostLimitReachedError: A daily cost circuit breaker tripped.
            NoProviderAccountError: No active account with the provider.
            QuotaExceededError: Concurrent or lifetime quota reached.
            InsufficientHoldBalanceError: Wallet balance below the price.
            ServerCreateError: Constraint violation on the server row.
            CreateServerCommandError: Idempotency key owned by another user.
        """
        # 1. User.
        if user.id is None:
            raise CreateServerCommandError("a persisted user id is required")
        if user.status is not UserStatus.ACTIVE:
            raise UserNotActiveError(f"user {user.id} is {user.status.value}")

        # 1.5 Terms (M02-004), when wired: provisioning requires the latest terms.
        if self._terms is not None:
            await self._terms.require_latest(user)

        # 2. Offer.
        offer = await self._catalog.get_offer(offer_ref)
        if offer is None:
            raise OfferNotFoundError(f"unknown offer {offer_ref.key}")
        if not offer.enabled:
            raise OfferDisabledError(f"offer {offer_ref.key} is not enabled")

        # 2.5 Maintenance switch: new orders for a blocked provider/location
        # stop here; existing resources are untouched (no power/billing gate).
        if self._maintenance is not None:
            blocked_scope = await self._maintenance.blocking_scope(
                offer_ref.provider_key, offer_ref.location_id
            )
            if blocked_scope is not None:
                where = (
                    f"provider {offer_ref.provider_key}"
                    if blocked_scope.location_id is None
                    else f"provider {offer_ref.provider_key} location {offer_ref.location_id}"
                )
                raise MaintenanceBlockedError(
                    f"new orders for {where} are blocked (provider maintenance)"
                )

        # 3. Provider account (read only; fail fast before touching money).
        account = await self._accounts.get_active(user.id, offer_ref.provider_key)
        if account is None:
            raise NoProviderAccountError(
                f"user {user.id} has no active {offer_ref.provider_key} account"
            )

        # 3.5 Cost circuit breaker (M10-004), when wired: a tripped daily
        # provider-cost cap (global or this account's) halts NEW spend.
        # Checked before any money is reserved, like the maintenance switch.
        if self._cost_breaker is not None:
            trigger = await self._cost_breaker.check(account.id)
            if trigger is not None:
                where = (
                    "all provider accounts"
                    if trigger.scope is CostLimitScopeKind.GLOBAL
                    else f"provider account {trigger.provider_account_id}"
                )
                raise CostLimitReachedError(
                    f"new orders halted for {where}: daily provider cost "
                    f"{trigger.spent_minor} reached the limit {trigger.limit_minor}"
                )

        # 4. Price (versioned price book).
        when = at if at is not None else datetime.now(UTC)
        cost = OfferCost(
            offer_ref.provider_key,
            offer_ref.plan_id,
            offer_ref.location_id,
            offer.price_per_quantum,
            offer.currency,
        )
        price: SellingPrice = await self._books.sell_price(
            book_name=self._book_name, offer=cost, at=when
        )

        # 5. Replay check (before reserving funds).
        hold_key = self._hold_key(idempotency_key)
        existing = await self._servers.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            if existing.user_id != user.id:
                raise CreateServerCommandError(
                    f"idempotency key {idempotency_key!r} belongs to another user"
                )
            wallet = await self._wallets.get(user.id)
            wallet_id = wallet.id if wallet is not None and wallet.id is not None else None
            hold: Hold | None = (
                await self._holds.get_by_idempotency(wallet_id, hold_key)
                if wallet_id is not None
                else None
            )
            snapshot = await self._snapshots.get_snapshot(existing.id)
            return CreateServerResult(server=existing, snapshot=snapshot, hold=hold, replayed=True)

        # 5.5 Quota (after the replay check so replays stay idempotent).
        active = await self._servers.count_active(user.id)
        if active >= self._quota.max_active:
            raise QuotaExceededError(
                f"user has {active} active servers (concurrent limit {self._quota.max_active})"
            )
        total = await self._servers.count_total(user.id)
        if total >= self._quota.max_total:
            raise QuotaExceededError(
                f"user has created {total} servers (lifetime limit {self._quota.max_total})"
            )

        # 6. Wallet + hold (balance validation lives in the hold).
        wallet = await self._wallets.get(user.id)
        if wallet is None or wallet.id is None:
            raise NoWalletError(f"user {user.id} has no wallet")
        wallet_id = wallet.id
        hold = await self._holds.create_hold(
            wallet_id, price.selling_minor, offer.currency, hold_key
        )

        # 7. Server intent (REQUESTED row + idempotency key).
        server = CloudServer(
            id=uuid4(),
            user_id=user.id,
            provider_key=offer_ref.provider_key,
            provider_account_id=account.id,
            state=ServerLifecycleState.REQUESTED,
        )
        intent = ServerCreateIntent(
            catalog_id=offer.id,
            cost_minor=offer.price_per_quantum,
            currency=offer.currency,
            idempotency_key=idempotency_key,
        )
        try:
            created = await self._servers.create(server, intent)
        except ServerCreateError:
            # Concurrent duplicate: the key was consumed between the replay
            # check and the insert. Resolve to the original intent.
            original = await self._servers.get_by_idempotency_key(idempotency_key)
            if original is not None and original.user_id == user.id:
                original_hold: Hold | None = await self._holds.get_by_idempotency(
                    wallet_id, hold_key
                )
                original_snapshot = await self._snapshots.get_snapshot(original.id)
                return CreateServerResult(
                    server=original,
                    snapshot=original_snapshot,
                    hold=original_hold,
                    replayed=True,
                )
            # Not a duplicate (e.g. an FK violation): release the hold so a
            # failed command never leaves an orphaned reservation.
            await self._release_hold(hold)
            raise

        # 8. Immutable price snapshot (billing source of truth).
        try:
            snapshot = await self._snapshots.create_snapshot(
                server_id=created.id,
                price=price,
                actor=None,
                reason="server create request",
            )
        except Exception:
            await self._fail_intent(created, hold, wallet_id)
            raise

        # 9. Audit.
        await self._audit.record_mutation(
            actor_type=ActorType.USER,
            actor_id=user.id,
            action="server.create_requested",
            resource_type="server",
            resource_id=str(created.id),
            reason=f"create {offer_ref.key}",
            metadata={
                "offer": offer_ref.key,
                "selling_minor": str(price.selling_minor),
                "currency": offer.currency,
                "book": self._book_name,
                "book_version": price.version,
                "hold_id": str(hold.id) if hold.id is not None else "",
            },
        )
        logger.info(
            "create intent for server %s (hold %s, book %s v%d)",
            created.id,
            hold.id,
            self._book_name,
            price.version,
        )
        return CreateServerResult(server=created, snapshot=snapshot, hold=hold, replayed=False)

    async def _fail_intent(self, server: CloudServer, hold: Hold, wallet_id: UUID) -> None:
        """Best-effort compensation for a failed intent: ERROR + release hold."""
        try:
            server.transition_to(ServerLifecycleState.ERROR)
            await self._servers.save(server)
        except Exception:
            logger.exception("failed to mark server %s ERROR after create failure", server.id)
        await self._release_hold(hold)

    async def _release_hold(self, hold: Hold) -> None:
        if hold.id is None:
            return
        try:
            await self._holds.release_hold(hold.id)
        except Exception:
            logger.exception("failed to release hold %s after create failure", hold.id)


# ---------------------------------------------------------------------------
# My Servers list/detail (M08-007)
# ---------------------------------------------------------------------------

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20


@dataclass(frozen=True, slots=True)
class ServerSummary:
    """One row of the user's server list."""

    server_id: UUID
    provider_key: str
    state: ServerLifecycleState
    created_at: datetime | None


@dataclass(frozen=True, slots=True)
class ServerDetail:
    """The detail view of one server the user owns."""

    server_id: UUID
    provider_key: str
    state: ServerLifecycleState
    provider_server_id: str | None
    created_at: datetime | None


@dataclass(frozen=True, slots=True)
class ServerPage:
    """One page of the server list plus pagination context."""

    items: tuple[ServerSummary, ...]
    total: int
    offset: int
    limit: int

    @property
    def has_next(self) -> bool:
        return self.offset + len(self.items) < self.total

    @property
    def pages(self) -> int:
        if self.limit <= 0:
            return 0
        return (self.total + self.limit - 1) // self.limit


class MyServersService:
    """User-facing read service: the user's own servers, paged and owned.

    Ownership is enforced in the application layer (not the UI): the list
    query is filtered to the acting user, and the detail lookup returns a
    uniform ``None`` for a missing *or foreign* server, so existence of
    other users' servers is never leaked.
    """

    def __init__(self, server_repo: ServerRepository) -> None:
        self._servers = server_repo

    async def list_servers(
        self, user_id: UUID, *, offset: int = 0, limit: int = DEFAULT_PAGE_SIZE
    ) -> ServerPage:
        """One page of the user's servers, newest first."""
        if offset < 0:
            raise ValueError("offset must be >= 0")
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if limit > MAX_PAGE_SIZE:
            raise ValueError(f"limit must be <= {MAX_PAGE_SIZE}")
        items, total = await self._servers.list_by_user_paged(user_id, offset=offset, limit=limit)
        return ServerPage(
            items=tuple(
                ServerSummary(
                    server_id=s.id,
                    provider_key=s.provider_key,
                    state=s.state,
                    created_at=s.created_at,
                )
                for s in items
            ),
            total=total,
            offset=offset,
            limit=limit,
        )

    async def get_server(self, user_id: UUID, server_id: UUID) -> ServerDetail | None:
        """One server the user owns, or None (missing or foreign alike)."""
        server = await self._servers.get(server_id)
        if server is None or server.user_id != user_id:
            return None
        return ServerDetail(
            server_id=server.id,
            provider_key=server.provider_key,
            state=server.state,
            provider_server_id=server.provider_server_id,
            created_at=server.created_at,
        )


# ---------------------------------------------------------------------------
# Provider/location maintenance switches (M10-005)
# ---------------------------------------------------------------------------


class MaintenanceSwitchService:
    """Operator switches that stop NEW orders for a provider or location.

    Existing resources are untouched: the switch only feeds
    :meth:`CreateServerService.create_server` via :meth:`blocking_scope`.
    Mutations are admin-gated (``admin:manage_settings``) and audited; a
    non-empty reason is required. Re-blocking a scope updates its reason
    (idempotent upsert keyed by scope).
    """

    def __init__(
        self,
        repo: MaintenanceSwitchRepository,
        audit_repo: AuditRepository,
    ) -> None:
        self._repo = repo
        self._audit = AuditTrail(audit_repo)

    @staticmethod
    def _require_reason(reason: str) -> None:
        if not reason or not reason.strip():
            raise MaintenanceSwitchError("maintenance commands must carry a non-empty reason")

    @staticmethod
    def _actor_context(actor: User | None) -> tuple[ActorType, UUID | None]:
        if actor is None:
            return ActorType.SYSTEM, None
        return ActorType.ADMIN, actor.id

    @staticmethod
    def _authorize(actor: User | None) -> None:
        if actor is not None:
            PermissionChecker(actor).require(Permission.ADMIN_MANAGE_SETTINGS)

    async def block_provider(
        self, *, actor: User | None, provider_key: str, reason: str
    ) -> MaintenanceBlock:
        """Stop all new orders for a provider."""
        self._authorize(actor)
        self._require_reason(reason)
        scope = MaintenanceScope(provider_key=provider_key)
        block = MaintenanceBlock(
            scope=scope,
            reason=reason,
            created_by=self._actor_context(actor)[1],
            created_at=datetime.now(UTC),
        )
        saved = await self._repo.save_block(block)
        await self._audit.record_mutation(
            actor_type=self._actor_context(actor)[0],
            actor_id=self._actor_context(actor)[1],
            action="maintenance.block",
            resource_type="provider",
            resource_id=provider_key,
            reason=reason,
            metadata=None,
        )
        return saved

    async def block_location(
        self, *, actor: User | None, provider_key: str, location_id: str, reason: str
    ) -> MaintenanceBlock:
        """Stop new orders for one provider/location pair only."""
        self._authorize(actor)
        self._require_reason(reason)
        scope = MaintenanceScope(provider_key=provider_key, location_id=location_id)
        block = MaintenanceBlock(
            scope=scope,
            reason=reason,
            created_by=self._actor_context(actor)[1],
            created_at=datetime.now(UTC),
        )
        saved = await self._repo.save_block(block)
        await self._audit.record_mutation(
            actor_type=self._actor_context(actor)[0],
            actor_id=self._actor_context(actor)[1],
            action="maintenance.block",
            resource_type="provider",
            resource_id=provider_key,
            reason=reason,
            metadata={"location_id": location_id},
        )
        return saved

    async def unblock_provider(self, *, actor: User | None, provider_key: str, reason: str) -> bool:
        """Resume new orders for a provider."""
        self._authorize(actor)
        self._require_reason(reason)
        removed = await self._repo.remove_block(MaintenanceScope(provider_key=provider_key))
        await self._audit.record_mutation(
            actor_type=self._actor_context(actor)[0],
            actor_id=self._actor_context(actor)[1],
            action="maintenance.unblock",
            resource_type="provider",
            resource_id=provider_key,
            reason=reason,
            metadata=None,
        )
        return removed

    async def unblock_location(
        self, *, actor: User | None, provider_key: str, location_id: str, reason: str
    ) -> bool:
        """Resume new orders for one provider/location pair."""
        self._authorize(actor)
        self._require_reason(reason)
        removed = await self._repo.remove_block(
            MaintenanceScope(provider_key=provider_key, location_id=location_id)
        )
        await self._audit.record_mutation(
            actor_type=self._actor_context(actor)[0],
            actor_id=self._actor_context(actor)[1],
            action="maintenance.unblock",
            resource_type="provider",
            resource_id=provider_key,
            reason=reason,
            metadata={"location_id": location_id},
        )
        return removed

    async def active_blocks(self) -> list[MaintenanceBlock]:
        """All active switches."""
        return await self._repo.list_blocks()

    async def blocking_scope(self, provider_key: str, location_id: str) -> MaintenanceScope | None:
        """The switch covering new orders for the scope, if any."""
        blocks = await self._repo.list_blocks()
        for block in blocks:
            if block.scope.provider_key != provider_key:
                continue
            if block.scope.location_id is None or block.scope.location_id == location_id:
                return block.scope
        return None

    async def is_order_blocked(self, provider_key: str, location_id: str) -> bool:
        """True when new orders for the scope are currently blocked."""
        return await self.blocking_scope(provider_key, location_id) is not None


# ---------------------------------------------------------------------------
# Cost circuit breakers (M10-004)
# ---------------------------------------------------------------------------


class CostCircuitBreakerService:
    """Daily provider-cost caps that can halt new spend (M10-004).

    Acceptance: **global/provider-account daily thresholds can halt new
    spend.** The daily spend is the provider COST accrued today (UTC day,
    the ``cost_minor`` of the accrual-period records - what the platform
    actually spends with providers), scoped to:

    - the GLOBAL limit: every server of every provider account;
    - a PROVIDER ACCOUNT limit: only that account's (non-deleted) servers.

    A scope whose accrued cost reaches its limit is tripped; the create
    command consults :meth:`check` before reserving any funds and refuses
    new orders while a scope is tripped. The account's own limit is the
    most specific trigger and wins over the global one in the error. No
    limit row, a disabled row, or a limit of 0-or-less never trips
    (``active`` is False), so the breaker is opt-in per scope.

    This is a gate on NEW spend only: existing servers keep running and
    keep accruing until an operator raises the limit or the day rolls
    over; nothing here stops, bills, or deletes anything.
    """

    def __init__(
        self,
        *,
        limit_repo: CostLimitRepository,
        server_repo: ServerRepository,
        accrual_repo: AccrualPeriodRepository,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._limits = limit_repo
        self._servers = server_repo
        self._accruals = accrual_repo
        self._now = clock or (lambda: datetime.now(UTC))

    def _day_window(self, now: datetime) -> tuple[datetime, datetime]:
        """The current UTC day as a half-open [start, end) window."""
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return day_start, day_start + timedelta(days=1)

    async def _spent_today(self, server_ids: frozenset[UUID], now: datetime) -> int:
        day_start, day_end = self._day_window(now)
        return await self._accruals.daily_cost_total(day_start, day_end, server_ids)

    async def check(self, provider_account_id: UUID) -> CostLimitTrigger | None:
        """The tripped scope for new spend on this account, if any."""
        now = self._now()

        # M11-006: the spend alert feed - always set, so the gauge reflects
        # the current UTC day even when no cost limit is configured.
        all_ids = frozenset(await self._servers.list_non_deleted_ids())
        global_spent = await self._spent_today(all_ids, now)
        metrics.record_daily_provider_cost(global_spent, {})

        # Most specific first: the account's own limit, then the global one.
        account_limit = await self._limits.get_for_account(provider_account_id)
        if account_limit is not None and account_limit.active:
            account_ids = frozenset(
                await self._servers.list_account_server_ids(provider_account_id)
            )
            spent = await self._spent_today(account_ids, now)
            if spent >= account_limit.limit_minor:
                return CostLimitTrigger(
                    scope=CostLimitScopeKind.PROVIDER_ACCOUNT,
                    provider_account_id=provider_account_id,
                    limit_minor=account_limit.limit_minor,
                    spent_minor=spent,
                )

        global_limit = await self._limits.get_global()
        if global_limit is not None and global_limit.active:
            if global_spent >= global_limit.limit_minor:
                return CostLimitTrigger(
                    scope=CostLimitScopeKind.GLOBAL,
                    provider_account_id=None,
                    limit_minor=global_limit.limit_minor,
                    spent_minor=global_spent,
                )
        return None

    async def status(self, provider_account_id: UUID | None = None) -> list[tuple[CostLimit, int]]:
        """(limit, spent_today) for every active limit (inspection)."""
        now = self._now()
        day_start, day_end = self._day_window(now)
        out: list[tuple[CostLimit, int]] = []
        for limit in await self._limits.list_all():
            if not limit.active:
                continue
            if limit.scope is CostLimitScopeKind.GLOBAL:
                ids = frozenset(await self._servers.list_non_deleted_ids())
            elif provider_account_id is not None and (
                limit.provider_account_id != provider_account_id
            ):
                continue
            else:
                assert limit.provider_account_id is not None
                ids = frozenset(
                    await self._servers.list_account_server_ids(limit.provider_account_id)
                )
            spent = await self._accruals.daily_cost_total(day_start, day_end, ids)
            out.append((limit, spent))
        return out


class CostLimitAdminService:
    """Admin commands that manage the daily cost limits (M10-004).

    Mutations are admin-gated (``admin:manage_settings``) and audited, like
    the maintenance switches (M10-005). Setting a limit of 0 or disabling a
    row disables enforcement for the scope (no limit = no breaker).
    """

    def __init__(
        self,
        *,
        limit_repo: CostLimitRepository,
        audit_repo: AuditRepository,
    ) -> None:
        self._limits = limit_repo
        self._audit = AuditTrail(audit_repo)

    @staticmethod
    def _actor_context(actor: User | None) -> tuple[ActorType, UUID | None]:
        if actor is None:
            return ActorType.SYSTEM, None
        return ActorType.ADMIN, actor.id

    @staticmethod
    def _authorize(actor: User | None) -> None:
        if actor is not None:
            PermissionChecker(actor).require(Permission.ADMIN_MANAGE_SETTINGS)

    async def set_global(
        self,
        *,
        actor: User | None,
        limit_minor: int,
        enabled: bool = True,
        reason: str = "",
    ) -> CostLimit:
        """Set (or clear) the global daily provider-cost cap."""
        self._authorize(actor)
        if limit_minor < 0:
            raise ValueError("limit_minor must not be negative")
        saved = await self._limits.upsert(
            CostLimit(
                scope=CostLimitScopeKind.GLOBAL,
                limit_minor=limit_minor,
                enabled=enabled,
            )
        )
        actor_type, actor_id = self._actor_context(actor)
        await self._audit.record_mutation(
            actor_type=actor_type,
            actor_id=actor_id,
            action="cost_limit.set",
            resource_type="cost_limit",
            resource_id="global",
            reason=reason,
            metadata={
                "scope": "global",
                "limit_minor": str(limit_minor),
                "enabled": str(enabled),
            },
        )
        return saved

    async def set_for_account(
        self,
        *,
        actor: User | None,
        provider_account_id: UUID,
        limit_minor: int,
        enabled: bool = True,
        reason: str = "",
    ) -> CostLimit:
        """Set (or clear) one provider account's daily provider-cost cap."""
        self._authorize(actor)
        if limit_minor < 0:
            raise ValueError("limit_minor must not be negative")
        saved = await self._limits.upsert(
            CostLimit(
                scope=CostLimitScopeKind.PROVIDER_ACCOUNT,
                limit_minor=limit_minor,
                provider_account_id=provider_account_id,
                enabled=enabled,
            )
        )
        actor_type, actor_id = self._actor_context(actor)
        await self._audit.record_mutation(
            actor_type=actor_type,
            actor_id=actor_id,
            action="cost_limit.set",
            resource_type="cost_limit",
            resource_id=str(provider_account_id),
            reason=reason,
            metadata={
                "scope": "provider_account",
                "provider_account_id": str(provider_account_id),
                "limit_minor": str(limit_minor),
                "enabled": str(enabled),
            },
        )
        return saved

    async def clear(
        self,
        *,
        actor: User | None,
        provider_account_id: UUID | None = None,
        reason: str = "",
    ) -> bool:
        """Remove a limit row (global or one account's). True when removed."""
        self._authorize(actor)
        if provider_account_id is None:
            removed = await self._limits.remove(CostLimitScopeKind.GLOBAL)
            resource_id = "global"
            scope = "global"
        else:
            removed = await self._limits.remove(
                CostLimitScopeKind.PROVIDER_ACCOUNT, provider_account_id
            )
            resource_id = str(provider_account_id)
            scope = "provider_account"
        actor_type, actor_id = self._actor_context(actor)
        await self._audit.record_mutation(
            actor_type=actor_type,
            actor_id=actor_id,
            action="cost_limit.clear",
            resource_type="cost_limit",
            resource_id=resource_id,
            reason=reason,
            metadata={"scope": scope, "removed": str(removed)},
        )
        return removed

    async def list(self) -> list[CostLimit]:
        """Every configured limit row (inspection, read-only)."""
        return await self._limits.list_all()
