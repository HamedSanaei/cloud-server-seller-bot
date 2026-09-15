"""Customer server-management application service (Telegram My Servers).

This is the ONLY place that turns a Telegram intent into provider work. It owns
everything the spec requires of the application layer:

- **ownership** — every entry point re-loads the local row and compares
  ``user_id``; a missing server and another customer's server are
  indistinguishable (one uniform ``ServerNotFoundError``), so the bot cannot be
  used to enumerate servers;
- **authorization/policy** — :mod:`~cloud_platform.modules.servers.policies`
  decides what the deployment exposes and what the state allows;
- **provider capability detection** — the adapter is probed structurally, so no
  provider name is ever compared here;
- **confirmation** — destructive operations require a consumed one-time token
  bound to (customer, server, operation, arguments, expiry);
- **idempotency** — power goes through the existing operation ledger; the
  destructive paths derive their key from the consumed confirmation nonce, so a
  replay can never mutate twice;
- **ambiguity** — a provider outcome that cannot be proven NEVER triggers a
  re-send: it is reported as ``outcome_unknown`` (safest wording) and logged;
- **audit + business events** — every mutation is audited and, where useful,
  emitted to the durable business-log outbox. Logging can never block a
  mutation (``emit_safe``).

The service never returns a provider DTO: callers receive the customer-safe
records in :mod:`cloud_platform.modules.servers.models`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from cloud_platform.modules.audit.domain import ActorType, AuditRepository
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.businesslog.domain import (
    BusinessEventSink,
    BusinessEventType,
    emit_safe,
)
from cloud_platform.modules.businesslog.events import (
    server_management_event,
    server_operation_failed_event,
    service_renewal_event,
)
from cloud_platform.modules.compute.domain import (
    CloudServer,
    ServerLifecycleState,
    ServerRepository,
)
from cloud_platform.modules.operations.service import (
    NotServerOwnerError,
    PowerActionNotAllowedError,
    PowerCommandError,
    PowerCommandService,
    PowerOutcomeUnknownError,
)
from cloud_platform.modules.servers import policies
from cloud_platform.modules.servers.confirmations import (
    ConfirmationBinding,
    ConfirmationStatus,
    ConfirmationVerifier,
)
from cloud_platform.modules.servers.models import (
    BILLABLE_SERVER_OPERATIONS,
    CustomerServerPage,
    CustomerServerView,
    IpAddressView,
    MonitoringView,
    ReinstallImageView,
    ServerActionOutcome,
    ServerConsoleView,
    ServerOperation,
    ServerRenameView,
    ServerRenewalView,
    ServerSnapshotView,
    TrafficUsageView,
    format_bytes,
)
from cloud_platform.modules.servers.policies import ServerManagementPolicy
from cloud_platform.providers.errors import ProviderError, ProviderOutcomeUnknown
from cloud_platform.providers.registry import ProviderRegistry
from cloud_platform.providers.routing import provider_for
from cloud_platform.providers.vps_ports import (
    ConsoleSession,
    DataTrafficUsage,
    VpsCapabilities,
    VpsInfo,
    VpsIpRecord,
    vps_capabilities_of,
)

logger = logging.getLogger(__name__)

__all__ = [
    "RESOURCE_TYPE_SERVER",
    "RenewalCollector",
    "ServerAmbiguousOutcomeError",
    "ServerConfirmationError",
    "ServerManagementError",
    "ServerManagementService",
    "ServerNotFoundError",
    "ServerOperationNotAllowedError",
    "ServerProviderError",
    "ServerUnavailableError",
]

RESOURCE_TYPE_SERVER = "server"

#: Provider state strings → the local lifecycle state they represent. Only
#: transitions the local state machine allows are applied (see
#: :meth:`ServerManagementService._apply_provider_state`).
_PROVIDER_STATE_TO_LOCAL: dict[str, ServerLifecycleState] = {
    "RUNNING": ServerLifecycleState.RUNNING,
    "STARTED": ServerLifecycleState.RUNNING,
    "ACTIVE": ServerLifecycleState.RUNNING,
    "STOPPED": ServerLifecycleState.STOPPED,
    "OFF": ServerLifecycleState.STOPPED,
    "SHUTOFF": ServerLifecycleState.STOPPED,
}

_MAX_DISPLAY_NAME = 64
_ALLOWED_DISPLAY_NAME = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789-_"
    ". "
    "آپچجحخدهذرژسشصضطظعغفقکگلمنوهی"
)


class RenewalCollector(Protocol):
    """The commercial half of My Servers (implemented by ``RenewalChecker``).

    Declared as a port so the server-management service never imports the
    renewal module's concrete class and so tests can supply a fake. Every
    method acts on the LOCAL service period; none of them can reach a provider
    API, and the settlement itself is exactly-once in the wallet ledger.
    """

    async def record_for(self, server_id: UUID) -> Any | None:
        """The commercial record of one service, or ``None``."""
        ...

    async def settle_now(self, server_id: UUID) -> Any:
        """Settle the current period from the wallet, exactly once."""
        ...

    async def set_auto_renew(self, server_id: UUID, enabled: bool) -> Any | None:
        """Persist the automatic-renewal preference, or ``None``."""
        ...


class ServerManagementError(Exception):
    """Base class for every customer server-management failure.

    Messages are written for operators/logs. The Telegram layer maps the
    exception *type* to a Persian message; a raw provider error never reaches a
    customer.
    """


class ServerNotFoundError(ServerManagementError):
    """The server does not exist OR does not belong to this customer."""


class ServerOperationNotAllowedError(ServerManagementError):
    """The policy, provider capability or local state forbids the operation."""

    def __init__(self, message: str, *, operation: ServerOperation, reason: str) -> None:
        super().__init__(message)
        self.operation = operation
        self.reason = reason


class ServerConfirmationError(ServerManagementError):
    """A confirmation token is missing, expired, replayed or mismatched."""

    def __init__(self, message: str, *, status: ConfirmationStatus) -> None:
        super().__init__(message)
        self.status = status


class ServerUnavailableError(ServerManagementError):
    """The provider is temporarily unreachable (safe to retry later)."""


class ServerProviderError(ServerManagementError):
    """The provider definitively rejected the operation."""


class ServerAmbiguousOutcomeError(ServerManagementError):
    """The provider outcome cannot be proven (never re-sent automatically)."""


@dataclass(frozen=True, slots=True)
class _CustomerRef:
    """Minimal identity shim so the event builders can label the actor."""

    id: UUID


@dataclass(frozen=True, slots=True)
class _ProviderContext:
    """The adapter plus its structurally detected VPS capabilities."""

    provider: Any
    capabilities: VpsCapabilities


class ServerManagementService:
    """Customer-facing, ownership-safe server management."""

    def __init__(
        self,
        *,
        servers: ServerRepository,
        registry: ProviderRegistry,
        policy: ServerManagementPolicy,
        confirmations: ConfirmationVerifier,
        audit_repo: AuditRepository,
        power: PowerCommandService | None = None,
        event_sink: BusinessEventSink | None = None,
        renewal_collector: RenewalCollector | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._servers = servers
        self._registry = registry
        self._policy = policy
        self._confirmations = confirmations
        self._audit = AuditTrail(audit_repo)
        self._power = power
        self._events = event_sink
        # The commercial collector (RenewalChecker) is optional: a deployment
        # that exposes no billing capability simply renders no commercial lines.
        self._renewals = renewal_collector
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def policy(self) -> ServerManagementPolicy:
        return self._policy

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def list_servers(self, customer_id: UUID, *, page: int = 1) -> CustomerServerPage:
        """One stable page of the customer's servers (newest first)."""
        if not self._policy.enabled:
            return CustomerServerPage(items=(), page=1, page_size=self._policy.page_size, total=0)
        page_size = self._policy.page_size
        requested = max(1, int(page))
        offset = (requested - 1) * page_size
        rows, total = await self._servers.list_by_user_paged(
            customer_id, offset=offset, limit=page_size
        )
        pages = max(1, -(-total // page_size))
        if requested > pages:
            requested = pages
            offset = (requested - 1) * page_size
            rows, total = await self._servers.list_by_user_paged(
                customer_id, offset=offset, limit=page_size
            )
        items = tuple(self._local_view(row) for row in rows)
        return CustomerServerPage(items=items, page=requested, page_size=page_size, total=total)

    async def get_server(self, customer_id: UUID, server_id: UUID) -> CustomerServerView:
        """The customer-safe view of one owned server (local state)."""
        server = await self._owned(customer_id, server_id)
        return await self._detail_view(server)

    async def refresh_server(self, customer_id: UUID, server_id: UUID) -> CustomerServerView:
        """Read-only refresh: provider state, IPs and image land on the row.

        Never mutates anything provider-side (§27): the worst case is an
        unchanged local row plus a ``refresh_error`` on the view.
        """
        server = await self._owned(customer_id, server_id)
        error: str | None = None
        if server.provider_server_id:
            context = await self._context(server)
            if context is not None and context.capabilities.inventory:
                try:
                    info = await context.provider.get_vps_info(server.provider_server_id)
                except ProviderError as exc:
                    error = _safe_reason(exc)
                    info = None
                if info is not None:
                    await self._apply_provider_info(server, info)
                    await self._apply_provider_ips(server, context)
        view = await self._detail_view(server)
        if error is not None:
            view = _with_refresh_error(view, error)
        return view

    async def traffic(
        self, customer_id: UUID, server_id: UUID, *, window_days: int | None = None
    ) -> TrafficUsageView:
        """Provider-reported traffic for the recent window (read-only)."""
        server = await self._owned(customer_id, server_id)
        context = await self._require(server, ServerOperation.TRAFFIC)
        days = int(window_days or self._policy.traffic_window_days)
        now = self._clock()
        start = now - timedelta(days=max(1, days))
        try:
            usages = await context.provider.get_vps_data_traffic(
                _provider_id(server),
                from_=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                to=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                granularity="DAY",
            )
        except ProviderError as exc:
            return TrafficUsageView(unavailable_reason=_safe_reason(exc))
        return _traffic_view(usages)

    async def console(self, customer_id: UUID, server_id: UUID) -> ServerConsoleView:
        """A temporary console session (secret-bearing, never logged)."""
        server = await self._owned(customer_id, server_id)
        context = await self._require(server, ServerOperation.CONSOLE)
        try:
            session: ConsoleSession = await context.provider.get_console_session(
                _provider_id(server)
            )
        except ProviderError as exc:
            raise ServerProviderError(_safe_reason(exc)) from exc
        await self._audit.record_mutation(
            actor_type=ActorType.USER,
            action="server.console_requested",
            resource_type=RESOURCE_TYPE_SERVER,
            resource_id=str(server.id),
            actor_id=customer_id,
            # The console URL is a temporary credential: never an audit field.
            metadata={"provider": server.provider_key},
        )
        await self._emit(
            BusinessEventType.SERVER_CONSOLE_REQUESTED,
            server,
            customer_id,
            operation=ServerOperation.CONSOLE.value,
            result="requested",
        )
        return ServerConsoleView(url=session.url)

    async def snapshots(self, customer_id: UUID, server_id: UUID) -> list[ServerSnapshotView]:
        """The server's snapshots (read-only)."""
        server = await self._owned(customer_id, server_id)
        context = await self._require(server, ServerOperation.SNAPSHOT_LIST)
        try:
            rows = await context.provider.list_vps_snapshots(_provider_id(server))
        except ProviderError as exc:
            raise ServerProviderError(_safe_reason(exc)) from exc
        return [
            ServerSnapshotView(
                ref=_snapshot_ref(row.id),
                name=row.name,
                state=row.state,
                created_at=row.created_at,
            )
            for row in rows
        ]

    async def reinstall_images(
        self, customer_id: UUID, server_id: UUID
    ) -> list[ReinstallImageView]:
        """The provider's CURRENT reinstall images (never hard-coded)."""
        server = await self._owned(customer_id, server_id)
        context = await self._require(server, ServerOperation.REINSTALL)
        try:
            rows = await context.provider.list_vps_reinstall_images(_provider_id(server))
        except ProviderError as exc:
            raise ServerProviderError(_safe_reason(exc)) from exc
        images = [
            ReinstallImageView(ref=str(row.id), name=row.name, family=row.family) for row in rows
        ]
        images.sort(key=lambda image: (image.family or "", image.name))
        return images

    async def list_ips(self, customer_id: UUID, server_id: UUID) -> list[IpAddressView]:
        """The server's IPs (read-only, customer-safe)."""
        server = await self._owned(customer_id, server_id)
        context = await self._require(server, ServerOperation.IP_LIST)
        try:
            rows = await context.provider.list_vps_ips(_provider_id(server))
        except ProviderError as exc:
            raise ServerProviderError(_safe_reason(exc)) from exc
        return [_ip_view(row) for row in rows]

    async def monitoring(self, customer_id: UUID, server_id: UUID) -> MonitoringView:
        """Monitoring state (read-only)."""
        server = await self._owned(customer_id, server_id)
        context = await self._require(server, ServerOperation.MONITORING)
        try:
            record = await context.provider.get_vps_monitoring(_provider_id(server))
        except ProviderError as exc:
            raise ServerProviderError(_safe_reason(exc)) from exc
        allowed = (
            policies.operation_allowed(
                ServerOperation.MONITORING_ENABLE,
                policy=self._policy,
                capabilities=context.capabilities,
                state=server.state,
            )
            is None
        )
        status = (record.status or "").strip().upper()
        return MonitoringView(
            enabled=status in {"UP", "ENABLED", "ACTIVE", "OK", "ON"},
            status=record.status,
            description=record.description,
            documented=record.documented,
            can_enable=allowed and status not in {"UP", "ENABLED", "ACTIVE", "OK", "ON"},
        )

    async def list_isos(self, customer_id: UUID, server_id: UUID) -> list[tuple[str, str]]:
        """The account's ISO catalogue (read-only), as ``(ref, name)``."""
        server = await self._owned(customer_id, server_id)
        context = await self._require(server, ServerOperation.ISO_LIST)
        try:
            rows = await context.provider.list_vps_isos()
        except ProviderError as exc:
            raise ServerProviderError(_safe_reason(exc)) from exc
        return [(str(row.id), row.name) for row in rows]

    # ------------------------------------------------------------------
    # Power (confirmation only where the policy says so)
    # ------------------------------------------------------------------

    async def start_server(
        self,
        customer_id: UUID,
        server_id: UUID,
        *,
        idempotency_key: str | None = None,
    ) -> ServerActionOutcome:
        return await self._power_action(
            customer_id, server_id, ServerOperation.START, idempotency_key=idempotency_key
        )

    async def reboot_server(
        self,
        customer_id: UUID,
        server_id: UUID,
        *,
        idempotency_key: str | None = None,
    ) -> ServerActionOutcome:
        return await self._power_action(
            customer_id, server_id, ServerOperation.REBOOT, idempotency_key=idempotency_key
        )

    async def stop_server(
        self,
        customer_id: UUID,
        server_id: UUID,
        *,
        confirmation_token: str | None = None,
        idempotency_key: str | None = None,
    ) -> ServerActionOutcome:
        """Stop the server (confirmation-protected: access returns only on start)."""
        return await self._power_action(
            customer_id,
            server_id,
            ServerOperation.STOP,
            confirmation_token=confirmation_token,
            idempotency_key=idempotency_key,
        )

    async def _power_action(
        self,
        customer_id: UUID,
        server_id: UUID,
        operation: ServerOperation,
        *,
        confirmation_token: str | None = None,
        idempotency_key: str | None = None,
    ) -> ServerActionOutcome:
        server = await self._owned(customer_id, server_id)
        context = await self._require(server, operation)
        if self._policy.requires_confirmation(operation):
            consumed = await self._consume(
                confirmation_token,
                server,
                customer_id,
                operation,
                arguments=None,
                context=context,
            )
            if consumed is not None:
                return consumed
        if self._power is None:
            raise ServerOperationNotAllowedError(
                "power commands are not configured",
                operation=operation,
                reason="power_unavailable",
            )
        action = {
            ServerOperation.START: "power_on",
            ServerOperation.STOP: "power_off",
            ServerOperation.REBOOT: "reboot",
        }[operation]
        key = idempotency_key or f"bot-power:{server.id}:{action}"
        method = {
            "power_on": self._power.power_on,
            "power_off": self._power.power_off,
            "reboot": self._power.reboot,
        }[action]
        try:
            result = await method(customer_id, server.id, key)
        except NotServerOwnerError as exc:
            # Ownership was verified above; this only happens if the row moved
            # under us. Same uniform answer as a missing server.
            raise ServerNotFoundError("server not found") from exc
        except PowerActionNotAllowedError as exc:
            raise ServerOperationNotAllowedError(
                "power action not allowed", operation=operation, reason="state_not_allowed"
            ) from exc
        except PowerCommandError as exc:
            if _is_ambiguous(exc):
                await self._record_ambiguous(server, customer_id, operation)
                raise ServerAmbiguousOutcomeError(_safe_reason(exc)) from exc
            raise ServerProviderError(_safe_reason(exc)) from exc
        except ProviderError as exc:
            # Defensive: the ledger converts provider failures into its own
            # errors, but an ambiguous one must never be reported as a
            # definitive rejection if it reaches here directly.
            if _is_ambiguous(exc):
                await self._record_ambiguous(server, customer_id, operation)
                raise ServerAmbiguousOutcomeError(_safe_reason(exc)) from exc
            raise ServerProviderError(_safe_reason(exc)) from exc

        event_type = {
            ServerOperation.START: BusinessEventType.SERVER_STARTED,
            ServerOperation.STOP: BusinessEventType.SERVER_STOPPED,
            ServerOperation.REBOOT: BusinessEventType.SERVER_REBOOT_REQUESTED,
        }[operation]
        await self._emit(
            event_type,
            server,
            customer_id,
            operation=operation.value,
            result="replayed" if result.replayed else "accepted",
        )
        return ServerActionOutcome(
            operation=operation,
            accepted=True,
            replayed=bool(result.replayed),
            detail=None if not result.replayed else "already_in_progress",
        )

    # ------------------------------------------------------------------
    # Destructive operations (confirmation required)
    # ------------------------------------------------------------------

    async def reinstall(
        self,
        customer_id: UUID,
        server_id: UUID,
        *,
        image_ref: str,
        confirmation_token: str | None,
    ) -> ServerActionOutcome:
        """Reinstall with a provider-fetched image (destructive, confirmed)."""
        server = await self._owned(customer_id, server_id)
        context = await self._require(server, ServerOperation.REINSTALL)
        arguments = {"image": str(image_ref)}
        consumed = await self._consume(
            confirmation_token,
            server,
            customer_id,
            ServerOperation.REINSTALL,
            arguments=arguments,
            context=context,
        )
        if consumed is not None:
            return consumed
        await self._call(
            context.provider.reinstall_vps(_provider_id(server), str(image_ref)),
            operation=ServerOperation.REINSTALL,
            server=server,
            customer_id=customer_id,
            arguments=arguments,
        )
        await self._emit(
            BusinessEventType.SERVER_REINSTALL_REQUESTED,
            server,
            customer_id,
            operation=ServerOperation.REINSTALL.value,
            image=str(image_ref),
            result="accepted",
        )
        return ServerActionOutcome(operation=ServerOperation.REINSTALL, accepted=True)

    async def reset_password(
        self,
        customer_id: UUID,
        server_id: UUID,
        *,
        confirmation_token: str | None,
    ) -> ServerActionOutcome:
        """Reset the OS password (destructive, confirmed).

        The provider response carries no password: the new value must be read
        through the credential endpoints, so NOTHING is invented or displayed
        here. The credential endpoints themselves stay operator-only.
        """
        server = await self._owned(customer_id, server_id)
        context = await self._require(server, ServerOperation.PASSWORD_RESET)
        consumed = await self._consume(
            confirmation_token,
            server,
            customer_id,
            ServerOperation.PASSWORD_RESET,
            arguments=None,
            context=context,
        )
        if consumed is not None:
            return consumed
        await self._call(
            context.provider.reset_vps_password(_provider_id(server)),
            operation=ServerOperation.PASSWORD_RESET,
            server=server,
            customer_id=customer_id,
        )
        await self._emit(
            BusinessEventType.SERVER_PASSWORD_RESET_REQUESTED,
            server,
            customer_id,
            operation=ServerOperation.PASSWORD_RESET.value,
            result="accepted",
        )
        return ServerActionOutcome(operation=ServerOperation.PASSWORD_RESET, accepted=True)

    async def create_snapshot(
        self,
        customer_id: UUID,
        server_id: UUID,
        *,
        name: str,
        confirmation_token: str | None,
    ) -> ServerActionOutcome:
        """Create a snapshot (confirmed; creation is billed provider-side)."""
        server = await self._owned(customer_id, server_id)
        context = await self._require(server, ServerOperation.SNAPSHOT_CREATE)
        arguments = {"name": _safe_name(name)}
        consumed = await self._consume(
            confirmation_token,
            server,
            customer_id,
            ServerOperation.SNAPSHOT_CREATE,
            arguments=arguments,
            context=context,
        )
        if consumed is not None:
            return consumed
        await self._call(
            context.provider.create_vps_snapshot(_provider_id(server), arguments["name"]),
            operation=ServerOperation.SNAPSHOT_CREATE,
            server=server,
            customer_id=customer_id,
            arguments=arguments,
        )
        await self._emit(
            BusinessEventType.SERVER_SNAPSHOT_CREATED,
            server,
            customer_id,
            operation=ServerOperation.SNAPSHOT_CREATE.value,
            snapshot=arguments["name"],
            result="accepted",
        )
        return ServerActionOutcome(operation=ServerOperation.SNAPSHOT_CREATE, accepted=True)

    async def restore_snapshot(
        self,
        customer_id: UUID,
        server_id: UUID,
        *,
        snapshot_ref: str,
        confirmation_token: str | None,
    ) -> ServerActionOutcome:
        """Restore a snapshot over the running server (destructive, confirmed)."""
        return await self._snapshot_mutation(
            customer_id,
            server_id,
            operation=ServerOperation.SNAPSHOT_RESTORE,
            snapshot_ref=snapshot_ref,
            confirmation_token=confirmation_token,
        )

    async def delete_snapshot(
        self,
        customer_id: UUID,
        server_id: UUID,
        *,
        snapshot_ref: str,
        confirmation_token: str | None,
    ) -> ServerActionOutcome:
        """Delete a snapshot (destructive, confirmed)."""
        return await self._snapshot_mutation(
            customer_id,
            server_id,
            operation=ServerOperation.SNAPSHOT_DELETE,
            snapshot_ref=snapshot_ref,
            confirmation_token=confirmation_token,
        )

    async def _snapshot_mutation(
        self,
        customer_id: UUID,
        server_id: UUID,
        *,
        operation: ServerOperation,
        snapshot_ref: str,
        confirmation_token: str | None,
    ) -> ServerActionOutcome:
        server = await self._owned(customer_id, server_id)
        context = await self._require(server, operation)
        arguments = {"snapshot": str(snapshot_ref)}
        consumed = await self._consume(
            confirmation_token,
            server,
            customer_id,
            operation,
            arguments=arguments,
            context=context,
        )
        if consumed is not None:
            return consumed
        if operation is ServerOperation.SNAPSHOT_RESTORE:
            call = context.provider.restore_vps_snapshot(_provider_id(server), str(snapshot_ref))
            event = BusinessEventType.SERVER_SNAPSHOT_RESTORED
        else:
            call = context.provider.delete_vps_snapshot(_provider_id(server), str(snapshot_ref))
            event = BusinessEventType.SERVER_SNAPSHOT_DELETED
        await self._call(
            call,
            operation=operation,
            server=server,
            customer_id=customer_id,
            arguments=arguments,
        )
        await self._emit(
            event,
            server,
            customer_id,
            operation=operation.value,
            snapshot=str(snapshot_ref),
            result="accepted",
        )
        return ServerActionOutcome(operation=operation, accepted=True)

    async def null_route_ip(
        self,
        customer_id: UUID,
        server_id: UUID,
        *,
        ip: str,
        confirmation_token: str | None,
    ) -> ServerActionOutcome:
        """Null-route one of the server's OWN IPs (destructive, confirmed)."""
        server = await self._owned(customer_id, server_id)
        context = await self._require(server, ServerOperation.IP_NULL_ROUTE)
        owned = await self._require_owned_ip(context, server, ip)
        arguments = {"ip": owned}
        consumed = await self._consume(
            confirmation_token,
            server,
            customer_id,
            ServerOperation.IP_NULL_ROUTE,
            arguments=arguments,
            context=context,
        )
        if consumed is not None:
            return consumed
        await self._call(
            context.provider.null_route_vps_ip(_provider_id(server), owned),
            operation=ServerOperation.IP_NULL_ROUTE,
            server=server,
            customer_id=customer_id,
            arguments=arguments,
        )
        await self._emit(
            BusinessEventType.SERVER_IP_NULL_ROUTED,
            server,
            customer_id,
            operation=ServerOperation.IP_NULL_ROUTE.value,
            ip=_mask_ip(owned),
            result="accepted",
        )
        return ServerActionOutcome(operation=ServerOperation.IP_NULL_ROUTE, accepted=True)

    async def unnull_route_ip(
        self, customer_id: UUID, server_id: UUID, *, ip: str
    ) -> ServerActionOutcome:
        """Remove a null route from the server's own IP (reversible)."""
        server = await self._owned(customer_id, server_id)
        context = await self._require(server, ServerOperation.IP_UNNULL_ROUTE)
        owned = await self._require_owned_ip(context, server, ip)
        await self._call(
            context.provider.unnull_route_vps_ip(_provider_id(server), owned),
            operation=ServerOperation.IP_UNNULL_ROUTE,
            server=server,
            customer_id=customer_id,
            arguments={"ip": owned},
        )
        await self._emit(
            BusinessEventType.SERVER_IP_UNNULL_ROUTED,
            server,
            customer_id,
            operation=ServerOperation.IP_UNNULL_ROUTE.value,
            ip=_mask_ip(owned),
            result="accepted",
        )
        return ServerActionOutcome(operation=ServerOperation.IP_UNNULL_ROUTE, accepted=True)

    async def set_reverse_dns(
        self, customer_id: UUID, server_id: UUID, *, ip: str, reverse_lookup: str
    ) -> IpAddressView:
        """Set reverse DNS for one of the server's own IPs (reversible)."""
        server = await self._owned(customer_id, server_id)
        context = await self._require(server, ServerOperation.IP_SET_RDNS)
        owned = await self._require_owned_ip(context, server, ip)
        value = _safe_hostname(reverse_lookup)
        try:
            record: VpsIpRecord = await context.provider.set_vps_ip_reverse_dns(
                _provider_id(server), owned, value
            )
        except ProviderError as exc:
            raise ServerProviderError(_safe_reason(exc)) from exc
        await self._audit.record_mutation(
            actor_type=ActorType.USER,
            action="server.ip_reverse_dns_set",
            resource_type=RESOURCE_TYPE_SERVER,
            resource_id=str(server.id),
            actor_id=customer_id,
            metadata={"ip": _mask_ip(owned)},
        )
        return _ip_view(record)

    async def attach_iso(
        self,
        customer_id: UUID,
        server_id: UUID,
        *,
        iso_ref: str,
        confirmation_token: str | None,
    ) -> ServerActionOutcome:
        """Attach an ISO from the provider catalogue (destructive, confirmed)."""
        server = await self._owned(customer_id, server_id)
        context = await self._require(server, ServerOperation.ISO_ATTACH)
        arguments = {"iso": str(iso_ref)}
        consumed = await self._consume(
            confirmation_token,
            server,
            customer_id,
            ServerOperation.ISO_ATTACH,
            arguments=arguments,
            context=context,
        )
        if consumed is not None:
            return consumed
        await self._call(
            context.provider.attach_vps_iso(_provider_id(server), str(iso_ref)),
            operation=ServerOperation.ISO_ATTACH,
            server=server,
            customer_id=customer_id,
            arguments=arguments,
        )
        return ServerActionOutcome(operation=ServerOperation.ISO_ATTACH, accepted=True)

    async def detach_iso(
        self, customer_id: UUID, server_id: UUID, *, confirmation_token: str | None
    ) -> ServerActionOutcome:
        """Detach the attached ISO (destructive, confirmed)."""
        server = await self._owned(customer_id, server_id)
        context = await self._require(server, ServerOperation.ISO_DETACH)
        consumed = await self._consume(
            confirmation_token,
            server,
            customer_id,
            ServerOperation.ISO_DETACH,
            arguments=None,
            context=context,
        )
        if consumed is not None:
            return consumed
        await self._call(
            context.provider.detach_vps_iso(_provider_id(server)),
            operation=ServerOperation.ISO_DETACH,
            server=server,
            customer_id=customer_id,
        )
        return ServerActionOutcome(operation=ServerOperation.ISO_DETACH, accepted=True)

    async def enable_monitoring(self, customer_id: UUID, server_id: UUID) -> ServerActionOutcome:
        """Enable provider monitoring (reversible)."""
        server = await self._owned(customer_id, server_id)
        context = await self._require(server, ServerOperation.MONITORING_ENABLE)
        await self._call(
            context.provider.enable_vps_monitoring(_provider_id(server)),
            operation=ServerOperation.MONITORING_ENABLE,
            server=server,
            customer_id=customer_id,
        )
        return ServerActionOutcome(operation=ServerOperation.MONITORING_ENABLE, accepted=True)

    async def rename(
        self, customer_id: UUID, server_id: UUID, *, display_name: str
    ) -> ServerRenameView:
        """Rename the server (local display name kept in step with the provider)."""
        server = await self._owned(customer_id, server_id)
        context = await self._require(server, ServerOperation.RENAME)
        name = _clean_display_name(display_name)
        try:
            info = await context.provider.rename_vps(_provider_id(server), name)
        except ProviderError as exc:
            if _is_ambiguous(exc):
                await self._record_ambiguous(server, customer_id, ServerOperation.RENAME)
                raise ServerAmbiguousOutcomeError(_safe_reason(exc)) from exc
            raise ServerProviderError(_safe_reason(exc)) from exc
        server.os = server.os
        if info.image_name:
            server.os = info.image_name or server.os
        await self._servers.save(server)
        await self._audit.record_mutation(
            actor_type=ActorType.USER,
            action="server.renamed",
            resource_type=RESOURCE_TYPE_SERVER,
            resource_id=str(server.id),
            actor_id=customer_id,
            metadata={"display_name": name},
        )
        return ServerRenameView(server_id=server.id, display_name=name)

    # ------------------------------------------------------------------
    # Commercial lifecycle (never a provider call, §18/§36-§38)
    # ------------------------------------------------------------------

    async def renew_now(
        self,
        customer_id: UUID,
        server_id: UUID,
        *,
        confirmation_token: str | None,
    ) -> ServerRenewalView:
        """Settle the current period from the customer's wallet, exactly once.

        Requires ownership, an allowed ``RENEW_NOW`` policy and a consumed
        confirmation token, because this moves money. Two independent guards
        stop a double charge: the confirmation token is single-use (atomic in
        the shared store), and the debit itself is idempotent per
        ``(server, period)`` in the wallet ledger. No provider API is called:
        Leaseweb renews its own contract with the platform (§28) and the modern
        VPS API exposes no renewal operation to call.
        """
        server = await self._owned(customer_id, server_id)
        await self._require_commercial(server, ServerOperation.RENEW_NOW)
        checker = self._require_renewals()
        consumed = await self._consume(
            confirmation_token,
            server,
            customer_id,
            ServerOperation.RENEW_NOW,
            arguments=None,
        )
        if consumed is not None:
            # The token was already consumed: nothing was charged a second
            # time, and the customer is told exactly that.
            return ServerRenewalView(
                server_id=server.id,
                reason="already_charged",
                settled=True,
            )
        outcome = await checker.settle_now(server.id)
        settled = bool(getattr(outcome, "settled", False))
        reason = str(getattr(outcome, "reason", "not_payable"))
        amount = int(getattr(outcome, "amount_minor", 0) or 0)
        currency = str(getattr(outcome, "currency", "") or "")
        period_end = getattr(outcome, "period_end", None)
        await self._audit.record_mutation(
            actor_type=ActorType.USER,
            action="service.renewal_requested",
            resource_type=RESOURCE_TYPE_SERVER,
            resource_id=str(server.id),
            actor_id=customer_id,
            reason=f"manual renewal: {reason}",
            metadata={
                "result": reason,
                "settled": str(settled),
                "amount_minor": str(amount),
                "currency": currency,
            },
        )
        await self._emit_renewal(
            event_type=_RENEWAL_EVENT.get(
                reason, BusinessEventType.SERVICE_OPERATOR_ATTENTION_REQUIRED
            ),
            server=server,
            customer_id=customer_id,
            result=reason,
            amount_minor=amount,
            currency=currency,
            period_end=period_end,
            auto_renew=getattr(outcome, "auto_renew_enabled", None),
            key_parts=(server.id, "settle-now", getattr(outcome, "status", ""), period_end),
        )
        return ServerRenewalView(
            server_id=server.id,
            reason=reason,
            settled=settled,
            status=str(getattr(outcome, "status", "")) or None,
            amount_minor=amount or None,
            currency=currency or None,
            period_end=period_end,
            grace_until=getattr(outcome, "grace_until", None),
            auto_renew_enabled=getattr(outcome, "auto_renew_enabled", None),
        )

    async def set_auto_renew(
        self, customer_id: UUID, server_id: UUID, *, enabled: bool
    ) -> ServerRenewalView:
        """Persist the customer's automatic-renewal preference (durable).

        Free and reversible, so no confirmation token is needed — but it is
        ownership-checked, policy-checked, audited and idempotent: writing the
        same value twice is a no-op that still reports the current state.
        """
        server = await self._owned(customer_id, server_id)
        await self._require_commercial(server, ServerOperation.AUTO_RENEW)
        checker = self._require_renewals()
        record = await checker.set_auto_renew(server.id, bool(enabled))
        if record is None:
            return ServerRenewalView(server_id=server.id, reason="no_renewal_record")
        current = bool(getattr(record, "auto_charge_enabled", enabled))
        reason = "auto_renew_on" if current else "auto_renew_off"
        if current is not bool(enabled):
            # The store did not hold the requested value: report the truth
            # rather than the intent, so the UI can never show a lie.
            reason = "no_renewal_record"
        await self._audit.record_mutation(
            actor_type=ActorType.USER,
            action="service.auto_renew_changed",
            resource_type=RESOURCE_TYPE_SERVER,
            resource_id=str(server.id),
            actor_id=customer_id,
            metadata={"auto_renew": "on" if current else "off"},
        )
        await self._emit_renewal(
            event_type=(
                BusinessEventType.SERVICE_AUTO_RENEW_ENABLED
                if current
                else BusinessEventType.SERVICE_AUTO_RENEW_DISABLED
            ),
            server=server,
            customer_id=customer_id,
            result=reason,
            auto_renew=current,
            key_parts=(server.id, "auto-renew", "on" if current else "off"),
        )
        return ServerRenewalView(
            server_id=server.id,
            reason=reason,
            status=str(getattr(record, "status", "")) or None,
            amount_minor=int(getattr(record, "customer_price_minor", 0) or 0) or None,
            currency=str(getattr(record, "currency", "") or "") or None,
            period_end=getattr(record, "provider_renewal_at", None),
            grace_until=getattr(record, "grace_until", None),
            auto_renew_enabled=current,
        )

    async def _require_commercial(self, server: CloudServer, operation: ServerOperation) -> None:
        """Policy gate for the commercial operations (no provider needed)."""
        unmet = policies.operation_allowed(
            operation,
            policy=self._policy,
            capabilities=_NO_PROVIDER_CAPABILITIES,
            state=server.state,
        )
        if unmet is not None:
            raise ServerOperationNotAllowedError(
                f"operation {operation.value} is not available ({unmet.reason})",
                operation=operation,
                reason=unmet.reason,
            )

    def _require_renewals(self) -> RenewalCollector:
        if self._renewals is None:
            raise ServerUnavailableError("the commercial lifecycle is not configured")
        return self._renewals

    async def _emit_renewal(
        self,
        *,
        event_type: BusinessEventType,
        server: CloudServer,
        customer_id: UUID,
        result: str,
        amount_minor: int | None = None,
        currency: str | None = None,
        period_end: datetime | None = None,
        grace_until: datetime | None = None,
        auto_renew: bool | None = None,
        key_parts: tuple[object, ...] = (),
    ) -> None:
        """Emit one commercial event; a broken sink never breaks the action."""
        await emit_safe(
            self._events,
            service_renewal_event(
                event_type=event_type,
                event_key_parts=key_parts,
                user=_CustomerRef(id=customer_id),
                server_id=server.id,
                provider_key=server.provider_key,
                state=server.state.value,
                result=result,
                amount_minor=amount_minor,
                currency=currency,
                period_end=period_end,
                grace_until=grace_until,
                auto_renew=auto_renew,
                at=self._clock(),
            ),
        )

    # ------------------------------------------------------------------
    # Confirmation issuance
    # ------------------------------------------------------------------

    async def issue_confirmation(
        self,
        customer_id: UUID,
        server_id: UUID,
        operation: ServerOperation,
        *,
        arguments: Mapping[str, Any] | None = None,
    ) -> str:
        """Mint a one-time confirmation token for an allowed operation.

        Commercial operations (renew-now) are provider-agnostic, so they are
        gated on the policy alone: a billing action must never depend on the
        provider adapter being reachable.
        """
        server = await self._owned(customer_id, server_id)
        if operation in BILLABLE_SERVER_OPERATIONS:
            await self._require_commercial(server, operation)
        else:
            context = await self._require(server, operation)
            del context
        return self._confirmations.issue(
            ConfirmationBinding(
                customer_id=customer_id,
                server_id=server.id,
                operation=operation,
                arguments=arguments,
            ),
            now=self._clock(),
        ).token

    # ------------------------------------------------------------------
    # Internals: ownership, policy, provider access
    # ------------------------------------------------------------------

    async def _owned(self, customer_id: UUID, server_id: UUID) -> CloudServer:
        """Load the server, proving ownership; uniform error when it fails."""
        if not self._policy.enabled:
            raise ServerOperationNotAllowedError(
                "server management is disabled",
                operation=ServerOperation.VIEW,
                reason="feature_disabled",
            )
        server = await self._servers.get(server_id)
        if server is None or server.user_id != customer_id:
            raise ServerNotFoundError("server not found")
        return server

    async def _context(self, server: CloudServer) -> _ProviderContext | None:
        """The adapter for this server plus its detected VPS capabilities."""
        try:
            provider = provider_for(
                self._registry, server.provider_key, server.credential_account_id
            )
        except KeyError:
            return None
        return _ProviderContext(provider=provider, capabilities=vps_capabilities_of(provider))

    async def _require(self, server: CloudServer, operation: ServerOperation) -> _ProviderContext:
        """The provider context, or the specific reason the operation is denied."""
        context = await self._context(server)
        if context is None:
            raise ServerOperationNotAllowedError(
                "provider is not registered",
                operation=operation,
                reason="provider_disabled",
            )
        unmet = policies.operation_allowed(
            operation,
            policy=self._policy,
            capabilities=context.capabilities,
            state=server.state,
        )
        if unmet is not None:
            raise ServerOperationNotAllowedError(
                f"operation {operation.value} is not available ({unmet.reason})",
                operation=operation,
                reason=unmet.reason,
            )
        if not server.provider_server_id:
            raise ServerOperationNotAllowedError(
                "server has no provider resource yet",
                operation=operation,
                reason="not_provisioned",
            )
        return context

    async def _require_owned_ip(
        self, context: _ProviderContext, server: CloudServer, ip: str
    ) -> str:
        """Prove ``ip`` currently belongs to this server before touching it.

        The requested address must appear in the provider's CURRENT IP list for
        this VPS — the check is done against the provider, not against a
        callback value, so a customer can never act on a foreign address.
        """
        requested = str(ip or "").strip()
        if not requested:
            raise ServerOperationNotAllowedError(
                "ip is required", operation=ServerOperation.IP_LIST, reason="invalid_ip"
            )
        try:
            rows = await context.provider.list_vps_ips(_provider_id(server))
        except ProviderError as exc:
            raise ServerProviderError(_safe_reason(exc)) from exc
        for row in rows:
            if str(row.ip) == requested:
                return requested
        raise ServerOperationNotAllowedError(
            "ip does not belong to this server",
            operation=ServerOperation.IP_LIST,
            reason="ip_not_owned",
        )

    # ------------------------------------------------------------------
    # Internals: confirmations, provider calls, events
    # ------------------------------------------------------------------

    async def _consume(
        self,
        token: str | None,
        server: CloudServer,
        customer_id: UUID,
        operation: ServerOperation,
        *,
        arguments: Mapping[str, Any] | None,
        context: _ProviderContext | None = None,
    ) -> ServerActionOutcome | None:
        """Consume a confirmation; None when the caller may proceed.

        A REPLAYED token returns an already-accepted outcome WITHOUT touching
        the provider — that is the exactly-once guarantee behind the
        double-click tests.
        """
        if not token:
            raise ServerConfirmationError(
                "confirmation required", status=ConfirmationStatus.INVALID
            )
        result = await self._confirmations.consume(
            token,
            ConfirmationBinding(
                customer_id=customer_id,
                server_id=server.id,
                operation=operation,
                arguments=arguments,
            ),
            now=self._clock(),
        )
        if result.status is ConfirmationStatus.OK:
            del context
            return None
        if result.status is ConfirmationStatus.REPLAYED:
            return ServerActionOutcome(
                operation=operation, accepted=True, replayed=True, detail="already_confirmed"
            )
        if result.status is ConfirmationStatus.UNAVAILABLE:
            # Fail CLOSED (PROD-HARDENING §9): the shared confirmation store
            # could not be reached, so we cannot prove this token is unused.
            # Refuse with an honest "try again shortly" rather than execute.
            raise ServerUnavailableError("confirmation store unavailable")
        raise ServerConfirmationError(
            f"confirmation rejected: {result.status.value}", status=result.status
        )

    async def _call(
        self,
        awaitable: Any,
        *,
        operation: ServerOperation,
        server: CloudServer,
        customer_id: UUID,
        arguments: Mapping[str, Any] | None = None,
    ) -> Any:
        """Await one provider mutation, classifying failure honestly."""
        metadata: dict[str, Any] = {"operation": operation.value}
        if arguments:
            metadata.update(
                {str(key): _safe_argument(str(key), value) for key, value in arguments.items()}
            )
        try:
            result = await awaitable
        except ProviderError as exc:
            if _is_ambiguous(exc):
                await self._record_ambiguous(server, customer_id, operation)
                raise ServerAmbiguousOutcomeError(_safe_reason(exc)) from exc
            await self._audit.record_mutation(
                actor_type=ActorType.USER,
                action=f"server.{operation.value}_failed",
                resource_type=RESOURCE_TYPE_SERVER,
                resource_id=str(server.id),
                actor_id=customer_id,
                metadata=metadata,
            )
            await self._emit_failure(
                server,
                customer_id,
                operation,
                category="provider_rejected",
                reason=_safe_reason(exc),
            )
            raise ServerProviderError(_safe_reason(exc)) from exc
        await self._audit.record_mutation(
            actor_type=ActorType.USER,
            action=f"server.{operation.value}",
            resource_type=RESOURCE_TYPE_SERVER,
            resource_id=str(server.id),
            actor_id=customer_id,
            metadata=metadata,
        )
        return result

    async def _record_ambiguous(
        self, server: CloudServer, customer_id: UUID, operation: ServerOperation
    ) -> None:
        """Record an unprovable outcome: attention for a human, never a re-send."""
        await self._audit.record_mutation(
            actor_type=ActorType.USER,
            action=f"server.{operation.value}_outcome_unknown",
            resource_type=RESOURCE_TYPE_SERVER,
            resource_id=str(server.id),
            actor_id=customer_id,
            metadata={"outcome": "unknown"},
        )
        await self._emit_failure(
            server,
            customer_id,
            operation,
            category="outcome_unknown",
            reason="provider did not confirm the result; not re-sent",
        )

    async def _emit(
        self,
        event_type: BusinessEventType,
        server: CloudServer,
        customer_id: UUID,
        **fields: Any,
    ) -> None:
        """Emit one business event; a broken sink never breaks the action."""
        await emit_safe(
            self._events,
            server_management_event(
                event_type=event_type,
                event_key_parts=(
                    server.id,
                    fields.get("operation"),
                    fields.get("result"),
                    fields.get("snapshot") or fields.get("image") or fields.get("ip"),
                ),
                user=_CustomerRef(id=customer_id),
                server_id=server.id,
                provider_key=server.provider_key,
                state=server.state.value,
                operation=str(fields.get("operation", "")),
                result=str(fields.get("result", "")),
                snapshot=fields.get("snapshot"),
                image=fields.get("image"),
                ip=fields.get("ip"),
                at=self._clock(),
            ),
        )

    async def _emit_failure(
        self,
        server: CloudServer,
        customer_id: UUID,
        operation: ServerOperation,
        *,
        category: str,
        reason: str,
    ) -> None:
        """Emit the failure/attention card (safe category + redacted reason)."""
        await emit_safe(
            self._events,
            server_operation_failed_event(
                user=_CustomerRef(id=customer_id),
                server_id=server.id,
                provider_key=server.provider_key,
                state=server.state.value,
                operation=operation.value,
                category=category,
                reason=reason,
                at=self._clock(),
            ),
        )

    # ------------------------------------------------------------------
    # Internals: provider snapshot -> local state
    # ------------------------------------------------------------------

    async def _apply_provider_info(self, server: CloudServer, info: VpsInfo) -> None:
        """Fold a read-only provider snapshot into the local row (safe only)."""
        target = _PROVIDER_STATE_TO_LOCAL.get(info.state.strip().upper())
        if target is not None and target is not server.state:
            try:
                server.transition_to(target)
            except ValueError:
                # The local state machine does not allow this move (e.g. the
                # provider still reports STARTING while we are RUNNING): keep
                # the local state rather than forcing an illegal transition.
                logger.debug(
                    "skipping provider state %s for server %s in state %s",
                    info.state,
                    server.id,
                    server.state,
                )
        if info.image_name and not server.os:
            server.os = info.image_name
        await self._servers.save(server)

    async def _apply_provider_ips(self, server: CloudServer, context: _ProviderContext) -> None:
        """Refresh the cached public address from the provider's IP list."""
        if not context.capabilities.ips:
            return
        try:
            rows = await context.provider.list_vps_ips(_provider_id(server))
        except ProviderError:
            return
        public_v4 = next(
            (
                row
                for row in rows
                if int(row.version) == 4 and str(row.network_type).upper().startswith("PUBLIC")
            ),
            None,
        )
        public_v6 = next(
            (
                row
                for row in rows
                if int(row.version) == 6 and str(row.network_type).upper().startswith("PUBLIC")
            ),
            None,
        )
        changed = False
        if public_v4 is not None and server.ipv4 != public_v4.ip:
            server.ipv4 = public_v4.ip
            changed = True
        if public_v6 is not None and server.ipv6 != public_v6.ip:
            server.ipv6 = public_v6.ip
            changed = True
        if changed:
            await self._servers.save(server)

    def _local_view(self, server: CloudServer) -> CustomerServerView:
        """Project a local row into the customer-safe view."""
        location = policies.location_label(_datacenter_of(server))
        return CustomerServerView(
            server_id=server.id,
            state=policies.customer_state(server.state),
            display_name=server.os or None,
            provider_display_name=_reference_of(server),
            location_code=_datacenter_of(server),
            location_label=_location_text(location),
            ip=server.ipv4,
            ipv6=server.ipv6,
            operating_system=server.os,
            plan=_plan_of(server),
            state_from_provider=False,
        )

    async def _detail_view(self, server: CloudServer) -> CustomerServerView:
        """The local view plus the COMMERCIAL status of the service (§18/§36).

        Infrastructure state and commercial state are separate facts: a server
        can be ``running`` (provider) and ``payment_due`` (billing) at once, and
        the screen shows both. A failure to read the commercial record degrades
        to "no commercial line" — it never hides the server or invents a price,
        and it never blocks a read-only screen.
        """
        view = self._local_view(server)
        if self._renewals is None:
            return view
        try:
            record = await self._renewals.record_for(server.id)
        except Exception:
            logger.warning("could not read the commercial record for server %s", server.id)
            return view
        if record is None:
            return view
        status = getattr(record, "status", None)
        return replace(
            view,
            commercial_status=str(getattr(status, "value", status) or "") or None,
            commercial_payable=bool(getattr(record, "payable", False)),
            auto_renew_enabled=bool(getattr(record, "auto_charge_enabled", True)),
            grace_until=getattr(record, "grace_until", None),
            next_renewal_at=getattr(record, "provider_renewal_at", None),
            renewal_price_minor=int(getattr(record, "customer_price_minor", 0) or 0) or None,
            renewal_currency=str(getattr(record, "currency", "") or "") or None,
        )


# ---------------------------------------------------------------------------
# Pure helpers (no I/O, unit-tested directly)
# ---------------------------------------------------------------------------


#: The business event one manual-renewal reason maps to (§39). Anything not
#: listed is an operator-attention case: the platform could not settle it.
_RENEWAL_EVENT: dict[str, BusinessEventType] = {
    "charged": BusinessEventType.SERVICE_RENEWAL_SUCCEEDED,
    "already_charged": BusinessEventType.SERVICE_RENEWAL_SUCCEEDED,
    "insufficient_funds": BusinessEventType.SERVICE_RENEWAL_FAILED_INSUFFICIENT_BALANCE,
    "not_payable": BusinessEventType.SERVICE_RENEWAL_DUE,
    "manual_review_required": BusinessEventType.SERVICE_OPERATOR_ATTENTION_REQUIRED,
}

#: An all-false capability set for the COMMERCIAL operations, which are
#: provider-agnostic by construction (``_OPERATION_CAPABILITY`` maps them to
#: ``None``), so the policy check never consults it.
_NO_PROVIDER_CAPABILITIES = VpsCapabilities()


def _provider_id(server: CloudServer) -> str:
    """The provider resource id — internal use only, never rendered."""
    if not server.provider_server_id:
        raise ServerNotFoundError("server not found")
    return server.provider_server_id


def _datacenter_of(server: CloudServer) -> str | None:
    """The server's datacenter code, when the platform recorded one."""
    return getattr(server, "datacenter", None) or None


def _reference_of(server: CloudServer) -> str | None:
    """The provider-side reference/name, when the platform recorded one."""
    return getattr(server, "provider_reference", None) or None


def _plan_of(server: CloudServer) -> str | None:
    """The sellable plan name, when the platform recorded one."""
    return getattr(server, "plan_name", None) or None


def _location_text(label: tuple[str, str] | None) -> str | None:
    if label is None:
        return None
    flag, city = label
    return f"{flag} {city}"


def _with_refresh_error(view: CustomerServerView, error: str) -> CustomerServerView:
    return CustomerServerView(
        server_id=view.server_id,
        state=view.state,
        display_name=view.display_name,
        provider_display_name=view.provider_display_name,
        location_code=view.location_code,
        location_label=view.location_label,
        ip=view.ip,
        ipv6=view.ipv6,
        operating_system=view.operating_system,
        plan=view.plan,
        cpu=view.cpu,
        ram_gb=view.ram_gb,
        storage_gb=view.storage_gb,
        traffic_limit=view.traffic_limit,
        traffic_used_bytes=view.traffic_used_bytes,
        traffic_limit_bytes=view.traffic_limit_bytes,
        contract_started_at=view.contract_started_at,
        contract_ends_at=view.contract_ends_at,
        next_renewal_at=view.next_renewal_at,
        refresh_error=error,
        state_from_provider=view.state_from_provider,
        extra_ips=view.extra_ips,
        commercial_status=view.commercial_status,
        commercial_payable=view.commercial_payable,
        auto_renew_enabled=view.auto_renew_enabled,
        grace_until=view.grace_until,
        renewal_price_minor=view.renewal_price_minor,
        renewal_currency=view.renewal_currency,
    )


def _ip_view(row: VpsIpRecord) -> IpAddressView:
    metadata = row.metadata or {}
    ddos = metadata.get("ddos") if isinstance(metadata, Mapping) else None
    profile: str | None = None
    if isinstance(ddos, Mapping):
        profile = ddos.get("protection_type") or ddos.get("detection_profile")
    return IpAddressView(
        ip=str(row.ip),
        version=int(row.version),
        network_type=str(row.network_type) if row.network_type else None,
        main_ip=bool(row.main_ip),
        null_routed=bool(row.null_routed),
        reverse_lookup=row.reverse_lookup,
        ddos_profile=str(profile) if profile else None,
    )


def _traffic_view(usages: Sequence[DataTrafficUsage]) -> TrafficUsageView:
    """Fold provider directions into one customer view (bytes as integers).

    The provider only reports what it reports: a direction it does not return
    is absent, never fabricated as zero, and ``separate_directions`` is True
    only when the provider actually returned both.
    """
    directions = tuple(usage.direction for usage in usages)
    by_direction = {usage.direction.lower(): int(usage.total_bytes or 0) for usage in usages}
    downloaded = by_direction.get("downpublic", 0)
    uploaded = by_direction.get("uppublic", 0)
    total = sum(by_direction.values()) if by_direction else 0
    metadata = usages[0].metadata if usages else {}
    limit_bytes: int | None = None
    raw_limit = metadata.get("limit_bytes") if isinstance(metadata, Mapping) else None
    if isinstance(raw_limit, int):
        limit_bytes = raw_limit
    return TrafficUsageView(
        period_from=str(metadata.get("from")) if isinstance(metadata, Mapping) else None,
        period_to=str(metadata.get("to")) if isinstance(metadata, Mapping) else None,
        granularity=(str(metadata.get("granularity")) if isinstance(metadata, Mapping) else None),
        downloaded_bytes=downloaded,
        uploaded_bytes=uploaded,
        total_bytes=total,
        directions=directions,
        limit_bytes=limit_bytes,
        limit_label=format_bytes(limit_bytes),
        separate_directions=len(usages) > 1,
    )


def _snapshot_ref(provider_snapshot_id: str) -> str:
    """The snapshot reference the UI carries.

    It is the provider's own id because snapshots have no local row to point
    at, but a callback never contains it: the confirmation token binds it and
    the screen holds it only for the duration of the flow.
    """
    return str(provider_snapshot_id)


def _safe_reason(exc: BaseException) -> str:
    """A short, redacted reason safe for audit metadata and business events."""
    text = str(exc).strip() or type(exc).__name__
    return text[:200]


def _is_ambiguous(exc: BaseException) -> bool:
    """Whether an error means the provider outcome cannot be proven."""
    if isinstance(exc, (ProviderOutcomeUnknown, PowerOutcomeUnknownError)):
        return True
    return type(exc).__name__.endswith(("AmbiguousMutationError", "OutcomeUnknownError"))


def _safe_argument(key: str, value: Any) -> str:
    """One audited operation argument, with addresses masked (§22).

    Arguments are identifiers (an image ref, a snapshot id, an IP), never a
    secret — but an IP is customer-identifying, so it is masked before it lands
    in the audit trail or a business event.
    """
    text = str(value)
    return _mask_ip(text) if key == "ip" else text


def _mask_ip(ip: str) -> str:
    """Mask the last octet/group of an address for logs and events."""
    text = str(ip or "")
    if "." in text:
        head, _, _ = text.rpartition(".")
        return f"{head}.0" if head else text
    if ":" in text:
        head, _, _ = text.rpartition(":")
        return f"{head}:0" if head else text
    return text


def _clean_display_name(value: str) -> str:
    """Validate a customer-chosen display name (length + character policy)."""
    text = (value or "").strip()
    if not text:
        raise ServerOperationNotAllowedError(
            "display name must not be empty",
            operation=ServerOperation.RENAME,
            reason="invalid_name",
        )
    if len(text) > _MAX_DISPLAY_NAME:
        raise ServerOperationNotAllowedError(
            f"display name must be at most {_MAX_DISPLAY_NAME} characters",
            operation=ServerOperation.RENAME,
            reason="invalid_name",
        )
    if any(char not in _ALLOWED_DISPLAY_NAME for char in text):
        raise ServerOperationNotAllowedError(
            "display name contains unsupported characters",
            operation=ServerOperation.RENAME,
            reason="invalid_name",
        )
    return text


def _safe_name(value: str) -> str:
    """A snapshot name that is always acceptable to the provider."""
    return _clean_display_name(value)[:60]


def _safe_hostname(value: str) -> str:
    """Validate a reverse-DNS value (empty clears it, per provider contract)."""
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) > 253:
        raise ServerOperationNotAllowedError(
            "reverse DNS is too long",
            operation=ServerOperation.IP_SET_RDNS,
            reason="invalid_hostname",
        )
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-.")
    if any(char not in allowed for char in text):
        raise ServerOperationNotAllowedError(
            "reverse DNS contains unsupported characters",
            operation=ServerOperation.IP_SET_RDNS,
            reason="invalid_hostname",
        )
    return text
