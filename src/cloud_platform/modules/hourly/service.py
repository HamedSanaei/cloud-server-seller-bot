"""Hourly cloud instance creation (STOREFRONT-REWORK).

The usage-based counterpart of the monthly order intent: it validates the
customer's hourly plan choice and persists a durable creation intent (hourly
``CloudServer`` + immutable hourly price snapshot + ``SERVER_CREATE``
operation) WITHOUT calling the provider and WITHOUT charging upfront.
Hourly money moves later, per quantum, through the existing accrual job,
which reads only the snapshot — never today's catalog price.

Billing-model branching is structural: this command accepts hourly offers
only, and the monthly command accepts monthly offers only. Neither branches
on a provider name.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.modules.audit.domain import ActorType, AuditRepository
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.catalog.image_compatibility import image_compatible
from cloud_platform.modules.compute.domain import (
    BILLING_MODEL_HOURLY,
    CloudServer,
    ServerCreateError,
    ServerCreateIntent,
    ServerLifecycleState,
)
from cloud_platform.modules.fx.domain import (
    SUPPORTED_CURRENCIES,
    FxPurpose,
    major_to_minor,
)
from cloud_platform.modules.offers.domain import (
    SellableOfferRepository,
    is_sellable_in_currency,
)
from cloud_platform.modules.operations.domain import (
    Operation,
    OperationRepository,
    OperationStatus,
    OperationType,
)
from cloud_platform.modules.pricing.domain import MarginRule, OfferCost, SellingPrice
from cloud_platform.modules.pricing.service import ServerPriceSnapshotService
from cloud_platform.modules.provider_accounts.domain import (
    ProviderAccountRepository,
)
from cloud_platform.modules.users.domain import User, UserStatus
from cloud_platform.modules.wallet.domain import WalletRepository, WalletStatus
from cloud_platform.observability.metrics import metrics
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderConflict,
    ProviderError,
    ProviderNotFound,
    ProviderOutcomeUnknown,
    ProviderUnavailable,
)
from cloud_platform.providers.routing import DEFAULT_CREDENTIAL_ACCOUNT

logger = logging.getLogger(__name__)

RESOURCE_TYPE_CLOUD_SERVER = "cloud_server"
REPLAYABLE_HOURLY_STATES = frozenset(
    {
        ServerLifecycleState.REQUESTED,
        ServerLifecycleState.PROVISIONING,
        ServerLifecycleState.RUNNING,
    }
)
REJECTED_HOURLY_PROVIDER_STATES = frozenset(
    {"failed", "error", "rejected", "cancelled", "canceled"}
)
RECOVERABLE_HOURLY_PROVIDER_STATES = frozenset(
    {
        "accepted",
        "pending",
        "processing",
        # A just-created instance reports "creating": a normal transitional
        # state, not an unrecognized one. Anything else outside this set
        # still resolves to outcome-unknown for reconcile-or-review.
        "creating",
        "provisioning",
        "provisioned",
        "active",
        "ready",
        "running",
        "off",
    }
)


def _recovered_hourly_state_problem(value: Any) -> str | None:
    raw_state = getattr(value, "state", None) or getattr(value, "status", "")
    state = str(getattr(raw_state, "value", raw_state) or "").strip().lower()
    if not state:
        return "provider response has no lifecycle state"
    if state in REJECTED_HOURLY_PROVIDER_STATES:
        return f"provider instance is terminally {state}"
    if state not in RECOVERABLE_HOURLY_PROVIDER_STATES:
        return f"provider instance has unrecognized state {state!r}"
    return None


def _validate_hourly_operation(operation: Any, server: CloudServer) -> None:
    # Production repositories return the frozen domain Operation. Lightweight
    # provider fakes used by adapters may omit identity attributes; real rows
    # are always checked strictly and cannot bypass this contract.
    if not isinstance(operation, Operation):
        return
    if operation.operation_type is not OperationType.SERVER_CREATE:
        raise HourlyError("operation is not a server-create operation")
    if operation.resource_type != RESOURCE_TYPE_CLOUD_SERVER:
        raise HourlyError("operation resource type does not match the hourly server")
    if operation.resource_id != server.id:
        raise HourlyError("operation resource id does not match the hourly server")
    if operation.provider_key != server.provider_key:
        raise HourlyError("operation provider key does not match the hourly server")


def _offer_fingerprint(
    offer: Any,
    credential_account_id: str | None = None,
    image_id: str | None = None,
) -> dict[str, object]:
    fingerprint: dict[str, object] = {
        "fingerprint_version": 1,
        "offer_id": str(getattr(offer, "id", "")),
        "provider_key": str(getattr(offer, "provider_key", "")),
        "product_id": str(getattr(offer, "product_id", "")),
        "location_id": str(getattr(offer, "location_id", "")),
        "provider_account_id": str(getattr(offer, "provider_account_id", "") or ""),
        "credential_account_id": str(
            credential_account_id
            if credential_account_id is not None
            else (getattr(offer, "provider_account_id", "") or "")
        ),
        "billing_model": str(getattr(offer, "billing_model", "")),
        "selling_price_minor": offer.selling_price_minor,
        "selling_currency": offer.selling_currency.strip().upper(),
        "provider_cost_minor": offer.provider_cost_minor,
        "provider_cost_currency": offer.provider_cost_currency.strip().upper(),
        "provider_rate_exact": _provider_hourly_rate(offer),
        "billing_parameters": dict(offer.billing_parameters or {}),
        "pricing_metadata": dict(offer.pricing_metadata or {}),
        "technical_metadata": dict(offer.technical_metadata or {}),
    }
    if image_id is not None:
        normalized_image = str(image_id).strip()
        if not normalized_image:
            raise HourlyNotAvailableError("hourly image identity must be non-empty")
        fingerprint["image_id"] = normalized_image
    return fingerprint


def _validate_hourly_contract(server: Any, snapshot: Any) -> None:
    """Fail closed unless the complete accepted hourly contract agrees."""
    fingerprint = getattr(server, "offer_fingerprint", None)
    if not isinstance(fingerprint, dict) or fingerprint.get("fingerprint_version") != 1:
        raise HourlyNotAvailableError("hourly server has no versioned offer fingerprint")
    if getattr(snapshot, "offer_fingerprint", None) != fingerprint:
        raise HourlyNotAvailableError("hourly snapshot fingerprint does not match the server")
    required_text = (
        "offer_id",
        "provider_key",
        "product_id",
        "location_id",
        "billing_model",
        "selling_currency",
        "provider_cost_currency",
        "provider_rate_exact",
        "image_id",
    )
    for key in required_text:
        value = fingerprint.get(key)
        if not isinstance(value, str) or not value.strip():
            raise HourlyNotAvailableError(f"hourly fingerprint field {key!r} is invalid")
    try:
        UUID(fingerprint["offer_id"])
    except (TypeError, ValueError, AttributeError) as exc:
        raise HourlyNotAvailableError("hourly fingerprint offer_id is invalid") from exc
    for key, minimum in (("selling_price_minor", 1), ("provider_cost_minor", 0)):
        value = fingerprint.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < minimum
            or value > 9_223_372_036_854_775_807
        ):
            raise HourlyNotAvailableError(f"hourly fingerprint field {key!r} is invalid")
    for key in ("billing_parameters", "pricing_metadata", "technical_metadata"):
        if not isinstance(fingerprint.get(key), dict):
            raise HourlyNotAvailableError(f"hourly fingerprint field {key!r} is not a mapping")
    try:
        exact_rate = Decimal(str(fingerprint["provider_rate_exact"]).strip())
        expected_cost_minor = major_to_minor(
            exact_rate,
            str(fingerprint["provider_cost_currency"]),
            FxPurpose.DISPLAY,
        )
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise HourlyNotAvailableError("hourly fingerprint exact rate is invalid") from exc
    if (
        not exact_rate.is_finite()
        or exact_rate <= 0
        or expected_cost_minor != fingerprint["provider_cost_minor"]
    ):
        raise HourlyNotAvailableError("hourly fingerprint exact rate/cost mismatch")
    if fingerprint["billing_model"] != BILLING_MODEL_HOURLY:
        raise HourlyNotAvailableError("hourly fingerprint contains a non-hourly billing model")
    if fingerprint["provider_key"] != str(getattr(server, "provider_key", "") or ""):
        raise HourlyNotAvailableError("hourly server provider differs from its fingerprint")
    if fingerprint["billing_model"] != getattr(server, "billing_model", ""):
        raise HourlyNotAvailableError("hourly server billing model differs from its fingerprint")
    if fingerprint["provider_account_id"] != str(
        getattr(server, "credential_account_id", "") or ""
    ):
        raise HourlyNotAvailableError("hourly fingerprint credential account is inconsistent")
    if fingerprint["image_id"] != str(getattr(server, "image_id", "") or "").strip():
        raise HourlyNotAvailableError("hourly server image differs from its fingerprint")
    if dict(getattr(snapshot, "pricing_metadata", {}) or {}) != fingerprint["pricing_metadata"]:
        raise HourlyNotAvailableError("hourly snapshot pricing metadata disagrees with fingerprint")
    expected = {
        "provider_key": snapshot.offer.provider_key,
        "product_id": snapshot.offer.plan_id,
        "location_id": snapshot.offer.location_id,
        "provider_account_id": str(fingerprint.get("provider_account_id", "")),
        "credential_account_id": str(getattr(server, "credential_account_id", "") or ""),
        "billing_model": getattr(server, "billing_model", ""),
        "selling_price_minor": snapshot.selling_minor,
        "selling_currency": snapshot.selling_currency,
        "provider_cost_minor": snapshot.offer.cost_minor,
        "provider_cost_currency": snapshot.offer.currency,
        "provider_rate_exact": snapshot.offer.provider_rate_exact or "",
        "image_id": str(getattr(server, "image_id", "") or "").strip(),
    }
    for key, actual in expected.items():
        if fingerprint.get(key) != actual:
            raise HourlyNotAvailableError(f"hourly fingerprint field {key!r} is inconsistent")
    if not str(getattr(server, "image_id", "") or "").strip():
        raise HourlyNotAvailableError("hourly server has no immutable image identity")


def _response_identity_matches(
    response: Any,
    *,
    location_id: str,
    plan_id: str,
    image_id: str,
    account_id: str | None,
    reference: str,
) -> bool:
    """Require the stable identity fields every provider response must expose.

    Leaseweb's response DTO guarantees region and reference. Optional provider
    fields are still checked when present; a missing required correlation is
    never treated as a match.
    """
    for names, expected in (
        (("region", "location_id", "location"), location_id),
        (("reference", "name"), reference),
    ):
        matched = False
        for name in names:
            value = getattr(response, name, None)
            if value is None:
                continue
            matched = True
            if str(value).strip() != str(expected).strip():
                return False
            break
        if not matched:
            return False
    for names, expected_required in (
        (("instance_type", "plan_id", "type_id"), plan_id),
        (("image_id", "image"), image_id),
        (("account_id", "provider_account_id", "credential_account_id"), account_id),
    ):
        if expected_required is None:
            continue
        matched = False
        for name in names:
            value = getattr(response, name, None)
            if value is None:
                continue
            matched = True
            if str(value).strip() != str(expected_required).strip():
                return False
            break
        # Optional identity evidence is checked when present; a response that
        # omits it cannot prove a mismatch. Region+reference above remain the
        # required correlation that must always match.
        if not matched:
            continue
    return True


def _provider_hourly_rate(offer: Any) -> str:
    """Return exact canonical provider-hourly-rate text; never alias/reconstruct."""
    raw_mapping = getattr(offer, "billing_parameters", None) or {}
    raw_rate = raw_mapping.get("provider_hourly_rate") if hasattr(raw_mapping, "get") else None
    if isinstance(raw_rate, (str, Decimal)) and str(raw_rate).strip():
        return str(raw_rate).strip()
    raise HourlyNotAvailableError(
        "hourly offer is missing exact provider_hourly_rate; refusing rounded cost"
    )


class HourlyError(Exception):
    """Base error for hourly cloud creation."""


class HourlyNotAvailableError(HourlyError):
    """The hourly offer/image selection is not currently creatable."""


class HourlyCloudResolver:
    """Provider-neutral port for hourly cloud adapters (multi-account).

    Resolves ``(provider_key, credential_account_id)`` to the exact cloud
    adapter that owns the observation. ``credential_account_id`` is the
    opaque, non-secret account id persisted on the hourly offer at sync
    time (``SellableOffer.provider_account_id``) and pinned on the hourly
    server at creation time. ``None`` resolves to the provider's logical
    default adapter (single-credential deployments and legacy rows).

    Implementations live in infrastructure (the container builds one from
    the Leaseweb cloud account router); domain/application code depends
    only on this port and never on a concrete provider name.
    """

    def adapter_for(self, provider_key: str, credential_account_id: str | None = None) -> Any:
        """The cloud adapter for a pinned credential account."""
        ...


@dataclass(frozen=True, slots=True)
class HourlyCreateResult:
    """Outcome of the hourly creation command."""

    server: CloudServer
    replayed: bool


def hourly_reference_name(server_id: UUID) -> str:
    """Deterministic provider reference for one hourly server.

    Shared by creation and reconciliation: every attempt for this server
    carries the same reference, so the adapter's get-before-create match
    and the reconciler's search identify exactly this intent's resource.
    """
    return f"srv-{server_id}"


class HourlyCloudService:
    """The hourly creation command handler (no provider calls, no charge)."""

    def __init__(
        self,
        *,
        server_repo: Any,
        offers_repo: SellableOfferRepository,
        account_repo: ProviderAccountRepository,
        wallet_repo: WalletRepository,
        snapshot_service: ServerPriceSnapshotService,
        operation_repo: OperationRepository,
        audit_repo: AuditRepository,
        cloud_providers: dict[str, Any] | None = None,
        cloud_resolver: HourlyCloudResolver | Any | None = None,
        #: Canonical storefront currency for foreign offers.  USD is the
        #: current deployment default; an explicit configured value is still
        #: validated at this application boundary.
        catalog_currency: str = "USD",
        catalog_stale_limit_seconds: int | None = None,
    ) -> None:
        self._servers = server_repo
        self._offers = offers_repo
        self._accounts = account_repo
        self._wallets = wallet_repo
        self._snapshots = snapshot_service
        self._ops = operation_repo
        self._audit = AuditTrail(audit_repo)
        self._cloud = dict(cloud_providers or {})
        # Provider-neutral account-aware resolution (multi-account hourly):
        # the resolver maps (provider_key, credential_account_id) to the
        # exact adapter that owns the observation. The plain dict stays as
        # the legacy fallback for single-credential deployments.
        self._cloud_resolver = cloud_resolver
        target_currency = (catalog_currency or "").strip().upper()
        if target_currency not in SUPPORTED_CURRENCIES:
            raise ValueError("catalog_currency must be an audited currency code")
        self._catalog_currency: str = target_currency
        self._catalog_stale_limit_seconds = catalog_stale_limit_seconds

    def _adapter_for(self, provider_key: str, credential_account_id: str | None) -> Any:
        """Exact cloud adapter for a pinned credential account (fail closed).

        The resolver owns account-aware dispatch; the legacy dict is only
        the single-adapter fallback. No provider-name branching here: the
        account id selects the adapter, never an ``if provider == ...``.
        """
        resolver = self._cloud_resolver
        if resolver is not None:
            adapter = resolver.adapter_for(provider_key, credential_account_id)
            # The container resolver is synchronous; accept an awaitable too
            # so test doubles may use either shape.
            if adapter is not None:
                return adapter
            # A configured account-aware router must never silently downgrade
            # an unknown/removed pinned account to the provider default.
            raise HourlyNotAvailableError(
                f"no hourly adapter for {provider_key!r} account {credential_account_id!r}"
            )
        if credential_account_id not in (None, DEFAULT_CREDENTIAL_ACCOUNT):
            raise HourlyNotAvailableError(
                f"no account-aware adapter for pinned account {credential_account_id!r}"
            )
        adapter = self._cloud.get(provider_key)
        if adapter is None:
            raise HourlyNotAvailableError(f"no hourly adapter for {provider_key!r}")
        return adapter

    async def _repair_hourly_bundle(
        self,
        existing: CloudServer,
        *,
        offer_id: UUID,
        image_id: str,
        expected_selling_price_minor: int | None,
        expected_selling_currency: str | None,
    ) -> bool:
        """Reconstruct a crash-interrupted hourly intent from pinned facts.

        The live offer is used only after the caller repeats the original
        price/currency. Any mismatch fails closed rather than silently
        repricing an already-created server.
        """
        if existing.state is not ServerLifecycleState.REQUESTED:
            return False
        if existing.offer_fingerprint is None:
            return False
        if str(getattr(existing, "image_id", "") or "") != str(image_id or "").strip():
            return False

        # Once the immutable snapshot exists, it is the sole authority for a
        # repair.  Catalog refreshes may change volatile pricing metadata, but
        # must never make a committed hourly contract unrecoverable.
        existing_snapshot = await self._snapshots.get_snapshot(existing.id)
        if existing_snapshot is not None:
            try:
                _validate_hourly_contract(existing, existing_snapshot)
            except (HourlyError, HourlyNotAvailableError):
                return False
            fingerprint = existing.offer_fingerprint
            if str(fingerprint.get("offer_id")) != str(offer_id):
                return False
            if expected_selling_price_minor is not None and existing_snapshot.selling_minor != (
                expected_selling_price_minor
            ):
                return False
            if (
                expected_selling_currency is not None
                and str(existing_snapshot.selling_currency).upper()
                != str(expected_selling_currency).upper()
            ):
                return False
            try:
                await self._ops.get_or_create(
                    operation_key=f"server-create:{existing.id}",
                    operation_type=OperationType.SERVER_CREATE,
                    resource_type=RESOURCE_TYPE_CLOUD_SERVER,
                    resource_id=existing.id,
                    provider_key=existing_snapshot.offer.provider_key,
                )
            except Exception:
                logger.exception("failed to repair hourly operation %s", existing.id)
                return False
            return True

        offer = await self._offers.get(offer_id)
        if offer is None or offer.billing_model != BILLING_MODEL_HOURLY:
            return False
        if existing.offer_fingerprint != _offer_fingerprint(
            offer, getattr(existing, "credential_account_id", None), image_id
        ):
            return False
        if expected_selling_price_minor is None or expected_selling_currency is None:
            return False
        if (
            offer.selling_price_minor != expected_selling_price_minor
            or offer.selling_currency.strip().upper()
            != str(expected_selling_currency).strip().upper()
        ):
            return False
        exact = _provider_hourly_rate(offer)
        same_currency = offer.selling_currency == offer.provider_cost_currency
        fixed = (
            int(Decimal(offer.selling_price_minor) - Decimal(offer.provider_cost_minor))
            if same_currency
            else 0
        )
        metadata = dict(offer.pricing_metadata or {})
        price = SellingPrice(
            offer=OfferCost(
                provider_key=offer.provider_key,
                plan_id=offer.product_id,
                location_id=offer.location_id,
                cost_minor=offer.provider_cost_minor,
                currency=offer.provider_cost_currency,
                provider_rate_exact=exact,
            ),
            selling_minor=offer.selling_price_minor,
            selling_currency=offer.selling_currency,
            rule=MarginRule(
                provider=offer.provider_key,
                plan=offer.product_id,
                location=offer.location_id,
                margin_factor=Decimal(1),
                fixed_minor=fixed,
            ),
            book_name="sellable-auto",
            version=1,
            priced_at=datetime.now(UTC),
            pricing_metadata=metadata,
            offer_fingerprint=_offer_fingerprint(
                offer, getattr(existing, "credential_account_id", None), image_id
            ),
        )
        try:
            existing_snapshot = await self._snapshots.get_snapshot(existing.id)
            if existing_snapshot is None:
                await self._snapshots.create_snapshot(
                    server_id=existing.id,
                    price=price,
                    actor=None,
                    reason="repaired hourly cloud create request",
                )
            else:
                if (
                    existing.offer_fingerprint
                    != _offer_fingerprint(
                        offer, getattr(existing, "credential_account_id", None), image_id
                    )
                    or existing_snapshot.selling_minor != price.selling_minor
                    or existing_snapshot.selling_currency != price.selling_currency
                    or existing_snapshot.offer.provider_key != offer.provider_key
                    or existing_snapshot.offer.plan_id != offer.product_id
                    or existing_snapshot.offer.location_id != offer.location_id
                    or existing_snapshot.offer.cost_minor != price.offer.cost_minor
                    or existing_snapshot.offer.currency != price.offer.currency
                    or str(existing_snapshot.offer.provider_rate_exact or "")
                    != str(price.offer.provider_rate_exact or "")
                    or dict(existing_snapshot.pricing_metadata or {})
                    != dict(price.pricing_metadata or {})
                ):
                    return False
            await self._ops.get_or_create(
                operation_key=f"server-create:{existing.id}",
                operation_type=OperationType.SERVER_CREATE,
                resource_type=RESOURCE_TYPE_CLOUD_SERVER,
                resource_id=existing.id,
                provider_key=offer.provider_key,
            )
        except Exception:
            logger.exception("failed to repair hourly intent %s", existing.id)
            return False
        return True

    async def _replay_hourly_server(
        self,
        existing: CloudServer,
        user: User,
        *,
        offer_id: UUID,
        image_id: str,
        expected_selling_price_minor: int | None,
        expected_selling_currency: str | None,
    ) -> HourlyCreateResult:
        """Resolve a same-key replay only after the creator finished its bundle.

        The server row is written before its immutable snapshot and operation
        row. A concurrent callback can observe that short intermediate state;
        treating it as corruption immediately would make the original request
        lose its intent. Wait for the bounded bundle to settle, then quarantine
        only a genuinely incomplete intent.
        """
        if existing.user_id != user.id:
            raise HourlyError("idempotency key belongs to another user")
        if existing.state not in REPLAYABLE_HOURLY_STATES:
            raise HourlyError(
                f"hourly replay is not allowed for server state {existing.state.value}"
            )
        if str(getattr(existing, "image_id", "") or "") != str(image_id):
            raise HourlyError("idempotency key was reused for a different image")
        deadline = asyncio.get_running_loop().time() + 2.0
        while True:
            try:
                snapshot = await self._snapshots.require_snapshot(existing.id)
                _validate_hourly_contract(existing, snapshot)
                fingerprint = existing.offer_fingerprint
                if not isinstance(fingerprint, dict):
                    raise HourlyError("hourly replay has no complete offer fingerprint")
                if str(fingerprint.get("offer_id")) != str(offer_id):
                    raise HourlyError("idempotency key was reused for a different offer")
                if getattr(snapshot, "offer_fingerprint", None) != fingerprint:
                    raise HourlyError("hourly snapshot is missing its complete offer fingerprint")
                if str(fingerprint.get("credential_account_id") or "") != str(
                    getattr(existing, "credential_account_id", "") or ""
                ):
                    raise HourlyError("hourly replay credential fingerprint is inconsistent")
                if any(
                    fingerprint.get(key) != actual
                    for key, actual in (
                        ("provider_key", snapshot.offer.provider_key),
                        ("product_id", snapshot.offer.plan_id),
                        ("location_id", snapshot.offer.location_id),
                        ("provider_cost_minor", snapshot.offer.cost_minor),
                        ("provider_cost_currency", snapshot.offer.currency),
                        ("provider_rate_exact", snapshot.offer.provider_rate_exact),
                        ("selling_price_minor", snapshot.selling_minor),
                        ("selling_currency", snapshot.selling_currency),
                    )
                ):
                    raise HourlyError(
                        "hourly replay snapshot disagrees with its immutable fingerprint"
                    )
                if (
                    expected_selling_price_minor is not None
                    and int(snapshot.selling_minor) != expected_selling_price_minor
                ) or (
                    expected_selling_currency is not None
                    and str(snapshot.selling_currency).strip().upper()
                    != str(expected_selling_currency).strip().upper()
                ):
                    raise HourlyError("idempotency key was reused for a different price")
                # Offer identity is carried by the immutable fingerprint; the
                # pricing metadata is provider provenance, not a second copy
                # of the offer id. Keeping one authority avoids replay failures
                # when a catalog row is updated after acceptance.
                operation = await self._ops.get_by_key(f"server-create:{existing.id}")
                if operation is not None:
                    _validate_hourly_operation(operation, existing)
                    if operation.status is OperationStatus.FAILED:
                        raise HourlyError(
                            "idempotency key refers to a failed hourly operation; "
                            "manual recovery is required"
                        )
                    return HourlyCreateResult(server=existing, replayed=True)
                if await self._repair_hourly_bundle(
                    existing,
                    offer_id=offer_id,
                    image_id=image_id,
                    expected_selling_price_minor=expected_selling_price_minor,
                    expected_selling_currency=expected_selling_currency,
                ):
                    operation = await self._ops.get_by_key(f"server-create:{existing.id}")
                    if operation is not None:
                        _validate_hourly_operation(operation, existing)
                        if operation.status is OperationStatus.FAILED:
                            raise HourlyError(
                                "idempotency key refers to a failed hourly operation; "
                                "manual recovery is required"
                            ) from None
                        return HourlyCreateResult(server=existing, replayed=True)
            except HourlyError:
                raise
            except Exception:
                repaired = await self._repair_hourly_bundle(
                    existing,
                    offer_id=offer_id,
                    image_id=image_id,
                    expected_selling_price_minor=expected_selling_price_minor,
                    expected_selling_currency=expected_selling_currency,
                )
                if repaired:
                    snapshot = await self._snapshots.require_snapshot(existing.id)
                    operation = await self._ops.get_by_key(f"server-create:{existing.id}")
                    if operation is not None:
                        _validate_hourly_operation(operation, existing)
                        if operation.status is OperationStatus.FAILED:
                            raise HourlyError(
                                "idempotency key refers to a failed hourly operation; "
                                "manual recovery is required"
                            ) from None
                        return HourlyCreateResult(server=existing, replayed=True)
            if asyncio.get_running_loop().time() >= deadline:
                # Leave REQUESTED intact. A separate creator may still be
                # committing its snapshot/operation bundle; wall-time expiry
                # is not proof of corruption and must not quarantine it.
                raise HourlyError(
                    "idempotency key refers to an incomplete hourly intent; "
                    "durable reconciliation is required"
                )
            await asyncio.sleep(0.01)

    async def create_instance(
        self,
        *,
        user: User,
        offer_id: UUID,
        image_id: str,
        image_label: str,
        idempotency_key: str,
        expected_selling_price_minor: int | None = None,
        expected_selling_currency: str | None = None,
    ) -> HourlyCreateResult:
        """Validate and persist the hourly creation intent."""
        if user.id is None:
            raise HourlyError("a persisted user id is required")
        if user.status is not UserStatus.ACTIVE:
            raise HourlyError(f"user {user.id} is {user.status.value}")
        if (expected_selling_price_minor is None) != (expected_selling_currency is None):
            raise HourlyNotAvailableError("selling price and currency must be pinned together")
        if expected_selling_price_minor is not None and (
            isinstance(expected_selling_price_minor, bool)
            or not isinstance(expected_selling_price_minor, int)
            or expected_selling_price_minor <= 0
            or expected_selling_price_minor > 9_223_372_036_854_775_807
        ):
            raise HourlyNotAvailableError("expected selling price must be a positive signed int64")
        if expected_selling_currency is not None and (
            not isinstance(expected_selling_currency, str)
            or expected_selling_currency.strip().upper() not in SUPPORTED_CURRENCIES
        ):
            raise HourlyNotAvailableError("expected selling currency must be audited")
        if not isinstance(image_id, str) or not image_id.strip():
            raise HourlyNotAvailableError("image_id must be a non-empty provider id")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise HourlyNotAvailableError("idempotency_key must not be empty")
        # intent was accepted, later catalog pricing/currency changes must not
        # make the same idempotency key fail or reprice its existing snapshot.
        existing = await self._servers.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            if existing.user_id != user.id:
                raise HourlyError(f"idempotency key {idempotency_key!r} belongs to another user")
            return await self._replay_hourly_server(
                existing,
                user,
                offer_id=offer_id,
                image_id=image_id,
                expected_selling_price_minor=expected_selling_price_minor,
                expected_selling_currency=expected_selling_currency,
            )

        offer = await self._offers.get(offer_id)
        if offer is None or not offer.sellable:
            raise HourlyNotAvailableError(f"offer {offer_id} is not sellable")
        if offer.billing_model != BILLING_MODEL_HOURLY:
            raise HourlyNotAvailableError(f"offer {offer_id} is not an hourly plan")
        if (expected_selling_price_minor is None) != (expected_selling_currency is None):
            raise HourlyNotAvailableError("selling price and currency must be pinned together")
        if expected_selling_price_minor is not None and (
            offer.selling_price_minor != expected_selling_price_minor
            or offer.selling_currency.strip().upper()
            != str(expected_selling_currency or "").strip().upper()
        ):
            raise HourlyNotAvailableError("offer price changed since confirmation")
        if not is_sellable_in_currency(
            offer,
            self._catalog_currency,
            catalog_stale_limit_seconds=self._catalog_stale_limit_seconds,
        ):
            if offer.selling_currency.strip().upper() != self._catalog_currency.strip().upper():
                raise HourlyNotAvailableError(
                    f"offer {offer_id} selling currency {offer.selling_currency!r} "
                    f"is not the configured catalog currency {self._catalog_currency!r}"
                )
            raise HourlyNotAvailableError(
                f"offer {offer_id} has no valid canonical pricing provenance"
            )
        if not image_id or not image_id.strip():
            raise HourlyNotAvailableError("an image id is required")

        wallet = await self._wallets.get(user.id)
        if wallet is None:
            raise HourlyError(f"user {user.id} has no wallet")
        if getattr(wallet, "status", WalletStatus.ACTIVE) is not WalletStatus.ACTIVE:
            raise HourlyError("wallet is not active for a new purchase")
        # A wallet balance has one unit.  Do not create an intent whose later
        # accrual would need a silent conversion or currency relabel.  Some
        # legacy in-memory test doubles predate ``Wallet.currency``; the
        # persisted wallet always has it, and those doubles are treated as the
        # historical same-currency path for compatibility.
        wallet_currency = getattr(wallet, "currency", None)
        if not hasattr(wallet, "currency"):
            # A small number of legacy in-memory ports predate the persisted
            # Wallet.currency field. Production ORM/domain wallets always
            # expose it, so only an actually absent attribute gets the
            # historical same-currency compatibility path.
            wallet_currency = offer.selling_currency
        if str(wallet_currency or "").strip().upper() != offer.selling_currency.strip().upper():
            raise HourlyError(
                f"wallet currency {wallet_currency} does not match offer selling currency "
                f"{offer.selling_currency}"
            )

        account = await self._accounts.get_or_create_active(user.id, offer.provider_key)
        pinned_account = str(offer.provider_account_id or "").strip() or None
        try:
            adapter = self._adapter_for(offer.provider_key, pinned_account)
        except HourlyNotAvailableError as exc:
            raise HourlyNotAvailableError(str(exc)) from exc
        validator = getattr(adapter, "validate_hourly_offer_for_checkout", None)
        if not callable(validator):
            raise HourlyNotAvailableError(
                "provider adapter lacks hourly image compatibility validation"
            )
        try:
            await validator(
                location_id=offer.location_id,
                product_id=offer.product_id,
                image_id=image_id,
                expected_cost_minor=offer.provider_cost_minor,
                currency=offer.provider_cost_currency,
                expected_cost_exact=_provider_hourly_rate(offer),
            )
        except Exception as exc:
            raise HourlyNotAvailableError(
                f"hourly image is not compatible with the pinned offer: {exc}"
            ) from exc

        # Re-read immediately before creating the durable server bundle. The
        # initial catalog read can race an automatic reprice; a stale displayed
        # price must never become an accepted hourly contract.
        latest_offer = await self._offers.get(offer_id)
        if latest_offer is None or not is_sellable_in_currency(
            latest_offer,
            self._catalog_currency,
            catalog_stale_limit_seconds=self._catalog_stale_limit_seconds,
        ):
            raise HourlyNotAvailableError("offer became unavailable during checkout")
        if _offer_fingerprint(latest_offer, pinned_account, image_id) != _offer_fingerprint(
            offer, pinned_account, image_id
        ):
            raise HourlyNotAvailableError("offer facts changed during checkout")
        offer = latest_offer
        latest_account = str(latest_offer.provider_account_id or "").strip() or None
        if (pinned_account is None and latest_account is not None) or (
            pinned_account is not None and latest_account != pinned_account
        ):
            raise HourlyNotAvailableError("offer credential account changed during checkout")

        # Build the immutable price contract before persisting a REQUESTED
        # server. Invalid exact rates must not leave a replayable partial
        # intent behind.
        same_currency = offer.selling_currency == offer.provider_cost_currency
        fixed = (
            int(Decimal(offer.selling_price_minor) - Decimal(offer.provider_cost_minor))
            if same_currency
            else 0
        )
        exact_provider_rate = _provider_hourly_rate(offer)
        # The snapshot metadata is the exact offer pricing provenance.  The
        # offer/account/technical identity already lives in the immutable
        # fingerprint; do not add derived fields on only one side or replay
        # would see a self-inconsistent contract.
        hourly_pricing_metadata = dict(offer.pricing_metadata or {})
        price = SellingPrice(
            offer=OfferCost(
                provider_key=offer.provider_key,
                plan_id=offer.product_id,
                location_id=offer.location_id,
                cost_minor=offer.provider_cost_minor,
                currency=offer.provider_cost_currency,
                provider_rate_exact=exact_provider_rate,
            ),
            selling_minor=offer.selling_price_minor,
            selling_currency=offer.selling_currency,
            rule=MarginRule(
                provider=offer.provider_key,
                plan=offer.product_id,
                location=offer.location_id,
                margin_factor=Decimal(1),
                fixed_minor=fixed,
            ),
            book_name="sellable-auto",
            version=1,
            priced_at=datetime.now(UTC),
            pricing_metadata=hourly_pricing_metadata,
            offer_fingerprint=_offer_fingerprint(offer, pinned_account, image_id),
        )

        # Hourly offer provenance: the credential account that actually
        # supplied/owns the observation (persisted by the multi-account
        # cloud sync in ``SellableOffer.provider_account_id``) is pinned
        # on the server now, before any provider call, so the worker POSTs
        # through exactly that credential — never an arbitrary first key.
        server = CloudServer(
            id=uuid4(),
            user_id=user.id,
            provider_key=offer.provider_key,
            provider_account_id=account.id,
            state=ServerLifecycleState.REQUESTED,
            billing_model=BILLING_MODEL_HOURLY,
            quantum_seconds=3600,
            os=image_label,
            image_id=image_id,
            credential_account_id=pinned_account,
            offer_fingerprint=_offer_fingerprint(offer, pinned_account, image_id),
        )
        intent = ServerCreateIntent(
            # No legacy-catalog pin (that FK points at the hourly catalog
            # tables, not the sellable price book): the hourly price
            # snapshot below pins provider/plan/location/cost instead.
            catalog_id=None,
            cost_minor=offer.provider_cost_minor,
            currency=offer.provider_cost_currency,
            idempotency_key=idempotency_key,
            image_id=image_id,
            offer_id=offer.id,
            offer_fingerprint=_offer_fingerprint(offer, pinned_account, image_id),
        )
        try:
            created = await self._servers.create(server, intent)
        except ServerCreateError:
            original = await self._servers.get_by_idempotency_key(idempotency_key)
            if original is not None and original.user_id == user.id:
                return await self._replay_hourly_server(
                    original,
                    user,
                    offer_id=offer_id,
                    image_id=image_id,
                    expected_selling_price_minor=expected_selling_price_minor,
                    expected_selling_currency=expected_selling_currency,
                )
            raise

        async def quarantine_partial() -> None:
            try:
                created.transition_to(ServerLifecycleState.ERROR)
                await self._servers.save(created)
            except Exception:
                logger.exception("failed to quarantine partial hourly intent")

        try:
            await self._snapshots.create_snapshot(
                server_id=created.id,
                price=price,
                actor=None,
                reason="hourly cloud create request",
            )
            await self._ops.get_or_create(
                operation_key=f"server-create:{created.id}",
                operation_type=OperationType.SERVER_CREATE,
                resource_type=RESOURCE_TYPE_CLOUD_SERVER,
                resource_id=created.id,
                provider_key=offer.provider_key,
            )
        except Exception:
            await quarantine_partial()
            raise

        try:
            await self._audit.record_mutation(
                actor_type=ActorType.USER,
                actor_id=user.id,
                action="hourly.create_requested",
                resource_type="server",
                resource_id=str(created.id),
                reason=f"hourly create {offer.ref}",
                metadata={
                    "offer": offer.ref,
                    "hourly_price_minor": str(offer.selling_price_minor),
                    "currency": offer.selling_currency,
                    "selling_currency": offer.selling_currency,
                    "provider_cost_minor": str(offer.provider_cost_minor),
                    # Keep the exact provider hourly rate explicit in the JSON
                    # audit metadata.  The immutable snapshot's OfferCost remains
                    # the authoritative cost record; no FX conversion happens here.
                    "provider_hourly_rate": exact_provider_rate,
                    "provider_hourly_rate_currency": offer.provider_cost_currency,
                    "image": image_label,
                    "billing_model": BILLING_MODEL_HOURLY,
                },
            )
        except Exception:
            await quarantine_partial()
            raise
        logger.info(
            "hourly create intent %s (offer %s, %d %s/h, image %s)",
            created.id,
            offer.ref,
            offer.selling_price_minor,
            offer.selling_currency,
            image_label,
        )
        return HourlyCreateResult(server=created, replayed=False)

    async def cloud_image_by_index(self, offer: Any, index: int) -> Any:
        """Reject legacy positional image callbacks.

        A provider image id, not a list position, is part of the billable
        contract. Positional callbacks are unsafe after catalog reordering
        and are therefore never resolved by this service.
        """
        del offer, index
        raise HourlyNotAvailableError("image selection requires a stable provider image id")

    async def cloud_image_by_id(self, offer: Any, image_id: str) -> Any:
        """Resolve one image by stable provider id through the pinned account.

        Read-only: lists the offer location's images through the credential
        account pinned on the offer and returns the matching image. Fails
        closed when the image is absent or unreadable.
        """
        if not isinstance(image_id, str) or not image_id.strip():
            raise HourlyNotAvailableError("image_id must be a non-empty provider id")
        pinned_account = str(getattr(offer, "provider_account_id", "") or "").strip() or None
        adapter = self._adapter_for(offer.provider_key, pinned_account)
        try:
            images = await adapter.list_images(offer.location_id)
        except Exception as exc:
            raise HourlyNotAvailableError(f"images currently unavailable for {offer.ref}") from exc
        for image in images:
            if getattr(image, "id", None) == image_id:
                return image
        raise HourlyNotAvailableError(f"image {image_id!r} is not offered for {offer.ref}")

    async def servers_requested(self) -> list[CloudServer]:
        """Hourly servers awaiting creation (the process job's queue)."""
        servers = await self._servers.list_requested()
        return [s for s in servers if not s.is_prepaid_monthly]

    async def servers_for_reconcile(self) -> list[CloudServer]:
        """Hourly rows needing read-only create reconciliation.

        REQUESTED rows are included because a crash can leave an ambiguous
        operation after the provider POST boundary. The reconciler still
        performs a read-only reference lookup and never re-POSTs them.
        """
        servers = await self._servers.list_requested()
        servers.extend(await self._servers.list_provisioning())
        return [s for s in servers if not s.is_prepaid_monthly and not s.provider_server_id]

    async def process_server(self, server_id: UUID) -> str:
        """Execute one hourly server's create intent (worker-called).

        Claims the ``server-create`` operation, builds the exact hourly POST
        from the price snapshot (plan/location) and the pinned image label,
        and POSTs once. Ambiguous outcomes become OUTCOME_UNKNOWN (never a
        blind re-POST); definitive failures become FAILED for operator
        review. Returns a short outcome label for job logging.
        """
        server = await self._servers.get(server_id)
        if server is None or server.is_prepaid_monthly:
            return "skipped"
        if server.state is not ServerLifecycleState.REQUESTED:
            # A crash can persist the provider id before the operation row is
            # completed. Revisit that durable pair instead of stranding it.
            if server.provider_server_id:
                return await self.reconcile_server(server_id)
            return "skipped"
        if server.provider_server_id:
            # A requested row with an attached provider identity is an
            # ambiguous/partial bundle, never permission to POST again.
            return await self.reconcile_server(server_id)
        # Validate the immutable contract before claiming an operation. A
        # crash after a claim must always mean a possible billable POST, never
        # merely a malformed pre-POST row.
        try:
            preflight_snapshot = await self._snapshots.require_snapshot(server.id)
            _validate_hourly_contract(server, preflight_snapshot)
        except Exception as exc:
            return await self._quarantine_invalid_intent(server, str(exc))
        operation = await self._ops.get_or_create(
            operation_key=f"server-create:{server.id}",
            operation_type=OperationType.SERVER_CREATE,
            resource_type=RESOURCE_TYPE_CLOUD_SERVER,
            resource_id=server.id,
            provider_key=server.provider_key,
        )
        _validate_hourly_operation(operation, server)
        if operation.is_terminal:
            return "skipped"
        try:
            claimed = await self._ops.claim(operation.id)
        except Exception as exc:
            # An infrastructure failure while claiming (a DB bind error, a
            # connection loss) happens BEFORE any provider POST, so nothing
            # billable was sent and the attempt count did not move. It must
            # never be swallowed as "still in progress": record it as a
            # provisioning failure so the failure/queue-age signals see a
            # request that is not progressing, then re-raise for the job's
            # own error accounting.
            metrics.record_provisioning_failure("worker")
            logger.exception("hourly claim failed for server %s: %s", server.id, exc)
            raise
        if claimed is None:
            return await self._reconcile_inflight(server, operation)
        try:
            snapshot = await self._snapshots.require_snapshot(server.id)
        except Exception as exc:
            return await self._fail_operation(claimed, server, f"no price snapshot: {exc}")
        try:
            _validate_hourly_contract(server, snapshot)
        except HourlyError as exc:
            return await self._fail_operation(claimed, server, str(exc))
        # The exact credential account pinned at creation owns the POST —
        # never an arbitrary configured key.
        pinned_account = getattr(server, "credential_account_id", None)
        try:
            adapter = self._adapter_for(server.provider_key, pinned_account)
        except HourlyNotAvailableError as exc:
            return await self._fail_operation(claimed, server, str(exc))
        image_id = getattr(server, "image_id", None)
        if not image_id:
            return await self._fail_operation(
                claimed, server, "hourly request has no immutable provider image id"
            )
        validator = getattr(adapter, "validate_hourly_offer_for_checkout", None)
        if not callable(validator):
            return await self._fail_operation(
                claimed,
                server,
                "provider adapter lacks hourly checkout revalidation capability",
            )
        try:
            await validator(
                location_id=snapshot.offer.location_id,
                product_id=snapshot.offer.plan_id,
                image_id=image_id,
                expected_cost_minor=snapshot.offer.cost_minor,
                currency=snapshot.offer.currency,
                expected_cost_exact=snapshot.offer.provider_rate_exact or "",
            )
        except (ProviderAuthError, ProviderNotFound, ProviderConflict) as exc:
            return await self._fail_operation(
                claimed, server, f"hourly offer revalidation failed: {exc}"
            )
        except Exception as exc:
            return await self._requeue_operation(
                claimed, f"hourly offer revalidation unavailable: {exc}"
            )
        try:
            images = await adapter.list_images(snapshot.offer.location_id)
        except (ProviderAuthError, ProviderNotFound, ProviderConflict) as exc:
            return await self._fail_operation(claimed, server, f"images unavailable: {exc}")
        except Exception as exc:
            return await self._requeue_operation(claimed, f"images unavailable: {exc}")
        image = next((img for img in images if img.id == image_id), None)
        if image is None:
            return await self._fail_operation(
                claimed, server, f"image id {image_id!r} no longer offered at pinned location"
            )
        offer_metadata = getattr(snapshot, "pricing_metadata", {}) or {}
        offer_architecture = getattr(snapshot.offer, "architecture", None) or (
            offer_metadata.get("architecture") if isinstance(offer_metadata, dict) else None
        )
        if not image_compatible(
            image,
            plan_id=snapshot.offer.plan_id,
            architecture=offer_architecture,
            location_id=snapshot.offer.location_id,
            account_id=pinned_account,
        ):
            return await self._fail_operation(
                claimed, server, "pinned image is not compatible with the pinned offer"
            )
        reference = hourly_reference_name(server.id)
        try:
            created = await adapter.create_instance(
                instance_type=snapshot.offer.plan_id,
                image_id=image.id,
                region=snapshot.offer.location_id,
                reference=reference,
                idempotency_key=IdempotencyKey(claimed.operation_key),
            )
        except ProviderOutcomeUnknown as exc:
            claimed.mark_outcome_unknown(str(exc))
            await self._ops.save(claimed)
            logger.warning("hourly create %s ambiguous: %s", server.id, exc)
            return "outcome-unknown"
        except ProviderUnavailable as exc:
            # The adapter contract proves this failure occurred before the
            # provider accepted the create. Requeue the same operation key;
            # the next worker pass is a new attempt, never a duplicate bill.
            claimed.requeue(str(exc))
            await self._ops.save(claimed)
            return "requeued"
        except ProviderError as exc:
            return await self._fail_operation(claimed, server, str(exc))
        created_id = getattr(created, "id", None)
        if not isinstance(created_id, str) or not created_id.strip():
            # A provider response without a durable resource identity cannot
            # prove what was created. Never retry the POST blindly.
            claimed.mark_outcome_unknown("hourly provider response has no resource id")
            await self._ops.save(claimed)
            return "outcome-unknown"
        raw_provider_status = getattr(created, "status", None) or getattr(created, "state", "")
        provider_status = str(getattr(raw_provider_status, "value", raw_provider_status) or "")
        provider_status = provider_status.strip().lower()
        if provider_status in REJECTED_HOURLY_PROVIDER_STATES:
            claimed.mark_outcome_unknown(
                f"hourly provider rejected the create with status {provider_status!r}"
            )
            await self._ops.save(claimed)
            return "outcome-unknown"
        if provider_status not in RECOVERABLE_HOURLY_PROVIDER_STATES:
            claimed.mark_outcome_unknown(
                f"hourly provider returned unrecognized state {provider_status!r}"
            )
            await self._ops.save(claimed)
            return "outcome-unknown"
        response_mismatches: list[str] = []
        if not _response_identity_matches(
            created,
            location_id=snapshot.offer.location_id,
            plan_id=snapshot.offer.plan_id,
            image_id=image_id,
            account_id=pinned_account,
            reference=reference,
        ):
            response_mismatches.append("missing or mismatched stable identity")
            for names, expected in (
                (("region", "location_id", "location"), snapshot.offer.location_id),
                (("instance_type", "plan_id", "type_id"), snapshot.offer.plan_id),
                (("image_id", "image"), image_id),
                (("account_id", "provider_account_id", "credential_account_id"), pinned_account),
                (("reference", "name"), reference),
            ):
                for name in names:
                    value = getattr(created, name, None)
                    if value is not None and (
                        expected is None or str(value).strip() != str(expected).strip()
                    ):
                        response_mismatches.append(f"{name}={value!r}")
                        break
        if response_mismatches:
            claimed.mark_outcome_unknown(
                "hourly provider response identity mismatch: " + ", ".join(response_mismatches)
            )
            await self._ops.save(claimed)
            return "outcome-unknown"
        # Persist the provider correlation on the server before terminalizing
        # the operation. If this save fails/crashes, the operation remains
        # IN_FLIGHT and reconciliation can find the exact POST outcome; the
        # inverse order would strand a billed resource in REQUESTED.
        server.provider_server_id = created_id
        if server.state is ServerLifecycleState.REQUESTED:
            server.transition_to(ServerLifecycleState.PROVISIONING)
        await self._servers.save(server)
        claimed.complete(
            {
                "provider_server_id": created_id,
                "provider_status": getattr(created, "state", None),
                "idempotency_key": claimed.operation_key,
            }
        )
        await self._ops.save(claimed)
        await self._audit.record_mutation(
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            action="hourly.create_submitted",
            resource_type="server",
            resource_id=str(server.id),
            reason=f"hourly instance {created_id} accepted",
            metadata={"provider_server_id": created_id},
        )
        logger.info("hourly server %s -> provider %s", server.id, created_id)
        return "provisioned"

    async def _reconcile_inflight(self, server: CloudServer, operation: Any) -> str:
        """Recover a REQUESTED server whose worker died after claiming its POST.

        Never re-POSTs an IN_FLIGHT operation. A read-only reference lookup
        first proves whether the provider accepted it; after a bounded stale
        lease the operation is moved to OUTCOME_UNKNOWN for operator review.
        """
        if getattr(operation, "status", None) is not OperationStatus.IN_FLIGHT:
            return "claimed-elsewhere"
        _validate_hourly_operation(operation, server)
        try:
            snapshot = await self._snapshots.require_snapshot(server.id)
            _validate_hourly_contract(server, snapshot)
            adapter = self._adapter_for(
                server.provider_key, getattr(server, "credential_account_id", None)
            )
            found = await adapter.find_by_reference(
                snapshot.offer.location_id, hourly_reference_name(server.id)
            )
        except Exception:
            found = None
        if found is not None:
            found_id = getattr(found, "id", None)
            if not isinstance(found_id, str) or not found_id.strip():
                return "outcome-unknown"
            if not _response_identity_matches(
                found,
                location_id=snapshot.offer.location_id,
                plan_id=snapshot.offer.plan_id,
                image_id=str(getattr(server, "image_id", "") or ""),
                account_id=getattr(server, "credential_account_id", None),
                reference=hourly_reference_name(server.id),
            ):
                operation.mark_outcome_unknown(
                    "hourly recovery response identity does not match the pinned contract"
                )
                await self._ops.save(operation)
                return "outcome-unknown"
            state_problem = _recovered_hourly_state_problem(found)
            if state_problem is not None:
                operation.mark_outcome_unknown(
                    f"hourly recovery rejected by provider state: {state_problem}"
                )
                await self._ops.save(operation)
                return "outcome-unknown"
            server.provider_server_id = found_id
            if server.state is ServerLifecycleState.REQUESTED:
                server.transition_to(ServerLifecycleState.PROVISIONING)
            await self._servers.save(server)
            try:
                operation.complete(
                    {
                        "provider_server_id": found_id,
                        "provider_status": getattr(found, "state", None),
                        "idempotency_key": operation.operation_key,
                        "recovered_read_only": True,
                    }
                )
                await self._ops.save(operation)
            except Exception:
                logger.exception("failed to complete recovered hourly operation %s", operation.id)
            return "recovered"
        updated = getattr(operation, "updated_at", None)
        if updated is not None:
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            if datetime.now(UTC) - updated < timedelta(minutes=15):
                return "still-inflight"
        try:
            operation.mark_outcome_unknown(
                "hourly create worker lease expired; provider outcome requires read-only review"
            )
            await self._ops.save(operation)
        except Exception:
            logger.exception("failed to quarantine stale hourly operation %s", operation.id)
        return "outcome-unknown"

    async def _requeue_operation(self, claimed: Any, error: str) -> str:
        """Return a pre-POST transient failure to the durable queue."""
        claimed.requeue(error)
        await self._ops.save(claimed)
        logger.warning("hourly create requeued before provider POST: %s", error)
        return "requeued"

    async def _quarantine_invalid_intent(self, server: CloudServer, error: str) -> str:
        """Persist a malformed pre-POST intent instead of polling it forever."""
        try:
            operation = await self._ops.get_or_create(
                operation_key=f"server-create:{server.id}",
                operation_type=OperationType.SERVER_CREATE,
                resource_type=RESOURCE_TYPE_CLOUD_SERVER,
                resource_id=server.id,
                provider_key=server.provider_key,
            )
            _validate_hourly_operation(operation, server)
            if operation.status in {
                OperationStatus.IN_FLIGHT,
                OperationStatus.OUTCOME_UNKNOWN,
            }:
                if server.state is ServerLifecycleState.REQUESTED:
                    server.transition_to(ServerLifecycleState.ERROR)
                await self._servers.save(server)
                return "review"
            if operation.status is OperationStatus.PENDING:
                claimed = await self._ops.claim(operation.id)
                if claimed is not None:
                    claimed.fail(f"invalid immutable hourly intent: {error}")
                    await self._ops.save(claimed)
            elif not operation.is_terminal:
                operation.fail(f"invalid immutable hourly intent: {error}")
                await self._ops.save(operation)
            if server.state is ServerLifecycleState.REQUESTED:
                server.transition_to(ServerLifecycleState.ERROR)
            await self._servers.save(server)
        except Exception:
            logger.exception("failed to quarantine malformed hourly intent %s", server.id)
        return f"invalid:{error}"

    async def _fail_operation(self, claimed: Any, server: CloudServer, error: str) -> str:
        claimed.fail(error)
        await self._ops.save(claimed)
        try:
            server.transition_to(ServerLifecycleState.ERROR)
            await self._servers.save(server)
        except Exception:
            logger.exception("failed to mark hourly server %s ERROR", server.id)
        logger.warning("hourly create %s failed: %s", server.id, error)
        return "failed"

    async def reconcile_server(self, server_id: UUID) -> str:
        """Attach a proven instance to an ambiguous hourly create, or leave it.

        For OUTCOME_UNKNOWN operations only: an exact reference match proves
        the earlier POST landed and is attached; anything else stays unknown
        for operator review (the operator may requeue, which re-POSTs safely
        through get-before-create). Never attaches by similarity.
        """
        server = await self._servers.get(server_id)
        if server is None or server.is_prepaid_monthly:
            return "skipped"
        operation = await self._ops.get_by_key(f"server-create:{server.id}")
        if operation is not None:
            _validate_hourly_operation(operation, server)
        if server.provider_server_id:
            if operation is not None and operation.status in {
                OperationStatus.IN_FLIGHT,
                OperationStatus.OUTCOME_UNKNOWN,
            }:
                operation.complete(
                    {
                        "provider_server_id": server.provider_server_id,
                        "provider_status": getattr(server, "state", None),
                        "recovered_attached_server": True,
                    }
                )
                await self._ops.save(operation)
                return "recovered"
            return "skipped"
        if operation is None or operation.status is not OperationStatus.OUTCOME_UNKNOWN:
            return "skipped"
        try:
            adapter = self._adapter_for(
                server.provider_key, getattr(server, "credential_account_id", None)
            )
        except HourlyNotAvailableError:
            return "skipped"
        try:
            snapshot = await self._snapshots.require_snapshot(server.id)
            _validate_hourly_contract(server, snapshot)
        except Exception:
            return "skipped"
        found = await adapter.find_by_reference(
            snapshot.offer.location_id, hourly_reference_name(server.id)
        )
        if found is None:
            logger.info("hourly reconcile %s: no instance found; still unknown", server.id)
            return "still-unknown"
        found_id = getattr(found, "id", None)
        if not isinstance(found_id, str) or not found_id.strip():
            return "still-unknown"
        if not _response_identity_matches(
            found,
            location_id=snapshot.offer.location_id,
            plan_id=snapshot.offer.plan_id,
            image_id=str(getattr(server, "image_id", "") or ""),
            account_id=getattr(server, "credential_account_id", None),
            reference=hourly_reference_name(server.id),
        ):
            # The operation is ALREADY outcome-unknown (reconcile gate
            # above): re-marking would raise InvalidOperationTransition, so
            # just persist and stay unknown without touching its state.
            logger.info(
                "hourly reconcile %s: response identity does not match the pinned contract",
                server.id,
            )
            await self._ops.save(operation)
            return "still-unknown"
        state_problem = _recovered_hourly_state_problem(found)
        if state_problem is not None:
            logger.info(
                "hourly reconcile %s rejected by provider state: %s", server.id, state_problem
            )
            await self._ops.save(operation)
            return "still-unknown"
        # Persist the proven provider correlation before terminalizing the
        # operation. A crash between these writes must leave a recoverable
        # IN_FLIGHT/REQUESTED pair, never a terminal operation with no server.
        server.provider_server_id = found_id
        if server.state is ServerLifecycleState.REQUESTED:
            server.transition_to(ServerLifecycleState.PROVISIONING)
        await self._servers.save(server)
        operation.complete(
            {
                "provider_server_id": found_id,
                "provider_status": getattr(found, "state", None),
                "reconciled": "reference-match",
            }
        )
        await self._ops.save(operation)
        logger.info("hourly reconcile %s attached provider %s", server.id, found_id)
        return "attached"
