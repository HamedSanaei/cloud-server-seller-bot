"""Operator funding domain: authorization grants, expenses and cutover evidence.

Some servers are paid for by the platform operator instead of the customer's
wallet. Operator funding is NOT customer-wallet credit and NOT a synthetic cash
payment: it records an *authorization* (a grant) before a provider cost is
incurred, and an immutable *expense* once the in-flight work is accepted. No
wallet, hold or ledger row is touched, and there is no "infinite balance".

These records are organisation-cost evidence: the immutable nominal amount the
operator authorized, plus a frozen provider-cost snapshot that keeps the
provider's native exact decimal amounts (as canonical strings) and currencies.
Original provider cost is never converted through a guessed FX rate, never a
float, and never inferred.

The module is deliberately independent of the ORM, provider adapters and the
Telegram framework: it validates the immutable financial facts and exposes the
repository port plus the typed records the persistence adapter maps.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from math import isfinite
from types import MappingProxyType
from typing import Any, Protocol
from uuid import UUID

__all__ = [
    "FundingAuthorizationError",
    "FundingError",
    "FundingFactMismatchError",
    "FundingGrantNotFoundError",
    "FundingGrantState",
    "FundingIdempotencyConflictError",
    "FundingPurpose",
    "FundingSource",
    "FundingStateError",
    "FundingValidationError",
    "OperatorExpense",
    "OperatorFundingCutover",
    "OperatorFundingGrant",
    "OperatorFundingRepository",
    "ProviderCostComponent",
    "ProviderCostSnapshot",
    "canonical_amount",
    "expense_operation_key",
]

#: PostgreSQL BIGINT columns accept a *signed* 64-bit integer; financial
#: amounts and identifiers must be validated against exactly this range.
_INT64_MAX = 2**63 - 1

#: Bounded string facts: keys that deterministic idempotency relies on must not
#: be unbounded, so a malformed/oversized key fails before it reaches storage.
MAX_KEY_LENGTH = 200
MAX_REASON_LENGTH = 1024
MAX_PROVENANCE_LENGTH = 512
MAX_LABEL_LENGTH = 128
MAX_AMOUNT_TEXT_LENGTH = 4096

_CURRENCY_RE = re.compile(r"[A-Z]{3}")
_AMOUNT_RE = re.compile(r"-?[0-9]+(?:\.[0-9]+)?", re.ASCII)


class FundingSource(StrEnum):
    """Who pays for a server's provider cost."""

    CUSTOMER_WALLET = "customer_wallet"
    OPERATOR = "operator"


class FundingPurpose(StrEnum):
    """Why an operator grant exists (the shape of the funded work)."""

    MONTHLY_CREATE = "monthly_create"
    MONTHLY_RENEW = "monthly_renew"
    HOURLY_USAGE = "hourly_usage"
    FINAL_USAGE = "final_usage"


class FundingGrantState(StrEnum):
    """Lifecycle of one operator funding grant."""

    AUTHORIZED = "authorized"
    SETTLED = "settled"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Domain errors — authorization, idempotency and fact mismatch stay distinct
# ---------------------------------------------------------------------------


class FundingError(Exception):
    """Base error for operator funding operations."""


class FundingValidationError(FundingError, ValueError):
    """An input does not describe a valid immutable funding fact."""


class FundingAuthorizationError(FundingError):
    """The actor/owner relationship does not permit the funding operation."""


class FundingIdempotencyConflictError(FundingError):
    """A deterministic operation key already exists with different facts."""


class FundingFactMismatchError(FundingError):
    """Persisted facts differ from the immutable facts an operation expected."""


class FundingStateError(FundingError):
    """The grant's state does not permit the requested transition."""


class FundingGrantNotFoundError(FundingError, LookupError):
    """The referenced grant does not exist."""


# ---------------------------------------------------------------------------
# Strict scalar validation
# ---------------------------------------------------------------------------


def _require_uuid(value: object, name: str) -> UUID:
    if not isinstance(value, UUID):
        raise FundingValidationError(f"{name} must be a UUID")
    return value


def _require_positive_int64(value: object, name: str) -> int:
    # ``type(value) is not int`` deliberately rejects ``bool`` (a bool is an int
    # subclass and must never pass as a financial amount).
    if type(value) is not int:
        raise FundingValidationError(f"{name} must be a plain integer")
    if not 0 < value <= _INT64_MAX:
        raise FundingValidationError(f"{name} must be a positive signed 64-bit integer")
    return int(value)


