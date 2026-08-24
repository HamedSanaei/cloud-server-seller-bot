from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.providers.errors import ProviderError


class Capability(StrEnum):
    COMPUTE = "compute"
    POWER = "power"
    REBUILD = "rebuild"
    RESCUE = "rescue"
    SNAPSHOT = "snapshot"
    BACKUP = "backup"
    FIREWALL = "firewall"
    NETWORK = "network"
    VOLUME = "volume"
    FLOATING_IP = "floating_ip"
    PRIMARY_IP = "primary_ip"
    RDNS = "rdns"


@dataclass(frozen=True, slots=True)
class ProviderLocation:
    id: str
    name: str
    country_code: str
    city: str | None = None
    network_zone: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderPlan:
    id: str
    name: str
    architecture: str
    vcpu: int
    memory_mb: int
    disk_gb: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderImage:
    id: str
    name: str
    os_family: str
    architecture: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CreateServerRequest:
    name: str
    plan_id: str
    image_id: str
    location_id: str
    ssh_key_ids: tuple[str, ...] = ()
    user_data: str | None = None
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderServer:
    id: str
    name: str
    status: str
    ipv4: str | None = None
    ipv6: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class CloudProvider(Protocol):
    key: str
    capabilities: frozenset[Capability]

    async def list_locations(self) -> list[ProviderLocation]: ...
    async def list_plans(self) -> list[ProviderPlan]: ...
    async def list_images(self) -> list[ProviderImage]: ...
    async def get_server(self, provider_server_id: str) -> ProviderServer | None: ...
    async def list_servers(self) -> list[ProviderServer]: ...
    async def create_server(
        self, request: CreateServerRequest, idempotency_key: IdempotencyKey
    ) -> ProviderServer: ...
    async def delete_server(
        self, provider_server_id: str, idempotency_key: IdempotencyKey
    ) -> None: ...
    async def power_on(self, provider_server_id: str, idempotency_key: IdempotencyKey) -> None: ...
    async def power_off(self, provider_server_id: str, idempotency_key: IdempotencyKey) -> None: ...
    async def reboot(self, provider_server_id: str, idempotency_key: IdempotencyKey) -> None: ...


def supports(provider: CloudProvider, capability: Capability) -> bool:
    """Return True if ``provider`` advertises support for ``capability``."""
    return capability in provider.capabilities


def supports_all(provider: CloudProvider, capabilities: Iterable[Capability]) -> bool:
    """Return True if ``provider`` advertises support for every capability in ``capabilities``."""
    return frozenset(capabilities).issubset(provider.capabilities)


def supports_any(provider: CloudProvider, capabilities: Iterable[Capability]) -> bool:
    """Return True if ``provider`` advertises support for any capability in ``capabilities``."""
    return not provider.capabilities.isdisjoint(capabilities)


class PowerEffectProbe(Protocol):
    """Optional capability: safely re-send an *ambiguous* power mutation.

    Providers without a native idempotency header (ArvanCloud, M15-002)
    cannot deduplicate a re-sent power call: re-sending a timed-out reboot
    would reboot a second time. Such providers implement this protocol, and
    the platform probes BEFORE re-sending a power mutation whose earlier
    attempt may already have applied:

    - ``True``  - the effect already holds; skip the provider call.
    - ``False`` - it does not; the re-send is safe.
    - ``None``  - inconclusive (still transient, or the action is not
      observable in steady state); the platform applies its conservative
      per-action default instead of guessing.

    The probe must be a read-only operation; it must never mutate.
    """

    async def probe_power_effect(self, provider_server_id: str, action: str) -> bool | None: ...


def supports_power_probe(provider: CloudProvider) -> bool:
    """Whether the provider can prove a power effect before a re-send."""
    return callable(getattr(provider, "probe_power_effect", None))


def power_probe_of(provider: CloudProvider) -> Callable[[str, str], Any] | None:
    """The provider's probe method, or None when it does not implement one.

    Typed escape hatch for the optional ``PowerEffectProbe`` capability:
    the domain (which only knows the ``CloudProvider`` port) calls this to
    obtain the method without a per-provider branch (M15-004/M15-007).
    """
    method = getattr(provider, "probe_power_effect", None)
    return method if callable(method) else None


def rebuild_support_of(provider: CloudProvider) -> Callable[[str, str], Any] | None:
    """The provider's ``rebuild_server`` method, or None (M13-002).

    Rebuild is an OPTIONAL capability: adapters that support re-imaging a
    server implement ``rebuild_server(provider_server_id, image_id)``;
    the domain probes for it instead of branching per provider.
    """
    method = getattr(provider, "rebuild_server", None)
    return method if callable(method) else None


def rescue_support_of(provider: CloudProvider) -> Callable[..., Any] | None:
    """The provider's ``enable_rescue`` method, or None (M13-003).

    Rescue mode is an OPTIONAL capability; the presence of ``enable_rescue``
    implies ``disable_rescue``. The domain probes for it instead of
    branching per provider.
    """
    enable = getattr(provider, "enable_rescue", None)
    disable = getattr(provider, "disable_rescue", None)
    if callable(enable) and callable(disable):
        return enable  # type: ignore[no-any-return]
    return None


