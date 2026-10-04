"""One managed Hetzner Project credential per sticky account route."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from cloud_platform.providers.base import AccountServerUsage
from cloud_platform.providers.credentials import CredentialHolder
from cloud_platform.providers.hetzner.client import HetznerCloudProvider
from cloud_platform.providers.hetzner.hourly import HetznerHourlyCloudProvider
from cloud_platform.providers.routing import (
    CredentialAccountState,
    CredentialAccountView,
    UnknownCredentialAccountError,
    normalize_account_id,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HetznerCredentialAccount:
    account_id: str
    api_token: str = field(repr=False)
    enabled: bool = True
    priority: int = 100
    state: CredentialAccountState = CredentialAccountState.ACTIVE
    label: str = ""
    server_limit: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", normalize_account_id(self.account_id))
        object.__setattr__(self, "state", CredentialAccountState(self.state))
        if (
            self.enabled
            and self.state != CredentialAccountState.DISABLED
            and not self.api_token.strip()
        ):
            raise ValueError("managed Hetzner account has no credential")
        if self.server_limit is not None and (
            type(self.server_limit) is not int or self.server_limit <= 0
        ):
            raise ValueError("Hetzner server_limit must be a positive integer")


def build_hetzner_account_router(settings: Any) -> HetznerAccountRouter | None:
    """Settings already normalize the legacy token into the explicit default route."""
    accounts = tuple(
        HetznerCredentialAccount(
            account_id=account.id,
            api_token=account.api_token,
            enabled=account.enabled,
            priority=account.priority,
            state=account.state,
            label=account.label,
            server_limit=account.server_limit,
        )
        for account in settings.hetzner_accounts
    )
    if not accounts:
        return None
    return HetznerAccountRouter(accounts, base_url=settings.hetzner_api_base_url)


class HetznerAccountRouter:
    """New orders use active routes; management never substitutes another account."""

    def __init__(
        self,
        accounts: Iterable[HetznerCredentialAccount],
        *,
        base_url: str = "https://api.hetzner.cloud/v1",
    ) -> None:
        configured: dict[str, HetznerCredentialAccount] = {}
        for account in accounts:
            if account.account_id in configured:
                raise ValueError(f"duplicate Hetzner account id: {account.account_id!r}")
            configured[account.account_id] = account
        self.accounts = tuple(
            sorted(configured.values(), key=lambda account: (account.priority, account.account_id))
        )
        self._accounts = configured
        self._providers: dict[str, HetznerCloudProvider] = {}
        self._hourly: dict[str, HetznerHourlyCloudProvider] = {}
        self._holders: dict[str, CredentialHolder] = {}
        self._closed = False
        for account in self.accounts:
            if not account.enabled or account.state == CredentialAccountState.DISABLED:
                continue
            holder = CredentialHolder(account.api_token)
            provider = HetznerCloudProvider(
                token=account.api_token,
                base_url=base_url,
                credential_source=holder,
                account_id=account.account_id,
            )
            self._holders[account.account_id] = holder
            self._providers[account.account_id] = provider
            self._hourly[account.account_id] = HetznerHourlyCloudProvider(
                provider=provider, account_id=account.account_id
            )

    @property
    def providers(self) -> Mapping[str, HetznerCloudProvider]:
        return dict(self._providers)

    @property
    def credential_holders(self) -> Mapping[str, CredentialHolder]:
        return dict(self._holders)

    @property
    def priorities(self) -> dict[str, int]:
        return {account.account_id: account.priority for account in self.accounts}

    @property
    def account_states(self) -> dict[str, CredentialAccountState]:
        return {
            account.account_id: account.state
            if account.enabled
            else CredentialAccountState.DISABLED
            for account in self.accounts
        }

    def views(self) -> tuple[CredentialAccountView, ...]:
        return tuple(
            CredentialAccountView(
                provider_key="hetzner",
                account_id=account.account_id,
                state=self.account_states[account.account_id],
                priority=account.priority,
                key_hint=self._holders[account.account_id].key_hint
                if account.account_id in self._holders
                else "",
            )
            for account in self.accounts
        )

    def new_order_clients(self) -> tuple[tuple[str, HetznerCloudProvider], ...]:
        return tuple(
            (account.account_id, self._providers[account.account_id])
            for account in self.accounts
            if account.enabled and account.state == CredentialAccountState.ACTIVE
        )

    def client_for(self, account_id: str | None) -> HetznerCloudProvider:
        key = normalize_account_id(account_id)
        try:
            return self._providers[key]
        except KeyError as exc:
            raise UnknownCredentialAccountError("hetzner", key) from exc

    def hourly_for(self, account_id: str | None) -> HetznerHourlyCloudProvider:
        key = normalize_account_id(account_id)
        try:
            return self._hourly[key]
        except KeyError as exc:
            raise UnknownCredentialAccountError("hetzner", key) from exc

    def accepts_new_orders(self, account_id: str) -> bool:
        key = normalize_account_id(account_id)
        account = self._accounts.get(key)
        return (
            account is not None
            and account.enabled
            and account.state == CredentialAccountState.ACTIVE
            and key in self._providers
        )

    def get_for(
        self, provider_key: str, account_id: str | None = None
    ) -> HetznerHourlyCloudProvider:
        if provider_key != "hetzner":
            raise KeyError(provider_key)
        return self.hourly_for(account_id)

    async def server_usage(self, account_id: str) -> AccountServerUsage:
        provider = self.client_for(account_id)
        account = self._accounts[provider.account_id]
        return AccountServerUsage(
            credential_account_id=provider.account_id,
            server_count=await provider.project_server_count(),
            server_limit=account.server_limit,
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        for account_id, provider in self._providers.items():
            try:
                await provider.close()
            except Exception:
                logger.warning("Hetzner account %s close failed", account_id)