def _require_bounded_text(value: object, name: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise FundingValidationError(f"{name} must be a string")
    text = value.strip()
    if not text:
        raise FundingValidationError(f"{name} must not be empty")
    if len(text) > max_length:
        raise FundingValidationError(f"{name} must be at most {max_length} characters")
    return text


def _require_currency(value: object, name: str) -> str:
    if not isinstance(value, str) or _CURRENCY_RE.fullmatch(value) is None:
        raise FundingValidationError(f"{name} must be an uppercase three-letter currency code")
    return value


def _require_aware_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise FundingValidationError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise FundingValidationError(f"{name} must be timezone-aware")
    return value


def canonical_amount(value: object, name: str = "amount") -> str:
    """Canonicalize an exact decimal amount string.

    Provider prices can carry more precision than a currency's minor unit, so
    the amount is preserved as an exact base-10 string. Floats, exponent
    notation, non-finite values and negatives are rejected; trailing zeros are
    stripped so equality on the frozen snapshot is value-based, not textual.
    """
    if not isinstance(value, str):
        raise FundingValidationError(f"{name} must be an exact decimal string")
    if len(value) > MAX_AMOUNT_TEXT_LENGTH:
        raise FundingValidationError(f"{name} exceeds the exact decimal text limit")
    text = value.strip()
    if len(text) > MAX_AMOUNT_TEXT_LENGTH or _AMOUNT_RE.fullmatch(text) is None:
        raise FundingValidationError(f"{name} must be a plain base-10 decimal string")
    negative = text.startswith("-")
    unsigned = text.removeprefix("-")
    integer, _, fraction = unsigned.partition(".")
    integer = integer.lstrip("0") or "0"
    fraction = fraction.rstrip("0")
    if negative and (integer != "0" or fraction):
        raise FundingValidationError(f"{name} must not be negative")
    return f"{integer}.{fraction}" if fraction else integer


def expense_operation_key(grant_operation_key: str) -> str:
    """Stable bounded expense identity; the grant retains the original key."""
    key = _require_bounded_text(
        grant_operation_key, "grant operation_key", max_length=MAX_KEY_LENGTH
    )
    return "operator-expense:v1:" + sha256(key.encode("utf-8")).hexdigest()


def _freeze_metadata(value: Any) -> Any:
    """Copy JSON-shaped metadata recursively, retaining no mutable aliases."""
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise FundingValidationError("metadata keys must be strings")
        return MappingProxyType({key: _freeze_metadata(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_metadata(item) for item in value)
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and isfinite(value):
        return value
    raise FundingValidationError("metadata must contain finite JSON values")


# ---------------------------------------------------------------------------
# Provider-cost snapshot — exact native evidence, never a float or guessed FX
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProviderCostComponent:
    """One named component of a provider's native cost."""

    label: str
    amount_exact: str
    currency: str
    provenance: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "label",
            _require_bounded_text(self.label, "component label", max_length=MAX_LABEL_LENGTH),
        )
        object.__setattr__(
            self, "amount_exact", canonical_amount(self.amount_exact, "component amount")
        )
        object.__setattr__(self, "currency", _require_currency(self.currency, "component currency"))
        object.__setattr__(
            self,
            "provenance",
            _require_bounded_text(
                self.provenance, "component provenance", max_length=MAX_PROVENANCE_LENGTH
            ),
        )


@dataclass(frozen=True, slots=True)
class ProviderCostSnapshot:
    """Frozen provider-native cost evidence for one operator-funded grant.

    ``amount_exact``/``currency`` are the primary documented cost; optional
    ``components`` preserve a breakdown. All amounts are exact base-10 strings,
    so the provider's documented precision survives audit unchanged.
    """

    amount_exact: str
    currency: str
    provenance: str
    components: tuple[ProviderCostComponent, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "amount_exact", canonical_amount(self.amount_exact, "provider cost")
        )
        object.__setattr__(
            self, "currency", _require_currency(self.currency, "provider cost currency")
        )
        object.__setattr__(
            self,
            "provenance",
            _require_bounded_text(
                self.provenance, "provider cost provenance", max_length=MAX_PROVENANCE_LENGTH
            ),
        )
        parts = tuple(self.components)
        if any(not isinstance(part, ProviderCostComponent) for part in parts):
            raise FundingValidationError(
                "provider cost components must be ProviderCostComponent values"
            )
        object.__setattr__(self, "components", parts)

    def to_dict(self) -> dict[str, Any]:
        """JSONB-safe representation (exact amounts stay strings)."""
        return {
            "amount_exact": self.amount_exact,
            "currency": self.currency,
            "provenance": self.provenance,
            "components": [
                {
                    "label": part.label,
                    "amount_exact": part.amount_exact,
                    "currency": part.currency,
                    "provenance": part.provenance,
                }
                for part in self.components
            ],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ProviderCostSnapshot:
        """Rebuild a snapshot from stored JSONB, validating every fact."""
        if not isinstance(data, Mapping):
            raise FundingValidationError("provider cost snapshot must be a mapping")
        for field_name in ("amount_exact", "currency", "provenance"):
            if not isinstance(data.get(field_name), str):
                raise FundingValidationError(
                    f"provider cost snapshot {field_name} must be a string"
                )
        raw_components = data.get("components", ())
        if not isinstance(raw_components, (list, tuple)):
            raise FundingValidationError("provider cost snapshot components must be a list")
        components: list[ProviderCostComponent] = []
        for item in raw_components:
            if not isinstance(item, Mapping):
                raise FundingValidationError("provider cost component must be a mapping")
            for field_name in ("label", "amount_exact", "currency", "provenance"):
                if not isinstance(item.get(field_name), str):
                    raise FundingValidationError(
                        f"provider cost component {field_name} must be a string"
                    )
            components.append(
                ProviderCostComponent(
                    label=str(item["label"]),
                    amount_exact=str(item["amount_exact"]),
                    currency=str(item["currency"]),
                    provenance=str(item["provenance"]),
                )
            )
        return cls(
            amount_exact=str(data["amount_exact"]),
            currency=str(data["currency"]),
            provenance=str(data["provenance"]),
            components=tuple(components),
        )


# ---------------------------------------------------------------------------
# Immutable records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OperatorFundingGrant:
    """Authorization evidence for operator-funded provider work.

    A grant is a reservation, not a wallet credit: it authorizes the operator's
    own organisation to incur a known provider cost for one resource. It is
    self-only (``owner_user_id`` must equal ``operator_user_id``).
    """

    id: UUID
    operator_user_id: UUID
    owner_user_id: UUID
    operation_key: str
    resource_key: str
    purpose: FundingPurpose
    nominal_amount_minor: int
    nominal_currency: str
    provider_cost_snapshot: ProviderCostSnapshot
    created_at: datetime
    state: FundingGrantState = FundingGrantState.AUTHORIZED

    def __post_init__(self) -> None:
        _require_uuid(self.id, "grant id")
        _require_uuid(self.operator_user_id, "operator_user_id")
        _require_uuid(self.owner_user_id, "owner_user_id")
        if self.owner_user_id != self.operator_user_id:
            raise FundingValidationError("operator funding grants are self-only")
        object.__setattr__(
            self,
            "operation_key",
            _require_bounded_text(self.operation_key, "operation_key", max_length=MAX_KEY_LENGTH),
        )
        object.__setattr__(
            self,
            "resource_key",
            _require_bounded_text(self.resource_key, "resource_key", max_length=MAX_KEY_LENGTH),
        )
        if not isinstance(self.purpose, FundingPurpose):
            raise FundingValidationError("purpose must be a FundingPurpose")
        object.__setattr__(
            self,
            "nominal_amount_minor",
            _require_positive_int64(self.nominal_amount_minor, "nominal_amount_minor"),
        )
        object.__setattr__(
            self,
            "nominal_currency",
            _require_currency(self.nominal_currency, "nominal_currency"),
        )
        if not isinstance(self.provider_cost_snapshot, ProviderCostSnapshot):
            raise FundingValidationError("provider_cost_snapshot must be a ProviderCostSnapshot")
        if not isinstance(self.state, FundingGrantState):
            raise FundingValidationError("state must be a FundingGrantState")
        _require_aware_datetime(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class OperatorExpense:
    """Immutable organisation-cost evidence: one grant, settled exactly once.

    A settled expense is a *cost*, never customer revenue: the nominal amount
    and provider snapshot are frozen copies of the grant, so the grant may not
    be silently mutated and a later FX movement cannot rewrite accepted cost.
    """

    id: UUID
    grant_id: UUID
    operation_key: str
    owner_user_id: UUID
    operator_user_id: UUID
    resource_key: str
    purpose: FundingPurpose
    nominal_amount_minor: int
    nominal_currency: str
    provider_cost_snapshot: ProviderCostSnapshot
    occurred_at: datetime

    def __post_init__(self) -> None:
        _require_uuid(self.id, "expense id")
        _require_uuid(self.grant_id, "grant_id")
        _require_uuid(self.owner_user_id, "owner_user_id")
        _require_uuid(self.operator_user_id, "operator_user_id")
        if self.owner_user_id != self.operator_user_id:
            raise FundingValidationError("operator expenses are self-only")
        object.__setattr__(
            self,
            "operation_key",
            _require_bounded_text(self.operation_key, "operation_key", max_length=MAX_KEY_LENGTH),
        )
        object.__setattr__(
            self,
            "resource_key",
            _require_bounded_text(self.resource_key, "resource_key", max_length=MAX_KEY_LENGTH),
        )
        if not isinstance(self.purpose, FundingPurpose):
            raise FundingValidationError("purpose must be a FundingPurpose")
        object.__setattr__(
            self,
            "nominal_amount_minor",
            _require_positive_int64(self.nominal_amount_minor, "nominal_amount_minor"),
        )
        object.__setattr__(
            self,
            "nominal_currency",
            _require_currency(self.nominal_currency, "nominal_currency"),
        )
        if not isinstance(self.provider_cost_snapshot, ProviderCostSnapshot):
            raise FundingValidationError("provider_cost_snapshot must be a ProviderCostSnapshot")
        _require_aware_datetime(self.occurred_at, "occurred_at")


@dataclass(frozen=True, slots=True)
class OperatorFundingCutover:
    """Immutable receipt for converting a server to operator funding.

    No cutover is executed by this unit; this record exists so the later,
    explicitly reviewed rollout can prove exactly when and why one server
    changed funding source, with a frozen metadata snapshot.
    """

    id: UUID
    operation_key: str
    server_id: UUID
    operator_user_id: UUID
    prior_funding_source: FundingSource
    effective_at: datetime
    reason: str
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        _require_uuid(self.id, "cutover id")
        _require_uuid(self.server_id, "server_id")
        _require_uuid(self.operator_user_id, "operator_user_id")
        object.__setattr__(
            self,
            "operation_key",
            _require_bounded_text(self.operation_key, "operation_key", max_length=MAX_KEY_LENGTH),
        )
        if not isinstance(self.prior_funding_source, FundingSource):
            raise FundingValidationError("prior_funding_source must be a FundingSource")
        object.__setattr__(
            self,
            "reason",
            _require_bounded_text(self.reason, "reason", max_length=MAX_REASON_LENGTH),
        )
        _require_aware_datetime(self.effective_at, "effective_at")
        if not isinstance(self.metadata, Mapping):
            raise FundingValidationError("cutover metadata must be a mapping")
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))


# ---------------------------------------------------------------------------
# Repository port
# ---------------------------------------------------------------------------


class OperatorFundingRepository(Protocol):
    """Port for the operator funding ledger (grants + immutable expenses)."""

    async def eligible(self, owner_id: UUID) -> bool:
        """Whether the owner currently passes configured active RBAC."""
        ...

    async def reserve(
        self,
        *,
        operator_id: UUID,
        owner_id: UUID,
        operation_key: str,
        resource_key: str,
        purpose: FundingPurpose,
        nominal_amount_minor: int,
        nominal_currency: str,
        provider_cost_snapshot: ProviderCostSnapshot | Mapping[str, Any],
        reason: str | None = None,
    ) -> OperatorFundingGrant:
        """Authorize work with an audit reason; legacy callers fall back to provenance."""
        ...

    async def get_grant(self, operation_key: str) -> OperatorFundingGrant | None:
        """Internal persistence read: the grant for an operation key, if any."""
        ...

    async def get_expense(self, operation_key: str) -> OperatorExpense | None:
        """Internal persistence read: the expense for an operation key, if any."""
        ...

    async def get_cutover(self, server_id: UUID) -> OperatorFundingCutover | None:
        """Latest immutable conversion receipt, including historical classification."""
        ...

    async def settle(self, grant: OperatorFundingGrant) -> OperatorExpense:
        """Settle an authorized grant into exactly one immutable expense."""
        ...

    async def cancel(self, grant: OperatorFundingGrant, *, reason: str) -> OperatorFundingGrant:
        """Cancel an un-settled grant (never erases a settled expense)."""
        ...

    async def list_expenses_between(
        self,
        start: datetime,
        end: datetime,
        *,
        limit: int = 500,
        offset: int = 0,
    ) -> list[OperatorExpense]:
        """Bounded, stably ordered expenses whose ``occurred_at`` is in [start, end)."""
        ...
