"""Pricing domain: price books and margin rules (M06-001).

A **price book** is a named, versioned set of **margin rules**. Each rule
matches offers by (provider, plan, location) patterns — ``"*"`` is a
wildcard — and derives the selling price from the provider cost:

    selling_minor = cost_minor * margin_factor + fixed_minor

rounded half-up to integer minor units. All money math is Decimal-based;
nothing is hard-coded and no float touches a price.

Versioning is explicit: every book version has an ``effective_at`` instant,
and the price for a sale at ``at`` is derived from the **latest version with
``effective_at <= at``** — older versions remain derivable, so historical
prices are reproducible.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol
from uuid import UUID

WILDCARD = "*"


class PricingError(Exception):
    """Base error for pricing operations."""


class NoMarginRuleError(PricingError):
    """Raised when no rule in the active version matches the offer."""


class AmbiguousMarginRuleError(PricingError):
    """Raised when two same-specificity rules match the same offer."""


class NoActiveVersionError(PricingError):
    """Raised when the book has no version effective at the given instant."""


class DuplicateBookVersionError(PricingError):
    """Raised when a (book, version) pair already exists."""


class SnapshotAlreadyExistsError(PricingError):
    """Raised when a server already has a price snapshot (immutability)."""


class MissingPriceSnapshotError(PricingError):
    """Raised when a server has no price snapshot."""


def _require_pattern(value: str, field: str) -> None:
    if not value or (value.strip() == "" and value != WILDCARD):
        raise ValueError(f"{field} must be {WILDCARD!r} or a non-empty pattern")


@dataclass(frozen=True, slots=True)
class MarginRule:
    """One margin rule: pattern + explicit margin + optional monthly cap.

    ``margin_factor`` multiplies the provider cost (e.g. Decimal("1.15") =
    +15%). ``fixed_minor`` is a flat per-quantum addition in minor units.
    ``monthly_cap_minor`` is an optional per-user, per-calendar-month (UTC)
    maximum for all charges under this rule; None means uncapped. The cap
    must never be exceeded: when it is reached, further settlement of
    periods for that user in that month is skipped (or, at deletion, the
    reserved creation hold is released back to the wallet).
    """

    provider: str
    plan: str
    location: str
    margin_factor: Decimal
    fixed_minor: int = 0
    monthly_cap_minor: int | None = None

    def __post_init__(self) -> None:
        _require_pattern(self.provider, "provider")
        _require_pattern(self.plan, "plan")
        _require_pattern(self.location, "location")
        if self.margin_factor <= 0:
            raise ValueError("margin_factor must be > 0")
        if self.fixed_minor < 0:
            raise ValueError("fixed_minor must not be negative")
        if self.monthly_cap_minor is not None and self.monthly_cap_minor < 0:
            raise ValueError("monthly_cap_minor must be >= 0 when set")

    def specificity(self) -> int:
        """Number of concrete (non-wildcard) fields: 0..3."""
        return sum(1 for v in (self.provider, self.plan, self.location) if v != WILDCARD)

    def matches(self, offer: OfferCost) -> bool:
        return (
            (self.provider == WILDCARD or self.provider == offer.provider_key)
            and (self.plan == WILDCARD or self.plan == offer.plan_id)
            and (self.location == WILDCARD or self.location == offer.location_id)
        )

    @property
    def pattern(self) -> tuple[str, str, str]:
        return (self.provider, self.plan, self.location)


@dataclass(frozen=True, slots=True)
class OfferCost:
    """The provider cost side of one offer (per quantum, minor units)."""

    provider_key: str
    plan_id: str
    location_id: str
    cost_minor: int
    currency: str

    def __post_init__(self) -> None:
        for name, value in (
            ("provider_key", self.provider_key),
            ("plan_id", self.plan_id),
            ("location_id", self.location_id),
            ("currency", self.currency),
        ):
            if not value or not value.strip():
                raise ValueError(f"{name} must not be empty")
        if self.cost_minor < 0:
            raise ValueError("cost_minor must not be negative")


@dataclass(frozen=True, slots=True)
class SellingPrice:
    """A selling price derived from a specific book version.

    Carries the rule and version it came from, so any historical price is
    reproducible and auditable.
    """

    offer: OfferCost
    selling_minor: int
    rule: MarginRule
    book_name: str
    version: int
    priced_at: datetime


@dataclass(frozen=True, slots=True)
class PriceBookVersion:
    """One immutable version of a named price book."""

    book_name: str
    version: int
    effective_at: datetime
    rules: tuple[MarginRule, ...]
    id: UUID | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.book_name or not self.book_name.strip():
            raise ValueError("book_name must not be empty")
        if self.version < 1:
            raise ValueError("version must be >= 1")
        if self.effective_at.tzinfo is None:
            raise ValueError("effective_at must be timezone-aware")
        if not self.rules:
            raise ValueError("a price book version needs at least one rule")
        seen: set[tuple[str, str, str]] = set()
        for rule in self.rules:
            if rule.pattern in seen:
                raise ValueError(f"duplicate rule pattern {rule.pattern!r}")
            seen.add(rule.pattern)


@dataclass(frozen=True, slots=True)
class ServerPriceSnapshot:
    """The immutable price of one server, fixed at provisioning (M06-002).

    Billing must read this snapshot — never the catalog or the price book —
    so historical prices are unaffected by catalog or price-book changes.
    There is exactly one snapshot per server and it is never updated.
    """

    server_id: UUID
    offer: OfferCost
    selling_minor: int
    book_name: str
    book_version: int
    rule: MarginRule
    priced_at: datetime
    id: UUID | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.selling_minor < 0:
            raise ValueError("selling_minor must not be negative")
        if self.book_version < 1:
            raise ValueError("book_version must be >= 1")
        if not self.book_name or not self.book_name.strip():
            raise ValueError("book_name must not be empty")
        if self.priced_at.tzinfo is None:
            raise ValueError("priced_at must be timezone-aware")


def snapshot_from_selling_price(server_id: UUID, price: SellingPrice) -> ServerPriceSnapshot:
    """Build a server snapshot from a versioned selling price."""
    return ServerPriceSnapshot(
        server_id=server_id,
        offer=price.offer,
        selling_minor=price.selling_minor,
        book_name=price.book_name,
        book_version=price.version,
        rule=price.rule,
        priced_at=price.priced_at,
    )


# ---------------------------------------------------------------------------
# Pure derivation
# ---------------------------------------------------------------------------


def active_version(versions: Iterable[PriceBookVersion], at: datetime) -> PriceBookVersion | None:
    """The latest version effective at ``at`` (effective_at <= at), or None."""
    best: PriceBookVersion | None = None
    for candidate in versions:
        if candidate.effective_at <= at and (best is None or candidate.version > best.version):
            best = candidate
    return best


def _match_rule(rules: tuple[MarginRule, ...], offer: OfferCost) -> MarginRule:
    best: MarginRule | None = None
    for rule in rules:
        if not rule.matches(offer):
            continue
        if best is None or rule.specificity() > best.specificity():
            best = rule
        elif rule.specificity() == best.specificity():
            # Unreachable when PriceBookVersion validation holds (duplicate
            # same-specificity patterns are rejected at construction), but
            # fail loudly rather than price ambiguously.
            raise AmbiguousMarginRuleError(
                f"rules {best.pattern} and {rule.pattern} both match {offer.plan_id!r}"
            )
    if best is None:
        raise NoMarginRuleError(
            f"no margin rule matches {offer.provider_key}/{offer.plan_id}/{offer.location_id}"
        )
    return best


def derive_selling_price(version: PriceBookVersion, offer: OfferCost, at: datetime) -> SellingPrice:
    """Derive the selling price for ``offer`` under ``version`` at ``at``.

    Pure: same (version, offer, at) always yields the same result.
    """
    rule = _match_rule(version.rules, offer)
    raw = Decimal(offer.cost_minor) * rule.margin_factor + Decimal(rule.fixed_minor)
    selling_minor = int(raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return SellingPrice(
        offer=offer,
        selling_minor=selling_minor,
        rule=rule,
        book_name=version.book_name,
        version=version.version,
        priced_at=at,
    )


# ---------------------------------------------------------------------------
# Port
# ---------------------------------------------------------------------------


class PriceBookRepository(Protocol):
    """Port for durable, versioned price books."""

    async def create_version(self, version: PriceBookVersion) -> PriceBookVersion:
        """Persist a validated version; raise DuplicateBookVersionError on collision."""
        ...

    async def list_versions(self, book_name: str) -> list[PriceBookVersion]:
        """All versions of a book, newest first."""
        ...

    async def get(self, book_name: str, version: int) -> PriceBookVersion | None:
        """One version, or None."""
        ...


class ServerPriceSnapshotRepository(Protocol):
    """Port for immutable per-server price snapshots."""

    async def create(self, snapshot: ServerPriceSnapshot) -> ServerPriceSnapshot:
        """Persist a snapshot; raise SnapshotAlreadyExistsError if the server has one."""
        ...

    async def get(self, server_id: UUID) -> ServerPriceSnapshot | None:
        """The server's snapshot, or None."""
        ...


# ---------------------------------------------------------------------------
# Persistence helpers (rules <-> JSON-serializable dicts)
# ---------------------------------------------------------------------------


def rule_to_dict(rule: MarginRule) -> dict[str, object]:
    return {
        "provider": rule.provider,
        "plan": rule.plan,
        "location": rule.location,
        "margin_factor": str(rule.margin_factor),
        "fixed_minor": rule.fixed_minor,
        "monthly_cap_minor": rule.monthly_cap_minor,
    }


def rule_from_dict(raw: dict[str, object]) -> MarginRule:
    fixed = raw["fixed_minor"]
    if not isinstance(fixed, int) or isinstance(fixed, bool):
        raise ValueError("fixed_minor must be an int")
    cap = raw.get("monthly_cap_minor")  # absent in pre-cap rows: uncapped
    if cap is not None and (not isinstance(cap, int) or isinstance(cap, bool)):
        raise ValueError("monthly_cap_minor must be an int or absent")
    return MarginRule(
        provider=str(raw["provider"]),
        plan=str(raw["plan"]),
        location=str(raw["location"]),
        margin_factor=Decimal(str(raw["margin_factor"])),
        fixed_minor=fixed,
        monthly_cap_minor=cap,
    )
