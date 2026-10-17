"""Read-only provider verification and local registration of the shared operator test VPS."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from cloud_platform.core.config import get_settings
from cloud_platform.db.base import AuditEvent, Provider, ProviderAccount, Server, User
from cloud_platform.db.timestamps import to_db_utc, utc_now
from cloud_platform.modules.catalog.repository import provider_key_to_uuid
from cloud_platform.modules.servers.test_identity import (
    TEST_IPV4,
    TEST_PROVIDER_ID,
    TEST_PROVIDER_KEY,
    is_test_server,
)
from cloud_platform.modules.users.superadmin_repository import authorize_superadmin_transaction
from cloud_platform.modules.users.superadmins import is_superadmin
from cloud_platform.providers.routing import provider_for


async def find_test_server(container: Any) -> Any | None:
    async with container.session_factory() as session:
        ids = (
            (
                await session.execute(
                    select(Server.id)
                    .join(Provider)
                    .where(
                        Provider.name == TEST_PROVIDER_KEY,
                        Server.provider_server_id == TEST_PROVIDER_ID,
                    )
                )
            )
            .scalars()
            .all()
        )
    if len(ids) > 1:
        raise ValueError("duplicate provider resource requires operator reconciliation")
    server = await container.server_repository().get(ids[0]) if ids else None
    if server is not None and not is_test_server(server):
        raise ValueError("existing test resource has a different owner/funding/identity contract")
    return server


async def register_test_server(container: Any, *, actor: Any = None) -> Any:
    """Never provision, reinstall or change power. Verify exact id AND IP before DB import."""
    settings = get_settings()
    async with container.session_factory() as session:
        admins = (
            (
                await session.execute(
                    select(User.id)
                    .where(
                        User.telegram_user_id.in_(settings.telegram_superadmin_user_ids),
                        User.role == "admin",
                        User.status == "active",
                    )
                    .order_by(User.created_at)
                )
            )
            .scalars()
            .all()
        )
    if not admins:
        raise ValueError("no configured active superadmin is available")
    owner = await container.user_repository().get(admins[0])
    if not is_superadmin(owner, settings.telegram_superadmin_user_ids):
        raise ValueError("configured superadmin required")
    actor = actor or owner
    if not is_superadmin(actor, settings.telegram_superadmin_user_ids):
        raise ValueError("configured superadmin actor required")
    matches = []
    for credential_account in settings.leaseweb_managed_accounts:
        provider = provider_for(
            container.provider_registry, TEST_PROVIDER_KEY, credential_account.id
        )
        management = getattr(provider, "vps_management", provider)
        info = await management.get_vps_info(TEST_PROVIDER_ID)
        if info is None:
            continue
        if str(info.id) != TEST_PROVIDER_ID:
            raise ValueError("provider returned a different test-resource identity")
        addresses = await management.list_vps_ips(TEST_PROVIDER_ID)
        if any(str(getattr(item, "ip", "")) == TEST_IPV4 for item in addresses):
            matches.append((credential_account.id, info))
    if len(matches) != 1:
        raise ValueError("test server must be proven in exactly one configured credential account")
    credential_id, info = matches[0]
    existing = await find_test_server(container)
    if existing is not None:
        if existing.credential_account_id != credential_id:
            raise ValueError("existing test resource is pinned to a different credential account")
        return existing
    state = {"RUNNING": "running", "ACTIVE": "running", "STOPPED": "stopped", "OFF": "stopped"}.get(
        info.state.upper()
    )
    if state is None:
        raise ValueError("test server state is not stable; retry a read-only import later")
    async with container.session_factory() as session, session.begin():
        await authorize_superadmin_transaction(
            session, owner.id, settings.telegram_superadmin_user_ids
        )
        if actor.id != owner.id:
            await authorize_superadmin_transaction(
                session, actor.id, settings.telegram_superadmin_user_ids
            )
        # Lock the provider namespace even on an otherwise empty installation.
        # The name upsert also fences concurrent catalog discovery.
        provider_query = (
            select(Provider).where(Provider.name == TEST_PROVIDER_KEY).with_for_update()
        )
        provider = (await session.execute(provider_query)).scalar_one_or_none()
        if provider is None:
            await session.execute(
                pg_insert(Provider)
                .values(
                    id=provider_key_to_uuid(TEST_PROVIDER_KEY),
                    name=TEST_PROVIDER_KEY,
                    region="global",
                )
                .on_conflict_do_nothing(index_elements=[Provider.name])
            )
            provider = (await session.execute(provider_query)).scalar_one()
        rows = (
            (
                await session.execute(
                    select(Server).where(
                        Server.provider_id == provider.id,
                        Server.provider_server_id == TEST_PROVIDER_ID,
                    )
                )
            )
            .scalars()
            .all()
        )
        if rows:
            server_id = rows[0].id
        else:
            account = (
                await session.execute(
                    select(ProviderAccount)
                    .where(
                        ProviderAccount.provider_id == provider.id,
                        ProviderAccount.user_id == owner.id,
                    )
                    .order_by(ProviderAccount.created_at)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if account is None:
                account = ProviderAccount(
                    id=uuid4(), provider_id=provider.id, user_id=owner.id, status="active"
                )
                session.add(account)
                await session.flush()
            server_id = uuid4()
            session.add(
                Server(
                    id=server_id,
                    user_id=owner.id,
                    provider_id=provider.id,
                    provider_account_id=account.id,
                    credential_account_id=credential_id,
                    provider_server_id=TEST_PROVIDER_ID,
                    ipv4=TEST_IPV4,
                    state=state,
                    os=info.image_name,
                    billing_model="prepaid_monthly_fixed",
                    currency="IRT",
                    price_per_quantum=0,
                    funding_source="operator",
                    funding_operator_id=owner.id,
                    idempotency_key="operator-shared-test:leaseweb:12717238",
                )
            )
            session.add(
                AuditEvent(
                    id=uuid4(),
                    actor_type="admin",
                    actor_id=actor.id,
                    action="operator.test_server.registered",
                    resource_type="server",
                    resource_id=str(server_id),
                    reason="Read-only import of verified existing operator test VPS",
                    event_metadata={
                        "owner_id": str(owner.id),
                        "credential_account_id": credential_id,
                    },
                    occurred_at=to_db_utc(utc_now()),
                )
            )
    result = await find_test_server(container)
    if result is None:
        raise ValueError("test resource registration could not be verified")
    return result
