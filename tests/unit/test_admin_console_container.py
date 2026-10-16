"""Real DI construction without configuration secrets, networks or database I/O."""

from unittest.mock import MagicMock
from uuid import uuid4

from cloud_platform.bot.admin_ui import AdminBotUi
from cloud_platform.core.config import Settings
from cloud_platform.core.container import Container
from cloud_platform.core.session_store import InMemoryBotSessionStore
from cloud_platform.modules.users.domain import Role, User
from cloud_platform.providers.registry import ProviderRegistry


def test_complete_management_composition_has_real_sections_and_shared_funding(monkeypatch):
    settings = Settings.model_construct(
        telegram_superadmin_user_ids=(10101,),
        fx_enabled=False,
        fx_domestic_enabled=False,
        fx_allow_usdt_proxy_for_settlement=False,
    )
    monkeypatch.setattr("cloud_platform.core.container.get_settings", lambda: settings)
    container = Container(
        session_factory=MagicMock(),
        provider_registry=ProviderRegistry(),
        provider_allocator=MagicMock(),
        hetzner_syncer=None,
        bot_session_store=InMemoryBotSessionStore(),
    )
    ui = AdminBotUi(
        "fixture-signing-key",
        gateways=container.gateway_management_service(()),
        users=container.user_repository(),
        wallets=container.wallet_repository(),
        wallet_admin=container.wallet_admin_service(),
        store=container.session_store(),
        configured_ids=(10101,),
        user_admin=container.admin_user_service(),
        wallet_adjustments=container.admin_wallet_adjustment_service(),
        services=container.admin_server_support_service(),
        sales=container.admin_sales_service(),
        health=container.admin_health_service(),
        messaging=container.admin_messaging_service(),
        payments=container.admin_payment_service({}),
        reports=container.admin_reports_service(),
    )
    actor = User(
        id=uuid4(),
        username="admin",
        email="admin@example.test",
        telegram_user_id=10101,
        role=Role.ADMIN,
    )
    screen = ui.menu(actor)
    assert set(ui._extra_sections) == {
        "payments",
        "reports",
        "services",
        "sales",
        "health",
        "messages",
    }
    assert ui._users_ui is not None and ui._wallet_ui is not None
    assert screen.text
    assert container.prepaid_hourly_billing_service()._funding is not None
    assert container.order_settlement_service()._funding is not None
