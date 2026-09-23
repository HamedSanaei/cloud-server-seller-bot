"""Leaseweb hourly-cloud credential accounts (multi-account Public Cloud).

Public Cloud is credential/account scoped exactly like VPS ordering: one API
key (Sales Organization) sees its own regions and instance types. A single
arbitrary key must never be assumed to own Cloud — every ACTIVE account is
probed read-only and regions/types are attributed per account.

Analogous in principle to the VPS account router
(:mod:`cloud_platform.providers.leaseweb.accounts`):

- **One adapter per account.** Every credential account gets its own
  :class:`LeasewebHourlyCloudProvider` (own transport, throttle, key). A
  request issued for one account can never carry another's ``X-LSW-Auth``.
- **Stable, non-secret identity.** Accounts are addressed by id; API keys
  never appear in logs, errors or diagnostics.
- **No cross-account fallback.** Resolving an unconfigured account raises
  instead of quietly returning another account's adapter.
- **No mutation while probing.** Discovery is regions + instance-type reads
  only.
- **Lifecycle parity.** Draining/disabled semantics follow
  :class:`CredentialAccountState`: disabled accounts are never probed,
  draining accounts still provide read evidence but receive no new business.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from cloud_platform.providers.errors import ProviderAuthError
from cloud_platform.providers.leaseweb.accounts import (
    PROVIDER_KEY,
    LeasewebCredentialAccount,
    leaseweb_accounts_from_settings,
)
from cloud_platform.providers.leaseweb.cloud import LeasewebHourlyCloudProvider
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
    "CloudAccountCapability",
    "LeasewebCloudAccountRouter",
    "build_cloud_account_router",
]


@dataclass(frozen=True, slots=True)
class CloudAccountCapability:
    """Read-only Public Cloud capability of one credential account.

    ``accessible`` means the regions read succeeded AND at least one region
    serves instance types. An account whose regions read fails carries
    ``error_class`` (``AuthenticationError`` for rejected keys, otherwise the
    exception class = transient/unknown, never "no Cloud").

    The shape counters let diagnostics distinguish "the endpoint returned
    nothing" (``regions_raw_items == 0``) from "the response was not
    recognized" (raw items present, nothing parsed) and "types exist but are
    unpriced" — all of which surface as ``accessible=False`` with
    ``error_class=None``.
    """

    account_id: str
    accessible: bool
    regions: tuple[tuple[str, int], ...] = ()
    error_class: str | None = None
    regions_seen: int = 0
    regions_raw_items: int = 0
    types_raw_items: int = 0
    types_priced_items: int = 0
    types_currency: str | None = None


def build_cloud_account_router(settings: Any) -> LeasewebCloudAccountRouter | None:
    """Build the hourly-cloud account router from settings, or None.

    Shared by the worker, the container and the CLI so every process probes
    the SAME accounts. Returns ``None`` when no credential account exists —
    callers skip the hourly product instead of failing.
    """
    accounts = leaseweb_accounts_from_settings(settings)
    if not accounts:
        return None
    return LeasewebCloudAccountRouter(
        accounts,
        base_url=getattr(settings, "leaseweb_api_base_url", DEFAULT_BASE_URL),
        timeout_seconds=float(getattr(settings, "leaseweb_timeout_seconds", 30.0)),
    )


class LeasewebCloudAccountRouter:
    """One hourly-cloud adapter per Leaseweb credential account.

    Implements the :class:`ProviderRouter` port: application code resolves an
    adapter through :meth:`provider_for` using the opaque account id pinned
    on the resource.
    """

    def __init__(
        self,
        accounts: Iterable[LeasewebCredentialAccount],
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds
        self._accounts: dict[str, LeasewebCredentialAccount] = {}
        self._providers: dict[str, LeasewebHourlyCloudProvider] = {}
        for account in accounts:
            if account.account_id in self._accounts:
                raise ValueError(f"duplicate leaseweb account id: {account.account_id!r}")
            self._accounts[account.account_id] = account
            if not account.enabled:
                continue
            self._providers[account.account_id] = LeasewebHourlyCloudProvider(
                api_key=account.api_key,
                base_url=base_url,
                timeout_seconds=timeout_seconds,
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
    def providers(self) -> Mapping[str, LeasewebHourlyCloudProvider]:
        """Adapter instances keyed by account id (enabled accounts only)."""
        return dict(self._providers)

    @property
    def priorities(self) -> dict[str, int]:
        """Configured selection priority per account (lower wins)."""
        return {account_id: account.priority for account_id, account in self._accounts.items()}

    @property
    def account_states(self) -> dict[str, CredentialAccountState]:
        """Configured lifecycle state per account (active/draining/disabled)."""
        return {account_id: account.state for account_id, account in self._accounts.items()}

    def provider_for(
        self, provider_key: str, credential_account_id: str | None = None
    ) -> LeasewebHourlyCloudProvider:
        """The adapter for a PINNED credential account (never a fallback)."""
        if provider_key != PROVIDER_KEY:
            raise KeyError(provider_key)
        key = normalize_account_id(credential_account_id)
        try:
            return self._providers[key]
        except KeyError as exc:
            raise UnknownCredentialAccountError(PROVIDER_KEY, key) from exc

    def client_for(self, account_id: str | None) -> LeasewebHourlyCloudProvider:
        """Alias of :meth:`provider_for` for the leaseweb provider key."""
        return self.provider_for(PROVIDER_KEY, account_id)

    def views(self) -> tuple[CredentialAccountView, ...]:
        """Safe operator metadata for every account (no credential values)."""
        return tuple(
            CredentialAccountView(
                provider_key=PROVIDER_KEY,
                account_id=account.account_id,
                state=account.state,
                priority=account.priority,
            )
            for account in self.accounts
        )

    def new_cloud_clients(self) -> tuple[tuple[str, LeasewebHourlyCloudProvider], ...]:
        """Enabled ACTIVE accounts in deterministic order, with adapters."""
        ordered = sorted(
            self._providers.items(),
            key=lambda item: (self._accounts[item[0]].priority, item[0]),
        )
        return tuple(
            (account_id, provider)
            for account_id, provider in ordered
            if self._accounts[account_id].accepts_new_orders
        )

    # ------------------------------------------------------------------
    # Read-only capability probing
    # ------------------------------------------------------------------

    async def probe_account(self, account_id: str) -> CloudAccountCapability:
        """regions + per-region type counts for one account (never mutates).

        Authentication failures and empty catalogs both mean "this account
        has no usable Cloud" (ineligible); transport failures are UNKNOWN
        (the account might serve Cloud — callers must not retire on it).
        Shape counters (parsed vs raw) travel on every ``accessible=False``
        outcome without an ``error_class`` so the doctor can tell an empty
        catalog from an unrecognized response.
        """
        try:
            provider = self.client_for(account_id)
        except UnknownCredentialAccountError:
            return CloudAccountCapability(account_id, accessible=False, error_class="NotConfigured")
        try:
            regions_read = await provider.read_regions()
        except ProviderAuthError:
            return CloudAccountCapability(
                account_id, accessible=False, error_class="AuthenticationError"
            )
        except Exception as exc:
            logger.warning(
                "leaseweb cloud probe of account %s regions inconclusive: %s",
                account_id,
                type(exc).__name__,
            )
            return CloudAccountCapability(
                account_id, accessible=False, error_class=type(exc).__name__
            )
        regions = regions_read.regions
        counts: list[tuple[str, int]] = []
        failed = False
        types_raw = 0
        types_priced = 0
        regions_seen = len(regions)
        regions_raw = regions_read.raw_items
        types_currency: str | None = None
        for region in regions:
            try:
                types_read = await provider.read_instance_types(region.id)
            except ProviderAuthError:
                return CloudAccountCapability(
                    account_id, accessible=False, error_class="AuthenticationError"
                )
            except Exception as exc:
                logger.warning(
                    "leaseweb cloud probe of account %s region %s inconclusive: %s",
                    account_id,
                    region.id,
                    type(exc).__name__,
                )
                failed = True
                continue
            types_raw += types_read.raw_items
            types_priced += types_read.priced_items
            if types_currency is None:
                types_currency = types_read.currency
            counts.append((region.id, len(types_read.types)))
        if failed:
            return CloudAccountCapability(
                account_id,
                accessible=False,
                error_class="RegionError",
                regions_seen=regions_seen,
                regions_raw_items=regions_raw,
                types_raw_items=types_raw,
                types_priced_items=types_priced,
                types_currency=types_currency,
            )
        total = sum(count for _region, count in counts)
        if total == 0:
            return CloudAccountCapability(
                account_id,
                accessible=False,
                regions_seen=regions_seen,
                regions_raw_items=regions_raw,
                types_raw_items=types_raw,
                types_priced_items=types_priced,
                types_currency=types_currency,
            )
        return CloudAccountCapability(
            account_id,
            accessible=True,
            regions=tuple(counts),
            regions_seen=regions_seen,
            regions_raw_items=regions_raw,
            types_raw_items=types_raw,
            types_priced_items=types_priced,
            types_currency=types_currency,
        )

    async def aclose(self) -> None:
        """Close every per-account transport (best effort)."""
        for account_id, provider in self._providers.items():
            try:
                await provider.close()
            except Exception:  # pragma: no cover - shutdown must not raise
                logger.warning("leaseweb cloud account %s close failed", account_id)
