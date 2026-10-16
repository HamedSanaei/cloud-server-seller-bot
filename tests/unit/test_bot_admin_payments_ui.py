"""Owner-bound invoice search, compact callbacks and held provider inquiry."""

from dataclasses import replace
from uuid import uuid4

import pytest
from test_admin_console_payments import service

from cloud_platform.bot.admin_payments_ui import AdminPaymentsUi
from cloud_platform.core.i18n import Locale, Translator
from cloud_platform.core.session_store import InMemoryBotSessionStore
from cloud_platform.modules.navigation.domain import Callback, decode_callback, encode_offer_ref
from cloud_platform.modules.payments.domain import PaymentSessionStatus
from cloud_platform.providers.base import PaymentIntent, PaymentStatus

KEY = "admin-payment-fixture-signing-key"


def buttons(screen):
    return [button for row in screen.keyboard.inline_keyboard for button in row]


@pytest.mark.parametrize("locale", [Locale.FA, Locale.EN])
async def test_lists_pending_review_and_profile_with_compact_secret_free_buttons(locale):
    actor, session, _, payments, _, facade = service()
    ui = AdminPaymentsUi(KEY, facade, InMemoryBotSessionStore(), Translator(locale))
    for screen, expected in [
        ("payments", None),
        ("p_pending", "pending"),
        ("p_review", "manual_review"),
    ]:
        result = await ui.handle(Callback("admin", screen), actor)
        assert result is not None
        assert payments.search_admin.await_args.kwargs["status"] == expected
        for button in buttons(result):
            assert len(button.callback_data.encode()) <= 64
            decode_callback(button.callback_data, KEY)
    result = await ui.handle(Callback("admin", "p_profile", (encode_offer_ref(session.id),)), actor)
    assert str(session.id) in result.text and str(session.user_id) in result.text
    assert "credential-secret" not in result.text
    assert "secret-local-attempt" not in result.text and "private-token" not in result.text
    assert all(
        decode_callback(button.callback_data, KEY).screen not in {"mark_paid", "credit"}
        for button in buttons(result)
    )


async def test_search_prompt_is_owner_scoped_bounded_and_navigation_cancels_only_prompt():
    actor, _, _, payments, _, facade = service()
    store = InMemoryBotSessionStore()
    ui = AdminPaymentsUi(KEY, facade, store, Translator())
    assert await ui.handle_text("query", actor) is None
    await ui.handle(Callback("admin", "p_search"), actor)
    assert await store.get("admin-payment-input", str(actor.id))
    await store.put("admin-credit-confirm", "other-owned-record", {"amount": 99}, ttl_seconds=900)
    assert await ui.handle_text("x" * 101, actor) is not None
    assert await store.get("admin-payment-input", str(actor.id))
    await ui.handle_text("123456", actor)
    assert payments.search_admin.await_args.args == ("123456",)
    assert await store.get("admin-payment-input", str(actor.id)) is None
    await ui.handle(Callback("admin", "p_search"), actor)
    await ui.handle(Callback("admin", "p_pending"), actor)
    assert await store.get("admin-payment-input", str(actor.id)) is None
    assert await store.get("admin-credit-confirm", "other-owned-record") == {"amount": 99}


async def test_search_pagination_cannot_reuse_another_owner_or_expired_record():
    actor, _, _, payments, _, facade = service()
    payments.search_admin.return_value = ([], True)
    store = InMemoryBotSessionStore()
    ui = AdminPaymentsUi(KEY, facade, store, Translator(Locale.EN))
    result = await ui.handle(Callback("admin", "payments"), actor)
    next_button = next(
        button
        for button in buttons(result)
        if decode_callback(button.callback_data, KEY).screen == "p_page"
    )
    callback = decode_callback(next_button.callback_data, KEY)
    record = await store.get("admin-payment-search", f"{actor.id}:{callback.args[0]}")
    await store.delete("admin-payment-search", f"{actor.id}:{callback.args[0]}")
    await store.put(
        "admin-payment-search", f"{uuid4()}:{callback.args[0]}", record, ttl_seconds=900
    )
    expired = await ui.handle(callback, actor)
    assert "expired" in expired.text.lower()


async def test_manual_review_inquiry_renders_provider_result_without_releasing_hold():
    actor, session, _, payments, authoritative, facade = service()
    payments.get.return_value = replace(session, status=PaymentSessionStatus.MANUAL_REVIEW)
    authoritative.inspect_session.return_value = PaymentIntent(
        gateway_payment_id=session.gateway_payment_id,
        status=PaymentStatus.SUCCEEDED,
        amount_minor=session.amount_minor,
        currency=session.currency,
    )
    ui = AdminPaymentsUi(KEY, facade, InMemoryBotSessionStore(), Translator(Locale.EN))
    result = await ui.handle(Callback("admin", "p_inquire", (encode_offer_ref(session.id),)), actor)
    assert "succeeded" in result.text and "manual_review" in result.text
    assert "remains held" in result.text
    authoritative.reconcile_session.assert_not_awaited()


async def test_unrelated_callback_does_not_claim_another_flow():
    actor, _, _, payments, _, facade = service()
    ui = AdminPaymentsUi(KEY, facade, InMemoryBotSessionStore(), Translator())
    assert await ui.handle(Callback("customer", "payments"), actor) is None
    assert await ui.handle(Callback("admin", "users"), actor) is None
    payments.search_admin.assert_not_awaited()
