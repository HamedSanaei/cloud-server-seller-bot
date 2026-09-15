"""Provider-neutral credential-account routing (LEASEWEB-MULTIACCOUNT).

One logical provider (``leaseweb``) may be served by SEVERAL provider
credential accounts — distinct API keys / Sales Organizations with their own
location scope, throttling and inventory. Customers must never see that
split: the storefront still says **Leaseweb** and the domain still uses ONE
provider key. Only the infrastructure layer knows which credential account
actually fulfils a given resource.

This module owns the vocabulary every layer shares:

- :data:`DEFAULT_CREDENTIAL_ACCOUNT` — the account id a legacy single-key
  configuration maps onto, so pre-existing servers/orders stay routable.
- :class:`CredentialAccountState` — the operator-visible lifecycle of one
  credential account (active / draining / disabled).
- :class:`CredentialAccountView` — SAFE metadata about one account (id,
  state, priority, credential fingerprint). Never the credential value.
- :class:`UnknownCredentialAccountError` — a pinned account that is no longer
  configured. This is a FAIL-CLOSED signal: the platform must never silently
  fall back to another credential account, because that would either place a
  billable order through the wrong account or address a customer's server
  with credentials that do not own it.
- :class:`ProviderRouter` — the port application code resolves a provider
  adapter through, given an opaque credential-account id.

The domain modules depend only on this port and on the opaque account id
string; they never learn that a Leaseweb API key exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "DEFAULT_CREDENTIAL_ACCOUNT",
    "CredentialAccountState",
    "CredentialAccountView",
    "ProviderRouter",
    "UnknownCredentialAccountError",
    "account_state_accepts_new_orders",
    "account_state_is_usable",
    "normalize_account_id",
    "provider_for",
]

#: The account id a legacy single-credential configuration maps onto. Existing
#: rows created before multi-account support are backfilled with this value so
#: they never become un-routable.
DEFAULT_CREDENTIAL_ACCOUNT = "default"


class CredentialAccountState(StrEnum):
    """Operator-visible lifecycle of ONE provider credential account.

    The distinction that matters is between receiving NEW business and still
    being able to MANAGE what that account already owns. Draining an account
    must never make existing customer servers unmanageable.
    """

    ACTIVE = "active"
    """Discovery, new orders and management of existing resources."""

    DRAINING = "draining"
    """No new orders; existing resources stay fully manageable."""

    DISABLED = "disabled"
    """No normal use; an operator must intervene (resources fail closed)."""


def account_state_accepts_new_orders(state: CredentialAccountState) -> bool:
    """Whether a credential account may receive NEW billable business."""
    return state is CredentialAccountState.ACTIVE


def account_state_is_usable(state: CredentialAccountState) -> bool:
    """Whether a credential account may still address what it already owns."""
    return state is not CredentialAccountState.DISABLED


def normalize_account_id(account_id: str | None) -> str:
    """Normalize an account id, mapping the empty/absent case to the default.

    Legacy rows (and single-credential deployments) carry no account id; they
    are routed to :data:`DEFAULT_CREDENTIAL_ACCOUNT` exactly like the value the
    backfill migration writes, so both spellings resolve identically.
    """
    candidate = (account_id or "").strip()
    return candidate or DEFAULT_CREDENTIAL_ACCOUNT


class UnknownCredentialAccountError(KeyError):
    """A resource is pinned to a credential account that is not configured.

    Deliberately NOT a fallback trigger: routing a pinned resource through a
    different credential account could place a second billable order or
    address a server with credentials that do not own it. Callers must fail
    closed and surface the situation to an operator.
    """

    def __init__(self, provider_key: str, account_id: str) -> None:
        self.provider_key = provider_key
        self.account_id = account_id
        super().__init__(
            f"credential account {account_id!r} for provider {provider_key!r} is not "
            f"configured; resources pinned to it fail closed until it is restored"
        )


@dataclass(frozen=True, slots=True)
class CredentialAccountView:
    """SAFE, loggable metadata about one provider credential account.

    Carries no credential material: ``key_hint`` is the non-reversible
    fingerprint from :mod:`cloud_platform.providers.credentials`.
    """

    provider_key: str
    account_id: str
    state: CredentialAccountState = CredentialAccountState.ACTIVE
    priority: int = 100
    key_hint: str = ""

    @property
    def enabled_for_new_orders(self) -> bool:
        """Whether this account may receive new billable business."""
        return account_state_accepts_new_orders(self.state)

    @property
    def usable(self) -> bool:
        """Whether this account may still address what it already owns."""
        return account_state_is_usable(self.state)

    @property
    def ref(self) -> str:
        """Stable ``provider/account`` reference for logs and diagnostics."""
        return f"{self.provider_key}/{self.account_id}"


def provider_for(
    registry: Any,
    provider_key: str,
    credential_account_id: str | None = None,
) -> Any:
    """Resolve the adapter serving a resource pinned to a credential account.

    The one-liner exists so every call site resolves a PINNED resource the
    same way, and so a registry that predates credential accounts (a test
    double, or a single-credential implementation) still resolves logically.
    The fail-closed guarantee is preserved: a registry that DOES understand
    credential accounts raises
    :class:`UnknownCredentialAccountError` for an unconfigured account rather
    than falling back to another one.
    """
    resolver = getattr(registry, "get_for", None)
    if callable(resolver):
        return resolver(provider_key, credential_account_id)
    return registry.get(provider_key)


@runtime_checkable
class ProviderRouter(Protocol):
    """Resolve a provider adapter for an opaque credential account.

    ``credential_account_id`` is an opaque, non-secret string (e.g. ``lw-2``).
    ``None`` means "the logical provider" and resolves to the provider key's
    default adapter, which is what provider-neutral catalog/storefront code
    uses.
    """

    def provider_for(self, provider_key: str, credential_account_id: str | None = None) -> Any:
        """The adapter instance serving ``credential_account_id``.

        Raises:
            KeyError: the provider key is unknown.
            UnknownCredentialAccountError: the provider exists but the pinned
                account does not (fail closed — never a silent fallback).
        """
        ...

    def accounts(self, provider_key: str) -> tuple[CredentialAccountView, ...]:
        """Every configured credential account of one provider (safe fields)."""
        ...
