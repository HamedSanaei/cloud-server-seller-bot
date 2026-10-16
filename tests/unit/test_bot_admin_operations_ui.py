"""Executable management actions retain actor-bound reason/confirmation steps."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cloud_platform.bot.admin_operations_ui import (
    AdminHealthUi,
    AdminMessagingUi,
    AdminSalesUi,
    AdminSupportUi,
)
from cloud_platform.core.i18n import Locale, Translator
from cloud_platform.core.session_store import InMemoryBotSessionStore
from cloud_platform.modules.navigation.domain import Callback, decode_callback, encode_offer_ref

KEY = "management-operations-test-key"


def actions(screen):
    result = []
    for row in screen.keyboard.inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode()) <= 64
            result.append(decode_callback(button.callback_data, KEY))
    assert "admin." not in screen.text
    return result


@pytest.mark.parametrize("locale", [Locale.FA, Locale.EN])
async def test_price_requires_integer_reason_and_explicit_confirmation(locale):
    actor, identity = SimpleNamespace(id=uuid4()), uuid4()
    offer = SimpleNamespace(
        id=identity,
        name="Fixture",
        provider_key="fixture",
        location_id="fixture",
        selling_price_minor=200,
        selling_currency="USD",
        enabled=True,
    )
    service = SimpleNamespace(
        detail=AsyncMock(return_value=offer),
        prepare=AsyncMock(return_value="confirmed"),
        apply=AsyncMock(return_value="set_price"),
        clear=AsyncMock(),
    )
    ui = AdminSalesUi(KEY, service, InMemoryBotSessionStore(), Translator(locale))
    detail = await ui.handle(Callback("admin", "c_offer", (encode_offer_ref(identity),)), actor)
    price = next(cb for cb in actions(detail) if cb.args[-1:] == ("set_price",))
    actions(await ui.handle(price, actor))
    for invalid in ("0", "-1", "12.5", str(2**63)):
        actions(await ui.handle_text(invalid, actor))
    service.prepare.assert_not_awaited()
    actions(await ui.handle_text("350", actor))
    preview = await ui.handle_text("Reviewed retail price", actor)
    assert "350 USD" in preview.text
    service.apply.assert_not_awaited()
    service.prepare.assert_awaited_once_with(
        actor,
        identity,
        "set_price",
        "Reviewed retail price",
        price_minor=350,
    )
    actions(await ui.handle(next(cb for cb in actions(preview) if cb.screen == "c_apply"), actor))
    service.apply.assert_awaited_once_with(actor, "confirmed")


async def test_message_freezes_targets_body_and_reason_before_enqueue_and_cancel():
    actor = SimpleNamespace(id=uuid4())
    service = SimpleNamespace(
        prepare=AsyncMock(return_value="approved"),
        apply=AsyncMock(return_value=uuid4()),
        clear=AsyncMock(),
    )
    ui = AdminMessagingUi(KEY, service, InMemoryBotSessionStore(), Translator(Locale.FA))
    actions(await ui.handle(Callback("admin", "messages"), actor))
    actions(await ui.handle_text("210 210", actor))
    service.prepare.assert_not_awaited()
    await ui.handle_text("210,211", actor)
    await ui.handle_text("Support notification", actor)
    preview = await ui.handle_text("Maintenance notice", actor)
    service.prepare.assert_awaited_once_with(
        actor,
        (210, 211),
        "Support notification",
        "Maintenance notice",
    )
    service.apply.assert_not_awaited()
    await ui.handle(next(cb for cb in actions(preview) if cb.screen == "m_apply"), actor)
    service.apply.assert_awaited_once_with(actor, "approved")
    await ui.handle(Callback("admin", "messages"), actor)
    await ui.clear_prompt(actor)
    assert await ui.handle_text("210", actor) is None


async def test_support_real_action_requires_reason_and_health_redacts_provider_errors():
    actor, identity = SimpleNamespace(id=uuid4()), uuid4()
    service = SimpleNamespace(
        detail=AsyncMock(
            return_value=SimpleNamespace(
                server_id=identity,
                owner_id=uuid4(),
                provider_key="fixture",
                state="running",
                ipv4=None,
                actions=("power_off",),
            )
        ),
        prepare=AsyncMock(return_value="approved"),
        clear=AsyncMock(),
    )
    ui = AdminSupportUi(KEY, service, InMemoryBotSessionStore(), Translator(Locale.FA))
    await ui.handle(Callback("admin", "services"), actor)
    detail = await ui.handle_text(str(identity), actor)
    await ui.handle(next(cb for cb in actions(detail) if cb.screen == "s_reason"), actor)
    preview = await ui.handle_text("Customer request", actor)
    assert any(cb.screen == "s_apply" for cb in actions(preview))
    service.prepare.assert_awaited_once_with(actor, identity, "power_off", "Customer request")
    health = AdminHealthUi(
        KEY,
        SimpleNamespace(
            snapshot=AsyncMock(
                return_value={
                    "database": "ready",
                    "dependencies": {"redis": "ready", "api": "ready"},
                    "servers": {"running": 2},
                    "operations": {"failed": 1},
                    "message_deliveries": {"ambiguous": 1},
                }
            )
        ),
        InMemoryBotSessionStore(),
        Translator(Locale.FA),
    )
    status = await health.handle(Callback("admin", "health"), actor)
    assert "redis: ready" in status.text and "server/running: 2" in status.text
    assert any(cb.screen == "h_failed" for cb in actions(status))
