import logging

from cloud_platform.providers.base import CloudProvider
from cloud_platform.providers.routing import (
    DEFAULT_CREDENTIAL_ACCOUNT,
    CredentialAccountView,
    UnknownCredentialAccountError,
    normalize_account_id,
)

logger = logging.getLogger(__name__)


class ProviderRegistry:
    """The process-wide map of provider KEY -> adapter.

    A provider key stays logical (``leaseweb``): customers, the catalog and the
    storefront never learn that several API keys may back it. When a provider
    is served by SEVERAL credential accounts, each account gets its own adapter
    instance registered as a ROUTE under the same provider key
    (:meth:`register_route`), and pinned resources resolve theirs through
    :meth:`get_for`.

    Routing discipline (LEASEWEB-MULTIACCOUNT):

    - :meth:`get` always returns the LOGICAL adapter. It is what catalog
      discovery, the storefront and other credential-agnostic code use.
    - :meth:`get_for` resolves a resource's PINNED credential account. An
      account that is no longer configured is a fail-closed error, never a
      silent fallback: falling back could place a second billable order
      through another account or address a server with credentials that do not
      own it.
    """

    def __init__(self) -> None:
        self._providers: dict[str, CloudProvider] = {}
        self._routes: dict[str, dict[str, CloudProvider]] = {}
        self._account_views: dict[str, tuple[CredentialAccountView, ...]] = {}

    def register(self, provider: CloudProvider) -> None:
        if provider.key in self._providers:
            raise ValueError(f"provider already registered: {provider.key}")
        self._providers[provider.key] = provider

    def register_route(
        self, provider_key: str, credential_account_id: str, provider: CloudProvider
    ) -> None:
        """Register one credential account's adapter under a logical key.

        The first route registered for a key also becomes the key's default
        adapter, so plain :meth:`get` keeps working for credential-agnostic
        callers.
        """
        account_id = normalize_account_id(credential_account_id)
        routes = self._routes.setdefault(provider_key, {})
        if account_id in routes:
            raise ValueError(f"credential account already registered: {provider_key}/{account_id}")
        routes[account_id] = provider
        self._providers.setdefault(provider_key, provider)

    def get(self, key: str) -> CloudProvider:
        try:
            return self._providers[key]
        except KeyError as exc:
            raise KeyError(f"unknown provider: {key}") from exc

    def get_for(self, key: str, credential_account_id: str | None = None) -> CloudProvider:
        """Resolve the adapter for a RESOURCE pinned to a credential account.

        ``None`` (legacy rows, credential-agnostic callers) resolves to the
        logical adapter, which is exactly what a single-credential deployment
        registered.

        Raises:
            KeyError: unknown provider key.
            UnknownCredentialAccountError: the provider is registered but the
                pinned account is not — fail closed.
        """
        if credential_account_id is None:
            return self.get(key)
        routes = self._routes.get(key)
        if not routes:
            # Single-credential provider: any account id maps to the one
            # adapter. Legacy rows keep working without a backfill.
            return self.get(key)
        account_id = normalize_account_id(credential_account_id)
        provider = routes.get(account_id)
        if provider is None:
            if account_id == DEFAULT_CREDENTIAL_ACCOUNT:
                # ``default`` is a RESERVED alias for "the provider's pre-
                # multi-account credential". Migration 0035 backfills every
                # pre-existing Leaseweb row with exactly this id, so an
                # operator who renames their accounts must not strand the
                # servers and orders those rows describe. Every OTHER unknown
                # account still fails closed.
                logger.warning(
                    "provider %s has no account %r; resolving the reserved "
                    "legacy alias to its logical adapter (accounts: %s)",
                    key,
                    account_id,
                    ", ".join(sorted(routes)),
                )
                return self.get(key)
            raise UnknownCredentialAccountError(key, account_id)
        return provider

    def account_of(self, provider: CloudProvider) -> str | None:
        """The credential account id an adapter instance is registered under."""
        for provider_key, routes in self._routes.items():
            for account_id, candidate in routes.items():
                if candidate is provider:
                    return account_id
            if self._providers.get(provider_key) is provider:
                return normalize_account_id(None)
        return None

    def route_ids(self, key: str) -> tuple[str, ...]:
        """Every registered credential account id of one provider (sorted)."""
        return tuple(sorted(self._routes.get(key, {})))

    def has_routes(self, key: str) -> bool:
        """Whether a provider is served by explicit credential accounts."""
        return bool(self._routes.get(key))

    def register_account_views(
        self, provider_key: str, views: tuple[CredentialAccountView, ...]
    ) -> None:
        """Attach safe operator metadata (state/priority/hint) to a provider."""
        self._account_views[provider_key] = tuple(views)

    def accounts(self, provider_key: str) -> tuple[CredentialAccountView, ...]:
        """Safe credential-account metadata for one provider (may be empty)."""
        return self._account_views.get(provider_key, ())

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))
