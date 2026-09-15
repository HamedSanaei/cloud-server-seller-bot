"""Leaseweb multi-credential-account support (LEASEWEB-MULTIACCOUNT).

A Leaseweb API key / Sales Organization has a narrow location scope, so one
key cannot serve every datacenter we want to sell. This module lets the
platform hold MANY Leaseweb credentials while presenting ONE logical provider::

    customer sees:   Leaseweb -> Frankfurt, Amsterdam, Singapore
    infrastructure:  leaseweb/lw-eu    -> Frankfurt, Amsterdam
                     leaseweb/lw-asia  -> Singapore
                     leaseweb/lw-us    -> (auth failed, draining)

Discipline this module guarantees:

- **One adapter per account.** Every credential account gets its own
  :class:`~cloud_platform.providers.leaseweb.transport.LeasewebTransport`, HTTP
  client, throttle state and
  :class:`~cloud_platform.providers.credentials.CredentialHolder`. A request
  issued for ``lw-1`` can never carry ``lw-2``'s ``X-LSW-Auth`` header, and one
  account's backoff never throttles another.
- **Stable, non-secret identity.** An account is addressed by its ``id``
  (``lw-eu``) for the whole lifetime of every order/server/inventory row it
  created — across API-key rotation, which swaps the holder's value but never
  the id.
- **No cross-account fallback.** :meth:`LeasewebAccountRouter.client_for` raises
  :class:`~cloud_platform.providers.routing.UnknownCredentialAccountError` for
  an unconfigured account instead of quietly returning a different one.
  Falling back could place a second billable order or address a customer's
  server with credentials that do not own it.
- **No secrets in logs.** Accounts are only ever described through
  :class:`~cloud_platform.providers.routing.CredentialAccountView`, which
  carries the credential *fingerprint*, never the key.

Location discovery is NOT configured here: each account discovers its own
eligible locations from live read-only provider responses (see
``ordering_sync``), and the durable per-location observation lives in the
``provider_routes`` table.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from cloud_platform.providers.credentials import CredentialHolder, CredentialSource
from cloud_platform.providers.leaseweb.client import Throttle
from cloud_platform.providers.leaseweb.errors import (
    LeasewebAuthenticationError,
    LeasewebError,
)
from cloud_platform.providers.leaseweb.ordering import LeaseWebOrderingProvider
from cloud_platform.providers.leaseweb.transport import DEFAULT_BASE_URL
from cloud_platform.providers.routing import (
    CredentialAccountState,
    CredentialAccountView,
    UnknownCredentialAccountError,
    normalize_account_id,
)

logger = logging.getLogger(__name__)

__all__ = [
    "PROVIDER_KEY",
    "LeasewebAccountHealth",
    "LeasewebAccountRouter",
    "LeasewebCredentialAccount",
    "LeasewebHealthReport",
    "build_leaseweb_account_router",
    "leaseweb_accounts_from_settings",
]

#: Provider-neutral provider key. The customer-facing storefront, the catalog
#: and the domain all use this ONE key regardless of how many API keys back it.
PROVIDER_KEY = "leaseweb"


@dataclass(frozen=True, slots=True)
class LeasewebCredentialAccount:
    """One Leaseweb API credential — OUR infrastructure account.

    Deliberately distinct from the end-customer's provider link
    (``modules.provider_accounts``): this is a reseller credential, never
    pinned to a customer and never customer-visible.
    """

    account_id: str
    api_key: str = field(repr=False)
    enabled: bool = True
    priority: int = 100
    state: CredentialAccountState = CredentialAccountState.ACTIVE

    def __post_init__(self) -> None:
        account_id = normalize_account_id(self.account_id)
        if not account_id or not account_id.strip():
            raise ValueError("account_id must not be empty")
        if self.enabled and not (self.api_key or "").strip():
            raise ValueError(f"credential account {account_id!r} is enabled without an api_key")
        if self.priority < 0:
            raise ValueError("priority must be >= 0")
        if not isinstance(self.state, CredentialAccountState):
            object.__setattr__(self, "state", CredentialAccountState(str(self.state)))

    def __repr__(self) -> str:
        return (
            f"LeasewebCredentialAccount(account_id={self.account_id!r}, "
            f"enabled={self.enabled!r}, priority={self.priority!r}, "
            f"state={self.state.value!r}, api_key=<redacted len={len(self.api_key)}>)"
        )

    __str__ = __repr__

    @property
    def accepts_new_orders(self) -> bool:
        """Whether this account may receive NEW billable business."""
        return self.enabled and self.state is CredentialAccountState.ACTIVE

    @property
    def usable(self) -> bool:
        """Whether this account may still address what it already owns."""
        return self.state is not CredentialAccountState.DISABLED

    def view(self, *, key_hint: str = "") -> CredentialAccountView:
        """Safe, loggable metadata (never the credential value)."""
        return CredentialAccountView(
            provider_key=PROVIDER_KEY,
            account_id=self.account_id,
            state=self.state,
            priority=self.priority,
            key_hint=key_hint,
        )


@dataclass(frozen=True, slots=True)
class LeasewebAccountHealth:
    """Per-account authentication outcome (safe to log and print)."""

    account_id: str
    ok: bool
    error_class: str | None = None

    def __repr__(self) -> str:
        detail = self.error_class or "ok"
        return f"LeasewebAccountHealth(account_id={self.account_id!r}, {detail})"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class LeasewebHealthReport:
    """Aggregate health across every configured Leaseweb credential account.

    One failing key must never look like a dead provider: the aggregate is
    ``degraded`` while at least one account works and ``unavailable`` only when
    none does. An empty report is ``not_configured``.
    """

    accounts: tuple[LeasewebAccountHealth, ...]

    @property
    def healthy(self) -> tuple[str, ...]:
        return tuple(account.account_id for account in self.accounts if account.ok)

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(account.account_id for account in self.accounts if not account.ok)

    @property
    def status(self) -> str:
        """``ok`` | ``degraded`` | ``unavailable`` | ``not_configured``."""
        if not self.accounts:
            return "not_configured"
        if not self.failed:
            return "ok"
        if self.healthy:
            return "degraded"
        return "unavailable"


def build_leaseweb_account_router(settings: Any) -> LeasewebAccountRouter | None:
    """Build the multi-account router from settings, or ``None`` for legacy.

    Shared by the application container, the arq worker and the CLI so every
    process constructs the SAME set of adapters from the SAME server-owned
    configuration. Returns ``None`` when no credential accounts are configured
    (the deprecated single ``api_key`` path then owns the provider).
    """
    accounts = leaseweb_accounts_from_settings(settings)
    if not accounts:
        return None
    return LeasewebAccountRouter(
        accounts,
        base_url=getattr(settings, "leaseweb_api_base_url", DEFAULT_BASE_URL),
        locations=tuple(
            part.strip()
            for part in (getattr(settings, "leaseweb_locations", "") or "").split(",")
            if part.strip()
        ),
        os_allowlist=tuple(
            part.strip()
            for part in (getattr(settings, "leaseweb_os_allowlist", "") or "").split(",")
            if part.strip()
        ),
        order_os_only_free=bool(getattr(settings, "leaseweb_order_os_only_free", True)),
        contract_term=getattr(settings, "leaseweb_contract_term", "1_MONTH"),
        billing_cycle=getattr(settings, "leaseweb_billing_cycle", "1_MONTH"),
        timeout_seconds=float(getattr(settings, "leaseweb_timeout_seconds", 30.0)),
    )


def leaseweb_accounts_from_settings(settings: Any) -> list[LeasewebCredentialAccount]:
    """Build credential accounts from settings (legacy single key included).

    ``Settings`` normalizes the deprecated ``leaseweb_api_key`` into account
    ``default``, so this is a straight translation with no special cases and
    never inspects a raw secret beyond handing it to the adapter.
    """
    accounts: list[LeasewebCredentialAccount] = []
    for account in getattr(settings, "leaseweb_accounts", None) or ():
        accounts.append(
            LeasewebCredentialAccount(
                account_id=account.id,
                api_key=account.api_key,
                enabled=account.enabled,
                priority=account.priority,
                state=CredentialAccountState(account.normalized_state),
            )
        )
    return accounts


class LeasewebAccountRouter:
    """Builds and owns ONE adapter (and transport) per Leaseweb credential.

    The router is the only place that knows which API key serves which account.
    Application and domain code resolve an adapter through
    :meth:`client_for` using the opaque account id pinned on the resource.
    """

    def __init__(
        self,
        accounts: Iterable[LeasewebCredentialAccount],
        *,
        base_url: str = DEFAULT_BASE_URL,
        locations: tuple[str, ...] = (),
        os_allowlist: tuple[str, ...] = (),
        order_os_only_free: bool = True,
        contract_term: str = "1_MONTH",
        billing_cycle: str = "1_MONTH",
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        credential_sources: Mapping[str, CredentialSource] | None = None,
        throttle_factory: Callable[[], Throttle] | None = None,
        provider_factory: Callable[..., LeaseWebOrderingProvider] | None = None,
    ) -> None:
        self._base_url = base_url
        self._locations = tuple(locations)
        self._os_allowlist = tuple(os_allowlist)
        self._order_os_only_free = order_os_only_free
        self._contract_term = contract_term
        self._billing_cycle = billing_cycle
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._credential_sources = dict(credential_sources or {})
        self._throttle_factory = throttle_factory or Throttle
        self._provider_factory = provider_factory or LeaseWebOrderingProvider

        self._accounts: dict[str, LeasewebCredentialAccount] = {}
        self._providers: dict[str, LeaseWebOrderingProvider] = {}
        self._holders: dict[str, CredentialHolder] = {}
        for account in accounts:
            if account.account_id in self._accounts:
                raise ValueError(f"duplicate leaseweb account id: {account.account_id!r}")
            self._accounts[account.account_id] = account
            if not account.enabled:
                continue
            holder = CredentialHolder(account.api_key)
            self._holders[account.account_id] = holder
            self._providers[account.account_id] = self._build_provider(
                account, holder, self._credential_sources.get(account.account_id)
            )

    def _build_provider(
        self,
        account: LeasewebCredentialAccount,
        holder: CredentialHolder,
        credential_source: CredentialSource | None,
    ) -> LeaseWebOrderingProvider:
        """One adapter per account: its OWN transport, client and throttle."""
        return self._provider_factory(
            api_key=account.api_key,
            base_url=self._base_url,
            locations=self._locations,
            contract_term=self._contract_term,
            billing_cycle=self._billing_cycle,
            os_allowlist=self._os_allowlist,
            order_os_only_free=self._order_os_only_free,
            # A per-account throttle: one key hitting its rate limit must not
            # slow down a different key.
            throttle=self._throttle_factory(),
            max_retries=self._max_retries,
            credential_source=credential_source or holder,
            timeout_seconds=self._timeout_seconds,
        )

    # ------------------------------------------------------------------
    # Identity and lookup
    # ------------------------------------------------------------------

    @property
    def accounts(self) -> tuple[LeasewebCredentialAccount, ...]:
        """Every configured account, ordered by (priority, id)."""
        return tuple(sorted(self._accounts.values(), key=lambda a: (a.priority, a.account_id)))

    @property
    def account_ids(self) -> tuple[str, ...]:
        return tuple(account.account_id for account in self.accounts)

    @property
    def providers(self) -> Mapping[str, LeaseWebOrderingProvider]:
        """Adapter instances keyed by account id (enabled accounts only)."""
        return dict(self._providers)

    @property
    def ordered_providers(self) -> dict[str, LeaseWebOrderingProvider]:
        """Adapters in deterministic ``(priority, account_id)`` order.

        The catalog sync aggregates in this order, so the highest-priority
        credential that can supply a product/location always contributes the
        provider-cost snapshot — the same configuration always produces the
        same catalog.
        """
        return {
            account_id: self._providers[account_id]
            for account_id in sorted(
                self._providers,
                key=lambda account_id: (
                    self._accounts[account_id].priority,
                    account_id,
                ),
            )
        }

    @property
    def priorities(self) -> dict[str, int]:
        """Configured selection priority per account (lower wins)."""
        return {account_id: account.priority for account_id, account in self._accounts.items()}

    @property
    def account_states(self) -> dict[str, CredentialAccountState]:
        """Configured lifecycle state per account (active/draining/disabled)."""
        return {account_id: account.state for account_id, account in self._accounts.items()}

    @property
    def credential_holders(self) -> Mapping[str, CredentialHolder]:
        """The per-account credential holders (for runtime rotation)."""
        return dict(self._holders)

    def has_account(self, account_id: str | None) -> bool:
        return normalize_account_id(account_id) in self._accounts

    def has_provider(self, account_id: str | None) -> bool:
        """Whether a live adapter exists for this account (not enabled-only)."""
        return normalize_account_id(account_id) in self._providers

    def account(self, account_id: str | None) -> LeasewebCredentialAccount:
        """The configured account record (raises when unknown)."""
        key = normalize_account_id(account_id)
        try:
            return self._accounts[key]
        except KeyError as exc:
            raise UnknownCredentialAccountError(PROVIDER_KEY, key) from exc

    def client_for(self, account_id: str | None) -> LeaseWebOrderingProvider:
        """The adapter for a PINNED credential account.

        Raises:
            UnknownCredentialAccountError: the account is not configured, or is
                configured but disabled. Never falls back to another account.
        """
        key = normalize_account_id(account_id)
        try:
            return self._providers[key]
        except KeyError as exc:
            raise UnknownCredentialAccountError(PROVIDER_KEY, key) from exc

    def new_order_clients(self) -> tuple[tuple[str, LeaseWebOrderingProvider], ...]:
        """Enabled ACTIVE accounts in deterministic order, with adapters.

        Deterministic by construction — ``(priority, account_id)`` — so the
        same configuration always picks the same fulfillment account for a
        location. Never a random dictionary-order choice, and never a
        cheapest-cost choice (that would silently reprice customer offers).
        """
        ordered = sorted(
            self._providers.items(),
            key=lambda item: (self._accounts[item[0]].priority, item[0]),
        )
        return tuple(
            (account_id, provider)
            for account_id, provider in ordered
            if self._accounts[account_id].accepts_new_orders
        )

    def views(self) -> tuple[CredentialAccountView, ...]:
        """Safe operator metadata for every account (no credential values)."""
        return tuple(
            account.view(key_hint=self._holders[account.account_id].key_hint)
            if account.account_id in self._holders
            else account.view()
            for account in self.accounts
        )

    def enabled_account_ids(self) -> tuple[str, ...]:
        return tuple(account.account_id for account in self.accounts if account.enabled)

    # ------------------------------------------------------------------
    # Read-only health
    # ------------------------------------------------------------------

    async def verify_account(self, account_id: str) -> LeasewebAccountHealth:
        """Authenticate ONE account with a read-only call (never billable)."""
        try:
            provider = self.client_for(account_id)
        except UnknownCredentialAccountError:
            return LeasewebAccountHealth(account_id, ok=False, error_class="NotConfigured")
        verify = getattr(provider, "verify_credential", None)
        if not callable(verify):
            return LeasewebAccountHealth(account_id, ok=True)
        try:
            holder = self._holders.get(normalize_account_id(account_id))
            credential = await holder.get() if holder is not None else None
            await verify(credential.value if credential is not None else "")
        except LeasewebAuthenticationError:
            return LeasewebAccountHealth(account_id, ok=False, error_class="AuthenticationError")
        except LeasewebError as exc:
            return LeasewebAccountHealth(account_id, ok=False, error_class=type(exc).__name__)
        except Exception as exc:
            logger.warning(
                "leaseweb account %s verification failed inconclusively: %s",
                account_id,
                type(exc).__name__,
            )
            return LeasewebAccountHealth(account_id, ok=False, error_class=type(exc).__name__)
        return LeasewebAccountHealth(account_id, ok=True)

    async def verify_all(self) -> LeasewebHealthReport:
        """Authenticate every configured account; the aggregate never raises."""
        results = [await self.verify_account(account_id) for account_id in self.account_ids]
        return LeasewebHealthReport(tuple(results))

    async def aclose(self) -> None:
        """Close every per-account transport (best effort)."""
        for account_id, provider in self._providers.items():
            close = getattr(provider, "close", None)
            if not callable(close):
                continue
            try:
                await close()
            except Exception:  # pragma: no cover - shutdown must not raise
                logger.warning("leaseweb account %s close failed", account_id)
