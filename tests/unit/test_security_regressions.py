"""Security regression tests (M10-009).

Acceptance: ownership, callback replay, secret redaction covered.

This module is the cross-cutting SECURITY sweep: it re-asserts, in one
place, the security invariants that every feature module promises
individually, so a future refactor that breaks any of them fails the same
test file:

1. OWNERSHIP - a user can never read or act on another user's resource.
   Missing and foreign resources are INDISTINGUISHABLE (no existence
   oracle): the same result, the same error, and no per-resource audit
   probing someone else's id.
2. CALLBACK REPLAY - a signed callback's effect happens at most once: the
   delete saga replay keeps the same operation key (no second provider
   delete, no second final charge), the create command replay reserves no
   second hold, and a replayed payment webhook cannot deposit twice; a
   TAMPERED callback is rejected, not re-interpreted.
3. SECRET REDACTION - provider tokens, passwords, Bearer headers and DSN
   credentials never reach the log output or the envelope repr; a tampered
   secret envelope is detected on decrypt; a forged or inflated gateway
   webhook signature fails verification.

Each test names the invariant it guards in its docstring; when one fails,
the security contract it protects has regressed.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from cloud_platform.core.logging import _REDACTED_PLACEHOLDER, _redact_value
from cloud_platform.core.webhooks import sign_gateway_payload, verify_gateway_signature
from cloud_platform.modules.billing.service import FinalChargeResult
from cloud_platform.modules.catalog.domain import OfferRef, OfferState
from cloud_platform.modules.compute.domain import (
    CloudServer,
    ServerLifecycleState,
)
from cloud_platform.modules.compute.service import (
    CreateServerCommandError,
    CreateServerService,
    MyServersService,
)
from cloud_platform.modules.navigation.domain import CallbackError, decode_callback
from cloud_platform.modules.operations.domain import (
    Operation,
    OperationStatus,
)
from cloud_platform.modules.operations.service import (
    DeleteCommandService,
    DeleteConfirmationService,
    DeleteOperationExecutor,
    NotServerOwnerError,
    PowerCommandService,
    PowerControlsService,
    delete_operation_key,
)
from cloud_platform.modules.pricing.domain import (
    MarginRule,
    OfferCost,
    SellingPrice,
    ServerPriceSnapshot,
)
from cloud_platform.modules.provider_accounts.domain import ProviderAccount
from cloud_platform.modules.users.domain import Role, User, UserStatus
from cloud_platform.modules.wallet.domain import (
    Hold,
    LedgerEntry,
    LedgerEntryType,
    Wallet,
)
from cloud_platform.providers.base import ProviderServer
from cloud_platform.providers.waiter import WaitOutcome, WaitResult

SIGNING_KEY = "security-test-signing-key"
NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
OWNER_ID = uuid4()
ATTACKER_ID = uuid4()
SERVER_ID = uuid4()
WALLET_ID = uuid4()
OTHER_WALLET_ID = uuid4()
ACCOUNT_ID = uuid4()
REF = OfferRef(provider_key="hetzner", plan_id="cx22", location_id="fsn1")


def _owner() -> User:
    return User(
        id=OWNER_ID,
        username="owner",
        email="o@example.com",
        role=Role.USER,
        status=UserStatus.ACTIVE,
    )


def _attacker() -> User:
    return User(
        id=ATTACKER_ID,
        username="attacker",
        email="a@example.com",
        role=Role.USER,
        status=UserStatus.ACTIVE,
    )


def _server(state: ServerLifecycleState = ServerLifecycleState.RUNNING) -> CloudServer:
    return CloudServer(
        id=SERVER_ID,
        user_id=OWNER_ID,
        provider_key="hetzner",
        provider_account_id=ACCOUNT_ID,
        state=state,
        idempotency_key="order-1",
        created_at=NOW,
        provider_server_id="prov-1",
        quantum_seconds=3600,
    )


def _price() -> SellingPrice:
    return SellingPrice(
        offer=OfferCost("hetzner", "cx22", "fsn1", 100, "EUR"),
        selling_minor=107,
        rule=MarginRule("*", "*", "*", Decimal("1.07")),
        book_name="retail-eur",
        version=1,
        priced_at=NOW,
    )


def _snapshot() -> ServerPriceSnapshot:
    p = _price()
    return ServerPriceSnapshot(
        server_id=SERVER_ID,
        offer=p.offer,
        selling_minor=p.selling_minor,
        book_name=p.book_name,
        book_version=p.version,
        rule=p.rule,
        priced_at=p.priced_at,
        id=uuid4(),
    )


# ==========================================================================
# 1. OWNERSHIP
# ==========================================================================


class _Fakes:
    """Minimal fakes shared by the ownership tests (mirrors each module's own)."""

    def __init__(self, servers: list[CloudServer]) -> None:
        self._servers = {s.id: s for s in servers}
        self.audit = AsyncMock()
        self.audit.append = AsyncMock(side_effect=lambda e: e)

    async def get(self, server_id: UUID) -> CloudServer | None:
        return self._servers.get(server_id)

    async def save(self, server: CloudServer) -> CloudServer:
        return server


class TestDeleteConfirmationOwnership:
    """Delete confirmation: a foreign server is indistinguishable from a
    missing one (uniform None), with no audit probing someone else's id."""

    def _service(self, servers: list[CloudServer]) -> tuple[DeleteConfirmationService, _Fakes]:
        fakes = _Fakes(servers)
        service = DeleteConfirmationService(server_repo=fakes, signing_key=SIGNING_KEY)  # type: ignore[arg-type]
        return service, fakes

    async def test_owner_sees_the_confirmation_screen(self) -> None:
        service, _ = self._service([_server()])
        view = await service.screen(OWNER_ID, SERVER_ID)
        assert view is not None
        assert view.server_id == SERVER_ID
        assert view.stage == 1

    async def test_foreign_server_is_indistinguishable_from_missing(self) -> None:
        service, fakes = self._service([_server()])
        foreign = await service.screen(ATTACKER_ID, SERVER_ID)
        missing = await service.screen(ATTACKER_ID, uuid4())
        assert foreign is None
        assert missing is None
        # no audit trail was written probing someone else's server
        assert fakes.audit.append.call_count == 0

    async def test_state_outside_precondition_is_uniform_none(self) -> None:
        # a server already being deleted yields the same uniform None as a
        # foreign/missing one (no state-leak oracle)
        service, _ = self._service([_server(ServerLifecycleState.DELETING)])
        assert await service.screen(OWNER_ID, SERVER_ID) is None


class TestPowerControlsOwnership:
    """Power controls: detail of a foreign server is None (no oracle),
    while the owner gets the actions."""

    def _service(self, provider_caps: frozenset) -> PowerControlsService:
        class _ServerRepo:
            async def get(self, server_id: UUID) -> CloudServer | None:
                return _server() if server_id == SERVER_ID else None

        class _Registry:
            def get(self, provider_key: str) -> object:
                class _P:
                    capabilities = provider_caps

                return _P()

        return PowerControlsService(  # type: ignore[call-arg]
            server_repo=_ServerRepo(),
            provider_registry=_Registry(),  # type: ignore[arg-type]
            signing_key=SIGNING_KEY,
        )

    async def test_foreign_server_detail_is_none(self) -> None:
        service = self._service(frozenset())
        view = await service.detail(ATTACKER_ID, SERVER_ID)
        missing = await service.detail(ATTACKER_ID, uuid4())
        assert view is None
        assert missing is None

    async def test_owner_gets_actions(self) -> None:
        from cloud_platform.providers.base import Capability

        service = self._service(frozenset({Capability.POWER}))
        view = await service.detail(OWNER_ID, SERVER_ID)
        assert view is not None
        assert [a.action for a in view.actions]  # the gated actions are exposed


class TestPowerCommandOwnership:
    """Power commands: acting on a foreign server raises the SAME
    NotServerOwnerError as a missing one - no existence oracle, no
    provider call for someone else's server."""

    def _service(self, server: CloudServer | None) -> PowerCommandService:
        class _ServerRepo:
            async def get(self, server_id: UUID) -> CloudServer | None:
                return server if server is not None and server.id == server_id else None

        class _OpRepo:
            async def get_by_key(self, key: str) -> Operation | None:
                return None

        return PowerCommandService(  # type: ignore[call-arg]
            server_repo=_ServerRepo(),
            operation_repo=_OpRepo(),  # type: ignore[arg-type]
            provider_registry=MagicMock(),  # type: ignore[arg-type]
            audit_repo=AsyncMock(),
        )

    async def test_foreign_and_missing_raise_the_same_error(self) -> None:
        with pytest.raises(NotServerOwnerError) as foreign_exc:
            await self._service(_server()).power_off(ATTACKER_ID, SERVER_ID, "power-1")
        with pytest.raises(NotServerOwnerError) as missing_exc:
            await self._service(None).power_off(ATTACKER_ID, uuid4(), "power-2")
        assert type(foreign_exc.value) is type(missing_exc.value)

    async def test_foreign_server_raises_not_owner(self) -> None:
        with pytest.raises(NotServerOwnerError):
            await self._service(_server()).power_off(ATTACKER_ID, SERVER_ID, "power-1")

    async def test_missing_server_raises_not_owner(self) -> None:
        with pytest.raises(NotServerOwnerError):
            await self._service(None).power_off(ATTACKER_ID, uuid4(), "power-2")


class TestMyServersOwnership:
    """My servers list + detail: only the requesting user's rows are ever
    visible; a foreign detail id is None, same as a missing id."""

    async def test_paged_list_never_leaks_foreign_rows(self) -> None:
        foreign = CloudServer(
            id=uuid4(),
            user_id=ATTACKER_ID,
            provider_key="hetzner",
            provider_account_id=ACCOUNT_ID,
            state=ServerLifecycleState.RUNNING,
            idempotency_key="other",
            created_at=NOW,
        )

        class _ServerRepo:
            async def list_by_user_paged(
                self, user_id: UUID, *, offset: int, limit: int
            ) -> tuple[list[CloudServer], int]:
                rows = [s for s in (foreign,) if s.user_id == user_id]
                return rows[offset : offset + limit], len(rows)

            async def get(self, server_id: UUID) -> CloudServer | None:
                return foreign if foreign.id == server_id else None

        service = MyServersService(_ServerRepo())  # type: ignore[arg-type]
        page = await service.list_servers(OWNER_ID)
        assert page.total == 0  # the attacker's server never leaks into the owner's list
        detail = await service.get_server(OWNER_ID, foreign.id)
        assert detail is None  # and the detail is a uniform None


class TestLedgerOwnership:
    """Wallet ledger: history for a wallet the user does not own yields the
    same empty page as a wallet that does not exist (no balance oracle)."""

    async def test_foreign_wallet_history_is_empty(self) -> None:
        from cloud_platform.core.money import Money
        from cloud_platform.modules.wallet.service import WalletHistoryService

        class _WalletRepo:
            async def get(self, user_id: UUID) -> Wallet | None:
                if user_id == ATTACKER_ID:
                    return Wallet(
                        user_id=ATTACKER_ID, id=OTHER_WALLET_ID, balance=100, currency="EUR"
                    )
                return Wallet(user_id=OWNER_ID, id=WALLET_ID, balance=100, currency="EUR")

        class _LedgerRepo:
            async def list_entries_paged(
                self, wallet_id: UUID, *, offset: int, limit: int
            ) -> tuple[list[LedgerEntry], int]:
                # only the owner's wallet has entries
                if wallet_id == WALLET_ID:
                    return (
                        [
                            LedgerEntry(
                                id=uuid4(),
                                wallet_id=wallet_id,
                                entry_type=LedgerEntryType.DEPOSIT,
                                amount=Money(500, "EUR"),
                                reference_type="payment",
                                reference_id="pay-1",
                                idempotency_key="k1",
                            )
                        ],
                        1,
                    )
                return [], 0

        service = WalletHistoryService(_WalletRepo(), _LedgerRepo())  # type: ignore[arg-type]
        own = await service.history(OWNER_ID)
        foreign = await service.history(ATTACKER_ID)
        assert own.total == 1
        assert foreign.total == 0  # the owner's entries are never visible to the attacker


# ==========================================================================
# 2. CALLBACK REPLAY
# ==========================================================================


class TestDeleteSagaReplay:
    """The delete execute-callback carries the idempotency key: a replayed
    callback re-runs the SAME operation key, so the provider delete and the
    final charge happen exactly once."""

    async def test_execute_callback_replay_keeps_the_same_key(self) -> None:
        service = DeleteConfirmationService(  # type: ignore[arg-type]
            server_repo=_Fakes([_server()]), signing_key=SIGNING_KEY
        )
        view = await service.screen(OWNER_ID, SERVER_ID, stage=2, idempotency_key="ik-42")
        assert view is not None
        # the wire callback round-trips with the key intact
        decoded = decode_callback(view.execute_callback, SIGNING_KEY)
        assert decoded.args == (str(SERVER_ID), "ik-42")
        # tampering with the key (e.g. stripping it to force a fresh saga)
        # is rejected, not re-interpreted
        _version, _key, sig = view.execute_callback.split("|")
        forged = f"{_version}|delete:execute:{SERVER_ID}:ik-99|{sig}"
        with pytest.raises(CallbackError, match="signature mismatch"):
            decode_callback(forged, SIGNING_KEY)

    def _harness(self):
        class _FakeProvider:
            def __init__(self) -> None:
                self.delete_calls: list[str] = []

            async def delete_server(self, provider_server_id: str, idempotency_key) -> None:
                self.delete_calls.append(provider_server_id)

            async def get_server(self, provider_server_id: str) -> ProviderServer | None:
                return None  # absent after delete

        class _FakeWaiter:
            async def wait_for(self, probe) -> WaitResult:
                return WaitResult(WaitOutcome.COMPLETED, 1, 0.1, None)

        class _FakeFinalCharge:
            def __init__(self) -> None:
                self.calls = 0

            async def charge_final(
                self, server: CloudServer, deleted_at: datetime
            ) -> FinalChargeResult:
                self.calls += 1
                return FinalChargeResult(
                    charged_minor=1000,
                    captured_hold=False,
                    posted_entry_key="final:x",
                    replayed=False,
                    capped=False,
                )

        class _HoldRepo:
            async def get_by_idempotency(self, wallet_id, key):
                return None

        class _HoldService:
            async def release_hold(self, wallet_id, hold_id, key):
                raise AssertionError("no hold to release on replay")

        class _WalletRepo:
            async def get(self, user_id):
                return Wallet(OWNER_ID, id=WALLET_ID, balance=10_000)

        server = _server()

        class _ServerRepo:
            def __init__(self) -> None:
                self._server = server

            async def get(self, server_id):
                return self._server if self._server.id == server_id else None

            async def save(self, s):
                self._server = s
                return s

        ops: dict[str, Operation] = {}

        class _OpRepo:
            async def get_or_create(
                self, *, operation_key, operation_type, resource_type, resource_id, provider_key
            ):
                op = ops.get(operation_key)
                if op is None:
                    op = Operation(
                        id=uuid4(),
                        operation_key=operation_key,
                        operation_type=operation_type,
                        resource_type=resource_type,
                        resource_id=resource_id,
                        provider_key=provider_key,
                    )
                    ops[operation_key] = op
                return op

            async def get_by_key(self, key):
                return ops.get(key)

            async def claim(self, operation_id):
                for op in ops.values():
                    if op.id == operation_id and op.status is OperationStatus.PENDING:
                        op.mark_in_flight()
                        return op
                return None

            async def save(self, operation):
                ops[operation.operation_key] = operation
                return operation

            async def list_pending(self, types):
                return [
                    op
                    for op in ops.values()
                    if op.status is OperationStatus.PENDING and op.operation_type in types
                ]

        class _Registry:
            def get(self, key):
                if key != "hetzner":
                    raise KeyError(key)
                return provider

        provider = _FakeProvider()
        final = _FakeFinalCharge()
        executor = DeleteOperationExecutor(
            operation_repo=_OpRepo(),  # type: ignore[arg-type]
            server_repo=_ServerRepo(),  # type: ignore[arg-type]
            provider_registry=_Registry(),  # type: ignore[arg-type]
            final_charge=final,  # type: ignore[arg-type]
            hold_repo=_HoldRepo(),  # type: ignore[arg-type]
            hold_service=_HoldService(),  # type: ignore[arg-type]
            wallet_repo=_WalletRepo(),  # type: ignore[arg-type]
            audit_repo=AsyncMock(),
            waiter=_FakeWaiter(),  # type: ignore[arg-type]
        )
        service = DeleteCommandService(
            server_repo=_ServerRepo(),  # type: ignore[arg-type]
            operation_repo=_OpRepo(),  # type: ignore[arg-type]
            provider_registry=_Registry(),  # type: ignore[arg-type]
            final_charge=final,  # type: ignore[arg-type]
            hold_repo=_HoldRepo(),  # type: ignore[arg-type]
            hold_service=_HoldService(),  # type: ignore[arg-type]
            wallet_repo=_WalletRepo(),  # type: ignore[arg-type]
            audit_repo=AsyncMock(),
            executor=executor,
        )
        return service, provider, final, ops

    async def test_replayed_command_uses_the_same_operation_key(self) -> None:
        service, provider, final, ops = self._harness()
        ik = "ik-42"
        first = await service.request(OWNER_ID, SERVER_ID, ik)
        second = await service.request(OWNER_ID, SERVER_ID, ik)
        assert first.replayed is False
        assert second.replayed is True
        # ONE operation for the saga key, never a second
        assert list(ops) == [delete_operation_key(SERVER_ID, ik)]
        assert provider.delete_calls == ["prov-1"]  # the provider delete ran once
        assert final.calls == 1  # the final charge ran once


class TestCreateReplay:
    """The create command: the same idempotency key replays the original
    outcome - no second hold, no second server, and a key owned by another
    user is rejected outright."""

    def _deps(self):
        servers = AsyncMock()
        servers.get_by_idempotency_key = AsyncMock(return_value=None)
        servers.create = AsyncMock(side_effect=lambda s, i: s)
        servers.save = AsyncMock(side_effect=lambda s: s)
        servers.count_active = AsyncMock(return_value=0)
        servers.count_total = AsyncMock(return_value=0)
        accounts = AsyncMock()
        accounts.get_active = AsyncMock(
            return_value=ProviderAccount(id=ACCOUNT_ID, user_id=OWNER_ID, provider_key="hetzner")
        )
        catalog = AsyncMock()
        catalog.get_offer = AsyncMock(
            return_value=OfferState(
                id=uuid4(),
                ref=REF,
                name="CX22",
                enabled=True,
                price_per_quantum=100,
                currency="EUR",
            )
        )
        books = MagicMock()
        books.sell_price = AsyncMock(return_value=_price())
        snaps = MagicMock()
        snaps.create_snapshot = AsyncMock(side_effect=lambda **kw: _snapshot())
        snaps.get_snapshot = AsyncMock(return_value=None)
        wallets = AsyncMock()
        wallets.get = AsyncMock(
            return_value=Wallet(user_id=OWNER_ID, id=WALLET_ID, balance=1000, currency="EUR")
        )
        holds = AsyncMock()
        holds.create_hold = AsyncMock(
            return_value=Hold(
                wallet_id=WALLET_ID,
                amount=107,
                currency="EUR",
                idempotency_key="server-create:ik-1",
                id=uuid4(),
            )
        )
        holds.get_by_idempotency = AsyncMock(return_value=None)
        holds.release_hold = AsyncMock(return_value=None)
        audit = AsyncMock()
        audit.append = AsyncMock(side_effect=lambda e: e)
        return servers, accounts, catalog, books, snaps, wallets, holds, audit

    def _service(self, *d) -> CreateServerService:
        servers, accounts, catalog, books, snaps, wallets, holds, audit = d
        return CreateServerService(  # type: ignore[call-arg]
            server_repo=servers,
            account_repo=accounts,
            catalog_repo=catalog,
            price_book_service=books,
            snapshot_service=snaps,
            wallet_repo=wallets,
            hold_repo=holds,
            audit_repo=audit,
            book_name="retail-eur",
        )

    async def test_replay_makes_no_second_hold_or_server(self) -> None:
        servers, accounts, catalog, books, snaps, wallets, holds, audit = self._deps()
        existing = CloudServer(
            id=uuid4(),
            user_id=OWNER_ID,
            provider_key="hetzner",
            provider_account_id=ACCOUNT_ID,
            state=ServerLifecycleState.PROVISIONING,
            idempotency_key="ik-1",
        )
        held = holds.create_hold.return_value
        service = self._service(servers, accounts, catalog, books, snaps, wallets, holds, audit)
        kwargs = dict(user=_owner(), offer_ref=REF, idempotency_key="ik-1", at=NOW)

        # First delivery: no row yet -> the command creates the intent.
        servers.get_by_idempotency_key = AsyncMock(return_value=None)
        holds.get_by_idempotency = AsyncMock(return_value=None)
        first = await service.create_server(**kwargs)

        # Second delivery of the SAME command: the row (and the original
        # hold) are read back -> an idempotent replay.
        servers.get_by_idempotency_key = AsyncMock(return_value=existing)
        holds.get_by_idempotency = AsyncMock(return_value=held)
        second = await service.create_server(**kwargs)

        assert first.replayed is False
        assert second.replayed is True
        assert second.server.id == existing.id
        assert holds.create_hold.call_count == 1  # funds reserved exactly once
        assert servers.create.call_count == 1  # and no second server row

    async def test_key_owned_by_another_user_is_rejected(self) -> None:
        servers, accounts, catalog, books, snaps, wallets, holds, audit = self._deps()
        other_server = CloudServer(
            id=uuid4(),
            user_id=ATTACKER_ID,
            provider_key="hetzner",
            provider_account_id=ACCOUNT_ID,
            state=ServerLifecycleState.PROVISIONING,
            idempotency_key="ik-1",
        )
        servers.get_by_idempotency_key = AsyncMock(return_value=other_server)
        service = self._service(servers, accounts, catalog, books, snaps, wallets, holds, audit)
        with pytest.raises(CreateServerCommandError, match="another user"):
            await service.create_server(
                user=_owner(), offer_ref=REF, idempotency_key="ik-1", at=NOW
            )
        assert holds.create_hold.call_count == 0


class TestPaymentWebhookReplay:
    """Payment webhook: a replayed callback cannot deposit twice - the
    service-level dedup and the ledger's unique idempotency key absorb the
    replay, and a forged/inflated signature is rejected at the gate."""

    def _harness(self):
        from cloud_platform.api.app import create_app
        from cloud_platform.api.routes.webhooks import get_gateway_payment_secret
        from cloud_platform.core.container import get_payment_webhook_service
        from cloud_platform.modules.payments.service import PaymentWebhookService

        secret = "security-sweep-secret"
        payments = AsyncMock()
        state: dict[str, object] = {}
        payments.get_by_external_id = AsyncMock(side_effect=lambda g, e: state.get("session"))
        payments.create = AsyncMock(side_effect=lambda s: dataclasses.replace(s, id=uuid4()))
        payments.save = AsyncMock(side_effect=lambda s: state.update({"session": s}) or s)
        wallet = AsyncMock()
        ledger = AsyncMock()
        ledger.get_entry_by_idempotency = AsyncMock(return_value=None)
        service = PaymentWebhookService(payments, wallet, ledger)
        app = create_app()
        app.dependency_overrides[get_payment_webhook_service] = lambda: service
        app.dependency_overrides[get_gateway_payment_secret] = lambda gateway_key: secret
        return TestClient(app), service, wallet, secret, OWNER_ID

    def _body(self, user_id: UUID, amount: int = 500) -> bytes:
        return json.dumps(
            {
                "gateway_payment_id": "ext-1",
                "status": "succeeded",
                "user_id": str(user_id),
                "amount_minor": amount,
                "currency": "EUR",
            }
        ).encode()

    def test_replayed_callback_credits_exactly_once(self) -> None:
        client, _service, wallet, secret, user_id = self._harness()
        body = self._body(user_id)
        sig = sign_gateway_payload(secret, body)

        first = client.post(
            "/webhooks/payments/zarinpal", content=body, headers={"x-gateway-signature": sig}
        )
        assert first.status_code == 200
        assert first.json()["action"] == "credited"

        second = client.post(
            "/webhooks/payments/zarinpal", content=body, headers={"x-gateway-signature": sig}
        )
        assert second.status_code == 200
        assert second.json()["action"] == "duplicate_ignored"
        wallet.add_funds.assert_awaited_once()  # exactly one deposit

    def test_inflated_amount_with_old_signature_is_rejected(self) -> None:
        client, _service, wallet, secret, user_id = self._harness()
        honest = self._body(user_id, amount=500)
        sig = sign_gateway_payload(secret, honest)
        dishonest = self._body(user_id, amount=50000)  # attacker inflates the amount
        response = client.post(
            "/webhooks/payments/zarinpal",
            content=dishonest,
            headers={"x-gateway-signature": sig},
        )
        assert response.status_code == 401
        wallet.add_funds.assert_not_awaited()

    def test_tampered_body_fails_verification(self) -> None:
        secret = "sweep-secret"
        body = b'{"status":"succeeded","amount_minor":5000}'
        sig = sign_gateway_payload(secret, body)
        assert verify_gateway_signature(secret, body, sig) is True
        assert verify_gateway_signature(secret, body.replace(b"succeeded", b"failed"), sig) is False
        assert verify_gateway_signature("other-secret", body, sig) is False


# ==========================================================================
# 3. SECRET REDACTION
# ==========================================================================


class TestLogRedaction:
    """Provider tokens, passwords and Bearer headers never survive the
    redaction processor, in flat, nested or list-shaped events."""

    @pytest.mark.parametrize(
        "field,value",
        [
            ("hetzner_api_token", "hn-secret-token-123"),
            ("authorization", "Bearer eyJhbGciOi.abc.def"),
            ("password", "hunter2"),
            ("api_key", "sk-live-123"),
            ("provider_credential_encryption_key", "FZ:abc"),
            ("ssh_private_key", "BEGIN OPENSSH PRIVATE KEY"),
            ("token", "t0k3n"),
            ("client_secret", "cs-123"),
        ],
    )
    def test_sensitive_field_values_never_reach_output(self, field: str, value: str) -> None:
        result = _redact_value({field: value, "user_id": 42})
        text = repr(result)
        assert value not in text, f"{field}={value} leaked into the log output"
        assert result["user_id"] == 42  # non-sensitive fields untouched

    def test_nested_and_listed_secrets_are_redacted(self) -> None:
        data = {
            "provider": {
                "credentials": {"token": "hn-xyz", "password": "pw"},
                "headers": {"Authorization": "Bearer abc.def.ghi"},
            },
            "requests": [{"token": "t1"}, {"password": "p2"}],
        }
        result = _redact_value(data)
        text = repr(result)
        assert "hn-xyz" not in text
        assert "Bearer abc.def.ghi" not in text
        assert result["provider"]["credentials"]["token"] == _REDACTED_PLACEHOLDER
        assert result["provider"]["credentials"]["password"] == _REDACTED_PLACEHOLDER
        assert result["requests"][0]["token"] == _REDACTED_PLACEHOLDER
        assert result["requests"][1]["password"] == _REDACTED_PLACEHOLDER

    def test_bearer_pattern_in_free_text_is_masked(self) -> None:
        value = "request failed: Authorization: Bearer sk-live-supersecret"
        redacted = _redact_value(value)
        assert _REDACTED_PLACEHOLDER in redacted
        assert "sk-live-supersecret" not in redacted

    def test_non_sensitive_keys_are_preserved(self) -> None:
        assert _redact_value({"server_id": SERVER_ID}) == {"server_id": SERVER_ID}
        assert _redact_value({"action": "login", "count": 3}) == {"action": "login", "count": 3}


class TestDsnRedaction:
    """Backup DSNs: the password is masked before it can reach logs or
    printed summaries."""

    def test_password_masked_host_port_db_kept(self) -> None:
        from cloud_platform.backup.job import redact_dsn

        dsn = "postgresql://backup:sup3r-s3cret@db.internal:5432/cloud"
        redacted = redact_dsn(dsn)
        assert "sup3r-s3cret" not in redacted
        assert ":***@" in redacted
        assert "db.internal:5432/cloud" in redacted

    def test_passwordless_dsn_unchanged(self) -> None:
        from cloud_platform.backup.job import redact_dsn

        dsn = "postgresql://backup@db.internal:5432/cloud"
        assert redact_dsn(dsn) == dsn


class TestSecretEnvelope:
    """Encrypted provider-secret envelopes: the repr never shows the key
    material, and a tampered envelope fails to decrypt (MAC check)."""

    def test_repr_hides_token_and_key(self) -> None:
        from cloud_platform.core.secrets import FernetSecretBox, MasterKey

        box = FernetSecretBox(MasterKey.generate())
        plaintext = "hn-live-provider-token-xyz"
        envelope = box.encrypt(plaintext)
        text = repr(envelope) + str(envelope)
        assert plaintext not in text
        assert envelope.token not in text
        assert box.decrypt(envelope) == plaintext

    def test_tampered_envelope_is_detected(self) -> None:
        from cloud_platform.core.secrets import (
            FernetSecretBox,
            MasterKey,
            SecretBoxError,
            SecretEnvelope,
        )

        box = FernetSecretBox(MasterKey.generate())
        envelope = box.encrypt("value-to-protect")
        tampered = SecretEnvelope(
            token=("A" if envelope.token[0] != "A" else "B") + envelope.token[1:],
            key_id=envelope.key_id,
        )
        with pytest.raises(SecretBoxError):
            box.decrypt(tampered)

    def test_wrong_master_key_is_detected(self) -> None:
        from cloud_platform.core.secrets import FernetSecretBox, MasterKey, SecretBoxError

        envelope = FernetSecretBox(MasterKey.generate()).encrypt("value-to-protect")
        wrong_box = FernetSecretBox(MasterKey.generate())
        with pytest.raises(SecretBoxError):
            wrong_box.decrypt(envelope)
