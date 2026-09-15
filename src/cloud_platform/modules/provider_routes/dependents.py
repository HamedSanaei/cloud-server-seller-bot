"""Resources still pinned to a credential account that is no longer configured.

Deleting a credential account from ``configuration.toml`` does not delete the
servers and provider orders it created: those rows keep the account id they were
pinned with, and every later call for them **fails closed** (see
:class:`~cloud_platform.providers.routing.UnknownCredentialAccountError`). That
is the correct behaviour — silently routing them through another account could
address a customer's VPS with credentials that do not own it.

The operator still needs to *know*. This module answers the one question a
doctor/CLI check can ask, provider-neutrally:

    "which configured-away credential accounts still have resources pinned to
    them, and how many?"

It reads only account ids and counts: no credential material, no provider
messages, and nothing customer-identifying.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import Provider as _ProviderModel
from cloud_platform.db.base import ProviderOrder as _ProviderOrderModel
from cloud_platform.db.base import Server as _ServerModel

__all__ = [
    "CredentialAccountDependents",
    "missing_credential_account_report",
    "scan_credential_account_dependents",
]


@dataclass(frozen=True, slots=True)
class CredentialAccountDependents:
    """How many existing resources still reference one credential account."""

    provider_key: str
    credential_account_id: str
    servers: int = 0
    provider_orders: int = 0

    @property
    def total(self) -> int:
        """Every resource pinned to this account (servers + provider orders)."""
        return self.servers + self.provider_orders

    def message(self) -> str:
        """A SAFE operator sentence: account id and counts only."""
        return (
            f"credential account {self.credential_account_id!r} is missing from "
            f"configuration but {self.total} existing resource(s) depend on it "
            f"({self.servers} server(s), {self.provider_orders} provider order(s)); "
            "those resources fail closed until the account is restored"
        )


async def scan_credential_account_dependents(
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    *,
    provider_key: str,
    configured_account_ids: Iterable[str],
) -> list[CredentialAccountDependents]:
    """Accounts with pinned resources that are NOT in ``configured_account_ids``.

    Ordered by dependent count (descending) then account id, so the operator
    sees the most consequential missing credential first.
    """
    configured = {str(account_id) for account_id in configured_account_ids}
    servers: dict[str, int] = {}
    orders: dict[str, int] = {}

    async with session_factory() as session:
        server_rows = (
            await session.execute(
                select(
                    _ServerModel.credential_account_id,
                    func.count(_ServerModel.id),
                )
                .select_from(_ServerModel)
                .join(_ProviderModel, _ProviderModel.id == _ServerModel.provider_id)
                .where(
                    _ProviderModel.name == provider_key,
                    _ServerModel.credential_account_id.is_not(None),
                )
                .group_by(_ServerModel.credential_account_id)
            )
        ).all()
        for account_id, count in server_rows:
            servers[str(account_id)] = int(count)

        order_rows = (
            await session.execute(
                select(
                    _ProviderOrderModel.credential_account_id,
                    func.count(_ProviderOrderModel.id),
                )
                .where(
                    _ProviderOrderModel.provider_key == provider_key,
                    _ProviderOrderModel.credential_account_id.is_not(None),
                )
                .group_by(_ProviderOrderModel.credential_account_id)
            )
        ).all()
        for account_id, count in order_rows:
            orders[str(account_id)] = int(count)

    report = [
        CredentialAccountDependents(
            provider_key=provider_key,
            credential_account_id=account_id,
            servers=servers.get(account_id, 0),
            provider_orders=orders.get(account_id, 0),
        )
        for account_id in {*servers, *orders}
        if account_id not in configured
    ]
    report.sort(key=lambda entry: (-entry.total, entry.credential_account_id))
    return report


async def missing_credential_account_report(
    session_factory: Callable[[], AbstractAsyncContextManager[Any]],
    *,
    provider_key: str,
    configured_account_ids: Iterable[str],
) -> tuple[list[CredentialAccountDependents], str | None]:
    """Best-effort wrapper returning the report plus a SAFE error class name.

    Operator diagnostics must never raise: a missing/unreachable database says
    "could not check", never "nothing depends on it". The returned string is an
    exception CLASS name, so no connection string or credential can leak.
    """
    try:
        return (
            await scan_credential_account_dependents(
                session_factory,
                provider_key=provider_key,
                configured_account_ids=configured_account_ids,
            ),
            None,
        )
    except Exception as exc:  # diagnostics are deliberately non-fatal
        return [], type(exc).__name__
