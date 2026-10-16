"""Payment admin authorization, redaction and shared authoritative settlement."""

from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from cloud_platform.modules.admin_console.payments import AdminPaymentService
from cloud_platform.modules.payments.domain import PaymentSession, PaymentSessionStatus
from cloud_platform.modules.payments.repository import SqlAlchemyPaymentSessionRepository
from cloud_platform.modules.users.domain import PermissionDeniedError, Role, User, UserStatus
from cloud_platform.providers.base import PaymentIntent, PaymentStatus

ADMIN_TID = 77110022


def admin():
    return User(
        id=uuid4(),
        username="operator",
        email="operator@example.test",
        role=Role.ADMIN,
        telegram_user_id=ADMIN_TID,
    )


def invoice():
    return PaymentSession(
        id=uuid4(),
        user_id=uuid4(),
        gateway_key="nowpayments",
        gateway_payment_id="123456",
        amount_minor=1050,
        currency="USD",
        idempotency_key="secret-local-attempt",
        credit_amount_minor=900,
        credit_currency="EUR",
        payment_details={"token": "credential-secret"},
        redirect_url="https://provider.test/private-token",
    )


def service(actor=None):
    actor = actor or admin()
    session = invoice()
    users = SimpleNamespace(get=AsyncMock(return_value=actor))
    payments = SimpleNamespace(
        get=AsyncMock(return_value=session), search_admin=AsyncMock(return_value=([session], False))
    )
    authoritative = SimpleNamespace(
        reconcile_session=AsyncMock(return_value=session), inspect_session=AsyncMock()
    )
    return (
        actor,
        session,
        users,
        payments,
        authoritative,
        AdminPaymentService(users, payments, authoritative, configured_ids=(ADMIN_TID,)),
    )


@pytest.mark.parametrize("operation", ["search", "profile", "inquire"])
@pytest.mark.parametrize("changed", ["blocked", "role", "identity", "deleted"])
async def test_every_method_requires_fresh_configured_active_persisted_admin(operation, changed):
    actor, session, users, payments, authoritative, facade = service()
    fresh = {
        "blocked": replace(actor, status=UserStatus.BANNED),
        "role": replace(actor, role=Role.USER),
        "identity": replace(actor, telegram_user_id=ADMIN_TID + 1),
        "deleted": None,
    }[changed]
    users.get.return_value = fresh
    with pytest.raises(PermissionDeniedError):
        await getattr(facade, operation)(actor, *(() if operation == "search" else (session.id,)))
    payments.get.assert_not_awaited()
    payments.search_admin.assert_not_awaited()
    authoritative.reconcile_session.assert_not_awaited()


async def test_search_and_profile_return_only_redacted_financial_facts():
    actor, session, users, payments, _, facade = service()
    page = await facade.search(actor, "123456", status="pending", limit=100)
    payments.search_admin.assert_awaited_once_with(
        "123456", status="pending", gateway_key=None, offset=0, limit=50
    )
    profile = await facade.profile(actor, session.id)
    assert users.get.await_count == 2
    assert page.items == (profile,)
    text = repr(asdict(profile))
    assert "credential-secret" not in text and "private-token" not in text
    assert "secret-local-attempt" not in text and "payment_details" not in text
    assert profile.amount_minor == 1050 and profile.credit_amount_minor == 900


@pytest.mark.parametrize(
    "options",
    [
        {"offset": -1},
        {"offset": True},
        {"offset": 10001},
        {"limit": 0},
        {"limit": True},
        {"query": "x" * 101},
        {"status": "invented"},
        {"gateway_key": "bad/key"},
    ],
)
async def test_search_bounds_and_filters_fail_before_database_read(options):
    actor, _, _, payments, _, facade = service()
    with pytest.raises(ValueError):
        await facade.search(actor, **options)
    payments.search_admin.assert_not_awaited()


async def test_privileged_inquiry_uses_actual_invoice_owner_without_impersonation():
    from test_payment_nowpayments_settlement import system

    payments, wallet, gateway, _, _, authoritative, _ = system()
    actor = admin()
    assert actor.id != payments.session.user_id
    facade = AdminPaymentService(
        SimpleNamespace(get=AsyncMock(return_value=actor)),
        payments,
        authoritative,
        configured_ids=(ADMIN_TID,),
    )
    try:
        result = await facade.inquire(actor, payments.session.id)
        assert result.payment.status is PaymentSessionStatus.SUCCEEDED
        assert result.payment.user_id != actor.id
        assert wallet.balance == 907
        await facade.inquire(actor, payments.session.id)
        assert wallet.balance == 907 and len(wallet.entries) == 1
    finally:
        await gateway.close()


async def test_manual_review_is_verified_read_only_and_cannot_be_marked_paid():
    actor, session, _, payments, authoritative, facade = service()
    held = replace(session, status=PaymentSessionStatus.MANUAL_REVIEW)
    payments.get.return_value = held
    authoritative.inspect_session.return_value = PaymentIntent(
        gateway_payment_id=held.gateway_payment_id,
        status=PaymentStatus.SUCCEEDED,
        amount_minor=held.amount_minor,
        currency=held.currency,
    )
    result = await facade.inquire(actor, held.id)
    assert result.settlement_held and result.observed_status == "succeeded"
    assert result.payment.status is PaymentSessionStatus.MANUAL_REVIEW
    authoritative.inspect_session.assert_awaited_once_with(held)
    authoritative.reconcile_session.assert_not_awaited()
    assert not hasattr(facade, "mark_paid") and not hasattr(facade, "credit")


async def test_real_gateway_full_payment_inspection_does_not_release_manual_review():
    from test_payment_nowpayments_settlement import system

    payments, wallet, gateway, _, _, authoritative, _ = system()
    payments.session = replace(payments.session, status=PaymentSessionStatus.MANUAL_REVIEW)
    actor = admin()
    facade = AdminPaymentService(
        SimpleNamespace(get=AsyncMock(return_value=actor)),
        payments,
        authoritative,
        configured_ids=(ADMIN_TID,),
    )
    try:
        result = await facade.inquire(actor, payments.session.id)
        assert result.observed_status == "succeeded" and result.settlement_held
        assert payments.session.status is PaymentSessionStatus.MANUAL_REVIEW
        assert wallet.balance == 7 and not wallet.entries
    finally:
        await gateway.close()


async def test_repository_query_is_bounded_stable_exact_and_filtered():
    statements = []

    class DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, stmt):
            statements.append(stmt)
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    repo = SqlAlchemyPaymentSessionRepository(DB)
    assert await repo.search_admin(
        "123456", status="manual_review", gateway_key="nowpayments", offset=10, limit=20
    ) == ([], False)
    compiled = statements[0].compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )
    sql = str(compiled)
    assert "LIKE" not in sql
    assert "gateway_payment_id = '123456'" in sql
    assert "provider_payment_id = '123456'" in sql
    assert "status = 'manual_review'" in sql
    assert "gateway_key = 'nowpayments'" in sql
    assert "created_at DESC, payment_sessions.id DESC" in sql
    assert "LIMIT 21 OFFSET 10" in sql
