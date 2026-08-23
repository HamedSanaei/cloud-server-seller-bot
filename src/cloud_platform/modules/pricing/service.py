"""Price book service: publish versions and derive selling prices (M06-001).

Publishing a version is an admin-authorized, audited mutation (money-
affecting). Deriving a selling price is a pure read: it resolves the latest
version effective at the given instant and applies the most specific margin
rule, so every price is explicit and reproducible.
"""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

from cloud_platform.modules.audit.domain import ActorType, AuditRepository
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.pricing.domain import (
    MarginRule,
    MissingPriceSnapshotError,
    NoActiveVersionError,
    OfferCost,
    PriceBookRepository,
    PriceBookVersion,
    SellingPrice,
    ServerPriceSnapshot,
    ServerPriceSnapshotRepository,
    active_version,
    derive_selling_price,
    snapshot_from_selling_price,
)
from cloud_platform.modules.users.domain import (
    Permission,
    PermissionChecker,
    User,
)

logger = logging.getLogger(__name__)


class PriceBookService:
    """Publishes price book versions and derives selling prices."""

    def __init__(
        self,
        book_repo: PriceBookRepository,
        audit_repo: AuditRepository,
    ) -> None:
        self._books = book_repo
        self._audit = AuditTrail(audit_repo)

    async def publish_version(
        self,
        *,
        book_name: str,
        rules: tuple[MarginRule, ...],
        effective_at: datetime,
        actor: User,
        reason: str,
    ) -> PriceBookVersion:
        """Publish the next version of a book.

        The version number is assigned as (max existing + 1); the unique
        (book, version) constraint makes concurrent publishes fail loudly
        instead of clobbering each other.

        Raises:
            PermissionDeniedError: If the actor lacks admin:manage_settings.
            ValueError: On empty reason, empty book name, or invalid rules
                (via domain validation).
            DuplicateBookVersionError: On a concurrent version collision.
        """
        PermissionChecker(actor).require(Permission.ADMIN_MANAGE_SETTINGS)
        if not reason or not reason.strip():
            raise ValueError("publishing a price book version requires a non-empty reason")
        if not book_name or not book_name.strip():
            raise ValueError("book_name must not be empty")

        existing = await self._books.list_versions(book_name)
        next_version = max((v.version for v in existing), default=0) + 1

        # Validate via the domain aggregate before any persistence.
        candidate = PriceBookVersion(
            book_name=book_name,
            version=next_version,
            effective_at=effective_at,
            rules=rules,
        )
        created = await self._books.create_version(candidate)

        await self._audit.record_mutation(
            actor_type=ActorType.ADMIN,
            actor_id=actor.id,
            action="pricing.publish_book_version",
            resource_type="price_book",
            resource_id=book_name,
            reason=reason,
            metadata={
                "version": created.version,
                "effective_at": effective_at.isoformat(),
                "rule_count": len(rules),
            },
        )
        logger.info("published %s v%d effective %s", book_name, created.version, effective_at)
        return created

    async def sell_price(
        self,
        *,
        book_name: str,
        offer: OfferCost,
        at: datetime,
    ) -> SellingPrice:
        """Derive the selling price for ``offer`` at instant ``at``.

        Raises:
            NoActiveVersionError: If the book has no version effective at
                ``at`` (unknown book included).
            NoMarginRuleError: If the active version has no matching rule.
        """
        if at.tzinfo is None:
            raise ValueError("at must be timezone-aware")
        versions = await self._books.list_versions(book_name)
        current = active_version(versions, at)
        if current is None:
            raise NoActiveVersionError(f"no {book_name!r} version effective at {at.isoformat()}")
        return derive_selling_price(current, offer, at)


class ServerPriceSnapshotService:
    """Fixes a server's price into an immutable snapshot at provisioning.

    The snapshot is the billing source of truth: once created it is never
    updated, so catalog or price-book changes cannot move a historical
    price. Creation is audited; a server with an existing snapshot can never
    be re-priced.
    """

    def __init__(
        self,
        snapshot_repo: ServerPriceSnapshotRepository,
        audit_repo: AuditRepository,
    ) -> None:
        self._snapshots = snapshot_repo
        self._audit = AuditTrail(audit_repo)

    async def create_snapshot(
        self,
        *,
        server_id: UUID,
        price: SellingPrice,
        actor: User | None = None,
        reason: str = "server provisioning",
    ) -> ServerPriceSnapshot:
        """Fix ``price`` as the server's immutable price.

        ``actor`` may be None (system provisioning) or an admin user.

        Raises:
            PermissionDeniedError: If a non-admin user provides the actor.
            ValueError: On an empty reason when an admin actor is given.
            SnapshotAlreadyExistsError: If the server already has a snapshot.
        """
        if actor is None:
            actor_type = ActorType.SYSTEM
            actor_id: UUID | None = None
        else:
            PermissionChecker(actor).require(Permission.ADMIN_MANAGE_SETTINGS)
            if not reason or not reason.strip():
                raise ValueError("admin price snapshotting requires a non-empty reason")
            actor_type = ActorType.ADMIN
            actor_id = actor.id

        snapshot = snapshot_from_selling_price(server_id, price)
        created = await self._snapshots.create(snapshot)

        await self._audit.record_mutation(
            actor_type=actor_type,
            actor_id=actor_id,
            action="pricing.snapshot_server",
            resource_type="server",
            resource_id=str(server_id),
            reason=reason,
            metadata={
                "book": created.book_name,
                "book_version": created.book_version,
                "selling_minor": created.selling_minor,
                "cost_minor": created.offer.cost_minor,
                "currency": created.offer.currency,
            },
        )
        logger.info(
            "priced server %s at %d %s (book %s v%d)",
            server_id,
            created.selling_minor,
            created.offer.currency,
            created.book_name,
            created.book_version,
        )
        return created

    async def get_snapshot(self, server_id: UUID) -> ServerPriceSnapshot | None:
        """The server's snapshot, or None if never priced."""
        return await self._snapshots.get(server_id)

    async def require_snapshot(self, server_id: UUID) -> ServerPriceSnapshot:
        """The server's snapshot or MissingPriceSnapshotError.

        Billing must go through this accessor so that an unpriced server can
        never be charged.
        """
        snapshot = await self._snapshots.get(server_id)
        if snapshot is None:
            raise MissingPriceSnapshotError(f"server {server_id} has no price snapshot")
        return snapshot