@dataclass(frozen=True, slots=True)
class ProviderSnapshot:
    """One server snapshot (disk image) known to a provider (M13-004).

    ``size_gb`` drives COST representation: snapshot storage is billed by
    size-time through the explicit snapshot rate card (modules/pricing),
    never by hardcoded provider prices.
    """

    id: str
    description: str
    size_gb: float | None
    server_provider_id: str | None
    created_at: str | None = None


def snapshot_support_of(provider: CloudProvider) -> Callable[[str, str], Any] | None:
    """The provider's ``create_snapshot`` method, or None (M13-004).

    Snapshots are an OPTIONAL capability; its presence implies
    ``list_snapshots`` and ``delete_snapshot``.
    """
    method = getattr(provider, "create_snapshot", None)
    return method if callable(method) else None


def rdns_support_of(provider: CloudProvider) -> Callable[..., Any] | None:
    """The provider's ``set_reverse_dns`` method, or None (M13-007).

    Reverse DNS is an OPTIONAL capability:
    ``set_reverse_dns(provider_server_id, ip, ptr)`` sets (or, with
    ``ptr=None``, resets) the PTR record for one of the server's IPs. The
    domain probes for it instead of branching per provider.
    """
    method = getattr(provider, "set_reverse_dns", None)
    return method if callable(method) else None


# ---------------------------------------------------------------------------
# Payment gateway port: create / verify / refund capability model (M09-001)
# ---------------------------------------------------------------------------


class GatewayCapability(StrEnum):
    """Operations a payment gateway may advertise."""

    CREATE_PAYMENT = "create_payment"
    VERIFY_PAYMENT = "verify_payment"
    REFUND = "refund"


class PaymentStatus(StrEnum):
    """Lifecycle state of a gateway-side payment."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUNDED = "refunded"


@dataclass(frozen=True, slots=True)
class PaymentIntent:
    """Gateway representation of one payment attempt.

    Amounts are integer minor units (never float); ``currency`` is ISO-4217.
    """

    gateway_payment_id: str
    status: PaymentStatus
    amount_minor: int
    currency: str
    redirect_url: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GatewayRefund:
    """Result of a gateway-side refund."""

    refund_id: str
    gateway_payment_id: str
    amount_minor: int
    status: PaymentStatus


class UnsupportedGatewayOperation(ProviderError):
    """Raised when an operation is not in the gateway's advertised capabilities."""


def _validate_amount(amount_minor: int, currency: str) -> None:
    if amount_minor <= 0:
        raise ValueError("payment amount must be a positive integer of minor units")
    if len(currency) != 3 or not currency.isalpha() or not currency.isupper():
        raise ValueError("currency must be a 3-letter uppercase ISO-4217 code")


class PaymentGateway(Protocol):
    """Port for external payment providers.

    Every mutating operation requires an :class:`IdempotencyKey` so retries
    can never double-charge or double-refund. Adapters advertise their
    supported operations via ``capabilities``; calling an unsupported
    operation raises :class:`UnsupportedGatewayOperation`.
    """

    key: str
    capabilities: frozenset[GatewayCapability]

    async def create_payment(
        self,
        *,
        amount_minor: int,
        currency: str,
        reference: str,
        idempotency_key: IdempotencyKey,
        redirect_url: str | None = None,
    ) -> PaymentIntent: ...

    async def verify_payment(self, gateway_payment_id: str) -> PaymentIntent: ...

    async def refund(
        self,
        *,
        gateway_payment_id: str,
        amount_minor: int,
        idempotency_key: IdempotencyKey,
        reason: str = "",
    ) -> GatewayRefund: ...


def gateway_supports(gateway: PaymentGateway, capability: GatewayCapability) -> bool:
    """Return True if ``gateway`` advertises support for ``capability``."""
    return capability in gateway.capabilities


class CapabilityGatedGateway:
    """Mixin enforcing the capability model structurally.

    Adapters subclass this and override only the operations they advertise;
    calling a non-advertised operation raises before any I/O occurs. An
    advertised-but-not-overridden operation fails loudly instead of
    silently doing nothing.
    """

    key: str
    capabilities: frozenset[GatewayCapability] = frozenset()

    def _require_capability(self, capability: GatewayCapability) -> None:
        if capability not in self.capabilities:
            raise UnsupportedGatewayOperation(
                f"gateway {getattr(self, 'key', type(self).__name__)} does not "
                f"support {capability.value}"
            )

    def _validate_amount(self, amount_minor: int, currency: str) -> None:
        _validate_amount(amount_minor, currency)

    async def create_payment(
        self,
        *,
        amount_minor: int,
        currency: str,
        reference: str,
        idempotency_key: IdempotencyKey,
        redirect_url: str | None = None,
    ) -> PaymentIntent:
        self._require_capability(GatewayCapability.CREATE_PAYMENT)
        self._validate_amount(amount_minor, currency)
        raise NotImplementedError("adapter must override create_payment")  # pragma: no cover

    async def verify_payment(self, gateway_payment_id: str) -> PaymentIntent:
        self._require_capability(GatewayCapability.VERIFY_PAYMENT)
        raise NotImplementedError("adapter must override verify_payment")  # pragma: no cover

    async def refund(
        self,
        *,
        gateway_payment_id: str,
        amount_minor: int,
        idempotency_key: IdempotencyKey,
        reason: str = "",
    ) -> GatewayRefund:
        self._require_capability(GatewayCapability.REFUND)
        if amount_minor <= 0:
            raise ValueError("refund amount must be a positive integer of minor units")
        raise NotImplementedError("adapter must override refund")  # pragma: no cover
