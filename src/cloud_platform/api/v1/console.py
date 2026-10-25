"""Website projection over the SAME wallet, catalog and server commands as Telegram."""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Sequence
from dataclasses import asdict, replace
from hashlib import sha256
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from cloud_platform.api.v1.dependencies import rate_limit, require_idempotency_key, require_scope
from cloud_platform.api.v1.errors import ApiError, ErrorCode
from cloud_platform.core.config import get_settings
from cloud_platform.core.container import get_container
from cloud_platform.db.base import BusinessLogEvent
from cloud_platform.modules.businesslog.domain import BusinessLogDispatcher
from cloud_platform.modules.checkout.service import CheckoutError
from cloud_platform.modules.hourly.service import HourlyError
from cloud_platform.modules.notifications.customer import enqueue_notice
from cloud_platform.modules.payments.recharge import RechargeError
from cloud_platform.modules.servers.guest import guest_management_configured
from cloud_platform.modules.servers.models import ServerOperation
from cloud_platform.modules.servers.service import ServerManagementError, ServerNotFoundError
from cloud_platform.modules.servers.test_identity import (
    TEST_IPV4,
    TEST_NAME,
    TEST_PROVIDER_ID,
    is_test_resource,
    is_test_server,
)
from cloud_platform.modules.tokens.domain import TokenAuthentication, TokenScope
from cloud_platform.modules.users.domain import PermissionDeniedError, User, UserStatus
from cloud_platform.modules.users.identity import IdentityRequiredError
from cloud_platform.modules.users.superadmins import is_superadmin
from cloud_platform.modules.wallet.domain import WalletError

CONSOLE_CONTRACT_VERSION = "1"


class ConsoleRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            try:
                if (
                    request.headers.get("x-platform-contract") != CONSOLE_CONTRACT_VERSION
                    or request.headers.get("x-platform-environment")
                    != get_settings().app_env.strip().lower()
                ):
                    raise ApiError(
                        ErrorCode.CONFLICT,
                        "نسخه قرارداد یا محیط سایت و پلتفرم سازگار نیست",
                    )
                response = await handler(request)
                response.headers["X-Platform-Contract"] = CONSOLE_CONTRACT_VERSION
                return response
            except (
                CheckoutError,
                HourlyError,
                RechargeError,
                WalletError,
                ServerManagementError,
            ) as exc:
                raise ApiError(
                    ErrorCode.CONFLICT,
                    "درخواست با وضعیت فعلی حساب، موجودی یا سرویس سازگار نیست؛ "
                    "شرایط ربات و سابقه را بررسی کنید",
                    {"kind": type(exc).__name__},
                ) from None
            except PermissionDeniedError:
                raise ApiError(ErrorCode.FORBIDDEN, "دسترسی به این عملیات مجاز نیست") from None
            except IdentityRequiredError:
                raise ApiError(
                    ErrorCode.FORBIDDEN, "ابتدا احراز هویت را در ربات تکمیل کنید"
                ) from None

        return guarded


router = APIRouter(
    prefix="/v1/console",
    tags=["website"],
    route_class=ConsoleRoute,
    dependencies=[Depends(rate_limit)],
)
link_router = APIRouter(
    prefix="/v1/console/link", tags=["website pairing"], route_class=ConsoleRoute
)
Read = Annotated[TokenAuthentication, Depends(require_scope(TokenScope.SERVERS_READ))]
Write = Annotated[TokenAuthentication, Depends(require_scope(TokenScope.SERVERS_WRITE))]
WalletRead = Annotated[TokenAuthentication, Depends(require_scope(TokenScope.WALLET_READ))]
CatalogRead = Annotated[TokenAuthentication, Depends(require_scope(TokenScope.CATALOG_READ))]
Key = Annotated[str, Depends(require_idempotency_key)]
AdminRead = Annotated[TokenAuthentication, Depends(require_scope(TokenScope.ADMIN_READ))]
AdminWrite = Annotated[TokenAuthentication, Depends(require_scope(TokenScope.ADMIN_WRITE))]


@link_router.get("/contract")
async def integration_contract() -> dict[str, Any]:
    settings = get_settings()
    return {
        "data": {
            "version": CONSOLE_CONTRACT_VERSION,
            "features": [
                "website-link-revoke:v1",
                "website-charge-requests:v1",
                "website-server-delegation:v1",
            ],
            "environment": settings.app_env.strip().lower(),
            "walletCurrency": settings.default_currency,
            "headerIdentityEnabled": settings.api_allow_header_identity,
            "usdUsdtParity": "1",
            "usdtSettlementEnabled": (
                settings.fx_enabled
                and settings.fx_domestic_enabled
                and settings.fx_allow_usdt_proxy_for_settlement
            ),
            "displayCurrency": settings.fx_default_display_currency,
            "confirmationSigningConfigured": len(settings.callback_signing_key.encode()) >= 32,
        }
    }


class Pairing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(pattern=r"^cpl_[A-Za-z0-9_-]{43}$")
    websiteUserId: UUID


@link_router.post("/exchange")
async def exchange(body: Pairing) -> dict[str, Any]:
    from cloud_platform.modules.tokens.website import exchange_pairing

    container = await get_container()
    try:
        user, token = await exchange_pairing(container, body.code, body.websiteUserId)
    except ValueError:
        raise ApiError(ErrorCode.UNAUTHORIZED, "کد اتصال منقضی شده یا قبلاً مصرف شده است") from None
    return {"data": {"id": str(user.id), "token": token}}


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmationToken: str | None = Field(default=None, max_length=2048)
    image: str | None = Field(default=None, max_length=200)
    port: int | None = Field(default=None, ge=1024, le=65535, strict=True)


class Recharge(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    amountMinor: int = Field(gt=0, le=2**63 - 1)
    gateway: str = Field(min_length=1, max_length=50)


class Purchase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    offerId: UUID
    image: str = Field(min_length=1, max_length=200)
    expectedMinor: int = Field(gt=0, le=2**63 - 1, strict=True)
    expectedCurrency: str = Field(min_length=3, max_length=3)


class WalletAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    amountMinor: int = Field(ge=-(2**63) + 1, le=2**63 - 1)
    reason: str = Field(min_length=4, max_length=500)


class CredentialAck(BaseModel):
    claimId: UUID


@router.post("/servers/{server_id}/credentials/claim")
async def claim_credentials(
    auth: Write, server_id: UUID, key: Key, response: Response
) -> dict[str, Any]:
    del key
    container = await get_container()
    user = await active_user(container, auth)
    owner = await target_owner(container, user, server_id, operation="credentials")
    claimed = await container.server_management_service(actor_user_id=user.id).claim_ssh_password(
        owner, server_id
    )
    if claimed is None:
        raise ApiError(ErrorCode.NOT_FOUND, "رمز یک‌بارمصرف قابل دریافت نیست")
    response.headers["Cache-Control"] = "no-store"
    return {
        "data": {
            "claimId": str(claimed.claim_id),
            "username": claimed.username,
            "password": claimed.reveal(),
        }
    }


@router.post("/servers/{server_id}/credentials/ack")
async def acknowledge_credentials(
    auth: Write, server_id: UUID, body: CredentialAck, key: Key
) -> dict[str, Any]:
    del key
    container = await get_container()
    user = await active_user(container, auth)
    owner = await target_owner(container, user, server_id, operation="credentials")
    ok = await container.server_management_service(actor_user_id=user.id).finish_ssh_password(
        owner, server_id, body.claimId, delivered=True
    )
    return {"data": {"ok": ok}}


async def superadmin(container: Any, auth: TokenAuthentication) -> User:
    user = await active_user(container, auth)
    if not is_superadmin(user, get_settings().telegram_superadmin_user_ids):
        raise ApiError(ErrorCode.FORBIDDEN, "دسترسی سوپرادمین تأییدشده لازم است")
    return user


@router.post("/admin/test-server/register")
async def register_fixture(auth: AdminWrite, key: Key) -> dict[str, Any]:
    del key
    from cloud_platform.modules.servers.test_fixture import register_test_server

    container = await get_container()
    actor = await superadmin(container, auth)
    try:
        server = await register_test_server(container, actor=actor)
    except ValueError:
        raise ApiError(
            ErrorCode.CONFLICT, "هویت سرور آزمایشی در یک حساب ارائه‌دهنده تأیید نشد"
        ) from None
    return {"data": {"serverId": str(server.id), "name": TEST_NAME}}


@router.get("/admin/users")
async def admin_users(auth: AdminRead, q: str = "") -> dict[str, Any]:
    container = await get_container()
    await superadmin(container, auth)
    users = await container.user_repository().search(q[:100], offset=0, limit=50)
    return {
        "data": [{"id": str(u.id), "username": u.username, "status": u.status.value} for u in users]
    }


class DelegationChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=4, max_length=500)
    operations: list[Literal["view", "refresh", "traffic", "ip_list", "monitoring"]] = Field(
        min_length=1, max_length=5
    )


class DelegationRevoke(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=4, max_length=500)


@router.post("/admin/servers/{server_id}/delegations/{user_id}")
async def delegate_server(
    auth: AdminWrite, server_id: UUID, user_id: UUID, body: DelegationChange, key: Key
) -> dict[str, Any]:
    container = await get_container()
    actor = await superadmin(container, auth)
    try:
        data = await container.server_access_repository().change(
            actor_id=actor.id,
            server_id=server_id,
            user_id=user_id,
            operations=body.operations,
            reason=body.reason,
            key=key,
            configured_ids=get_settings().telegram_superadmin_user_ids,
        )
    except ValueError:
        raise ApiError(
            ErrorCode.CONFLICT, "Delegation identity, scope or idempotency conflict"
        ) from None
    return {"data": data}


@router.post("/admin/servers/{server_id}/delegations/{user_id}/revoke")
async def revoke_server_delegation(
    auth: AdminWrite, server_id: UUID, user_id: UUID, body: DelegationRevoke, key: Key
) -> dict[str, Any]:
    container = await get_container()
    actor = await superadmin(container, auth)
    try:
        data = await container.server_access_repository().change(
            actor_id=actor.id,
            server_id=server_id,
            user_id=user_id,
            operations=[],
            reason=body.reason,
            key=key,
            configured_ids=get_settings().telegram_superadmin_user_ids,
            revoke=True,
        )
    except ValueError:
        raise ApiError(ErrorCode.CONFLICT, "Delegation identity or idempotency conflict") from None
    return {"data": data}


@router.post("/admin/users/{user_id}/wallet-adjustments")
async def wallet_adjust(
    auth: AdminWrite, user_id: UUID, body: WalletAdjustment, key: Key
) -> dict[str, Any]:
    container = await get_container()
    actor = await superadmin(container, auth)
    try:
        wallet, _ = await container.wallet_admin_service().adjust_balance(
            admin=actor,
            user_id=user_id,
            amount=body.amountMinor,
            reason=body.reason,
            idempotency_key=command_key(auth.user_id, key),
            expected_currency="IRT",
        )
    except ValueError:
        raise ApiError(
            ErrorCode.CONFLICT, "اصلاح موجودی با مبلغ، ارز یا وضعیت فعلی حساب سازگار نیست"
        ) from None
    return {"data": {"balanceMinor": wallet.balance, "currency": wallet.currency}}


async def active_user(container: Any, auth: TokenAuthentication) -> User:
    user: User | None = await container.user_repository().get(auth.user_id)
    if user is None or user.id != auth.user_id or user.status is not UserStatus.ACTIVE:
        raise ApiError(ErrorCode.FORBIDDEN, "حساب کاربری فعال نیست")
    return user


async def target_owner(
    container: Any, user: User, server_id: UUID, *, operation: str = "view"
) -> UUID:
    """Authorize access, retaining the real owner (never transfer it to a delegate)."""
    server = await container.server_repository().get(server_id)
    if server is None:
        raise ApiError(ErrorCode.NOT_FOUND, "سرور یافت نشد")
    if is_test_resource(server):
        if not is_test_server(server) or not await test_resource_visible(container, user, server):
            raise ApiError(ErrorCode.NOT_FOUND, "سرور یافت نشد")
        if not is_superadmin(user, get_settings().telegram_superadmin_user_ids):
            allowed = await container.server_access_repository().permissions(user.id, server)
            if operation not in allowed:
                raise ApiError(ErrorCode.FORBIDDEN, "Operation outside explicit delegation")
        return cast(UUID, server.user_id)
    if server.user_id == user.id:
        return cast(UUID, server.user_id)
    raise ApiError(ErrorCode.NOT_FOUND, "سرور یافت نشد")


async def test_resource_visible(container: Any, user: User, server: Any) -> bool:
    if not is_test_server(server):
        return False
    if is_superadmin(user, get_settings().telegram_superadmin_user_ids):
        return True
    factory = getattr(container, "server_access_repository", None)
    return factory is not None and "view" in await factory().permissions(user.id, server)


def command_key(user_id: UUID, key: str) -> str:
    return "web:" + sha256(f"{user_id}:{key}".encode()).hexdigest()


async def run_action(
    container: Any, user: User, server_id: UUID, action: str, body: Command, key: str
) -> dict[str, Any]:
    from cloud_platform.modules.operations.repository import SqlAlchemyOperationRepository
    from cloud_platform.modules.servers.commands import (
        ManagementReceiptConflict,
        execute_management,
    )
    from cloud_platform.modules.servers.management_lifecycle import prepare_observation

    actor_id = user.id
    if actor_id is None:
        raise ApiError(ErrorCode.FORBIDDEN, "حساب کاربری ثبت‌شده لازم است")
    if action not in {
        "power-on",
        "power-off",
        "reboot",
        "reinstall",
        "reset-password",
        "delete",
        "ssh-port",
    }:
        raise ApiError(ErrorCode.ACTION_NOT_ALLOWED, "Unsupported management action")
    await target_owner(
        container,
        user,
        server_id,
        operation={"power-on": "start", "power-off": "stop"}.get(action, action),
    )
    server = await container.server_repository().get(server_id)
    try:
        if action in {"reinstall", "reset-password"}:
            # Shared-service receipts also cover ordinary Telegram controls.
            return await _dispatch_action(container, user, server_id, action, body, key)
        return await execute_management(
            SqlAlchemyOperationRepository(container.session_factory),
            key=key,
            user_id=actor_id,
            server=server,
            action=action,
            arguments=body.model_dump(),
            prepare=lambda: prepare_observation(
                container.provider_registry, server, actor_id, action, body.model_dump()
            ),
            dispatch=lambda: _dispatch_action(container, user, server_id, action, body, key),
        )
    except ManagementReceiptConflict:
        raise ApiError(
            ErrorCode.CONFLICT,
            "این درخواست قبلاً ارسال شده یا کلید آن با اطلاعات دیگری استفاده شده است؛ "
            "سابقه عملیات را بررسی کنید",
        ) from None


async def _dispatch_action(
    container: Any, user: User, server_id: UUID, action: str, body: Command, key: str
) -> dict[str, Any]:
    actor_id = user.id
    if actor_id is None:
        raise ApiError(ErrorCode.FORBIDDEN, "حساب کاربری ثبت‌شده لازم است")
    owner = await target_owner(
        container,
        user,
        server_id,
        operation={"power-on": "start", "power-off": "stop"}.get(action, action),
    )
    management = container.server_management_service(actor_user_id=user.id)
    try:
        if action == "ssh-port":
            from cloud_platform.modules.servers.guest import change_test_ssh_port

            server = await container.server_repository().get(server_id)
            if body.port is None or not body.confirmationToken:
                raise ApiError(ErrorCode.VALIDATION_ERROR, "پورت و تأیید عملیات الزامی است")
            outcome = await change_test_ssh_port(
                container, user, server, body.port, body.confirmationToken, key
            )
        elif action == "power-on":
            outcome = await management.start_server(owner, server_id, idempotency_key=key)
        elif action == "power-off":
            outcome = await management.stop_server(
                owner, server_id, confirmation_token=body.confirmationToken, idempotency_key=key
            )
        elif action == "reboot":
            outcome = await management.reboot_server(owner, server_id, idempotency_key=key)
        elif action == "reinstall":
            if not body.image:
                raise ApiError(ErrorCode.VALIDATION_ERROR, "انتخاب سیستم‌عامل الزامی است")
            outcome = await management.reinstall(
                owner,
                server_id,
                image_ref=body.image,
                confirmation_token=body.confirmationToken,
                idempotency_key=key,
            )
        elif action == "reset-password":
            outcome = await management.reset_password(
                owner,
                server_id,
                confirmation_token=body.confirmationToken,
                idempotency_key=key,
            )
        elif action == "delete":
            if is_test_server(await container.server_repository().get(server_id)):
                raise ApiError(ErrorCode.ACTION_NOT_ALLOWED, "حذف سرور آزمایشی مشترک مجاز نیست")
            outcome = await management.delete_server(
                owner, server_id, confirmation_token=body.confirmationToken
            )
        else:
            raise ApiError(
                ErrorCode.ACTION_NOT_ALLOWED, "این عملیات توسط ارائه‌دهنده پشتیبانی نمی‌شود"
            )
    except ServerNotFoundError:
        raise ApiError(ErrorCode.NOT_FOUND, "سرور یافت نشد") from None
    except ServerManagementError as exc:
        await enqueue_notice(
            container.business_log_repository(),
            key=key + ":failed",
            user_id=actor_id,
            title=f"عملیات {action} روی سرور {str(server_id)[:8]} تکمیل نشد؛ وضعیت را بررسی کنید.",
            server_id=str(server_id),
        )
        raise ApiError(
            ErrorCode.CONFLICT,
            "عملیات تکمیل نشد؛ وضعیت فعلی و سابقه عملیات را بررسی کنید",
            {"kind": type(exc).__name__},
        ) from exc
    # An accepted provider request is never presented as completed provisioning.
    result = jsonable_encoder(asdict(outcome))
    from cloud_platform.modules.audit.domain import ActorType
    from cloud_platform.modules.audit.service import AuditTrail

    await AuditTrail(container.audit_repository()).record_mutation(
        actor_type=ActorType.ADMIN
        if is_superadmin(user, get_settings().telegram_superadmin_user_ids)
        else ActorType.USER,
        actor_id=user.id,
        action="console.server.operation",
        resource_type="server",
        resource_id=str(server_id),
        reason="Explicit website server-management request",
        metadata={"action": action, "accepted": outcome.accepted, "owner_id": str(owner)},
    )
    await enqueue_notice(
        container.business_log_repository(),
        key=key + ":result",
        user_id=actor_id,
        title=f"نتیجه درخواست {action} روی سرور {str(server_id)[:8]}: "
        + ("نیازمند بررسی" if outcome.outcome_unknown else "پذیرفته شد"),
        server_id=str(server_id),
    )
    return {"data": result}


@router.get("/account")
async def account(auth: Read) -> dict[str, Any]:
    container = await get_container()
    user = await active_user(container, auth)
    return {
        "data": {
            "id": str(user.id),
            "telegramUserId": str(user.telegram_user_id),
            "username": user.username,
            "superadmin": is_superadmin(user, get_settings().telegram_superadmin_user_ids),
            "testPortManagement": guest_management_configured(),
        }
    }


@router.post("/link/revoke")
async def revoke_website_link(auth: Read, key: Key) -> dict[str, Any]:
    """Revoke only the bearer used for this request, never another user's token."""
    container = await get_container()
    del key
    await container.token_service().revoke(actor_user_id=auth.user_id, token_id=auth.token_id)
    return {"data": {"revoked": True}}


@router.get("/charge-requests")
async def charge_requests(auth: WalletRead) -> dict[str, Any]:
    """Read existing payment facts. No settlement, invoice fabrication or FX lookup."""
    from cloud_platform.db.base import PaymentSession
    from cloud_platform.modules.fx.formatting import format_minor

    container = await get_container()
    user = await active_user(container, auth)
    async with container.session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(PaymentSession)
                    .where(PaymentSession.user_id == user.id)
                    .order_by(PaymentSession.created_at.desc(), PaymentSession.id.desc())
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )
    result: list[dict[str, Any]] = []
    for row in rows:
        amount = (
            row.credit_amount_minor if row.credit_amount_minor is not None else row.amount_minor
        )
        currency = row.credit_currency if row.credit_amount_minor is not None else row.currency
        if not currency or not isinstance(amount, int) or abs(amount) > 2**53 - 1:
            raise ApiError(ErrorCode.CONFLICT, "مبلغ پرداخت برای نمایش امن معتبر نیست")
        status = (
            "approved"
            if row.status == "succeeded" and row.credited_at is not None
            else "processing"
            if row.status in {"succeeded", "manual_review"}
            else "rejected"
            if row.status == "failed"
            else "pending"
        )
        result.append(
            {
                "id": str(row.id),
                "number": str(row.id),
                "amount": amount,
                "currency": currency,
                "formattedAmount": format_minor(amount, currency),
                "status": status,
                "date": row.created_at.isoformat(),
            }
        )
    return {"data": result}


@router.get("/wallet")
async def wallet(auth: WalletRead) -> dict[str, Any]:
    container = await get_container()
    user = await active_user(container, auth)
    balance = await container.wallet_history_service().balance(user.id)
    history = await container.wallet_history_service().history(user.id, limit=50)
    return {
        "data": {
            "balance": jsonable_encoder(asdict(balance)),
            "history": jsonable_encoder(asdict(history)),
        }
    }


@router.get("/recharge/gateways")
async def gateways(auth: WalletRead) -> dict[str, Any]:
    container = await get_container()
    await active_user(container, auth)
    wallet = await container.wallet_repository().get(auth.user_id)
    adapters = container.payment_gateways()
    try:
        service = container.wallet_recharge_service(gateways=adapters)
        keys = await service.compatible_gateways_async(wallet.currency if wallet else "IRT")
        return {"data": keys}
    finally:
        await container.aclose_gateways(adapters)


@router.post("/recharge", status_code=202)
async def recharge(auth: WalletRead, body: Recharge, key: Key) -> dict[str, Any]:
    if not auth.has(TokenScope.SERVERS_WRITE):
        raise ApiError(ErrorCode.FORBIDDEN, "write scope required")
    container = await get_container()
    user = await active_user(container, auth)
    wallet = await container.wallet_repository().get(auth.user_id)
    if wallet is None:
        raise ApiError(ErrorCode.NOT_FOUND, "کیف پول یافت نشد")
    adapters = container.payment_gateways()
    try:
        service = container.wallet_recharge_service(gateways=adapters)
        result = await service.start(
            user=user,
            amount_minor=body.amountMinor,
            currency=wallet.currency,
            idempotency_key=command_key(auth.user_id, key),
            gateway_key=body.gateway,
        )
        return {
            "data": {
                "paymentUrl": result.redirect_url,
                "replayed": result.replayed,
                "details": jsonable_encoder(asdict(result.payment_details))
                if result.payment_details
                else None,
            }
        }
    finally:
        await container.aclose_gateways(adapters)


@router.get("/servers")
async def servers(auth: Read) -> dict[str, Any]:
    container = await get_container()
    user = await active_user(container, auth)
    management = container.server_management_service(actor_user_id=user.id)
    first_page = await management.list_servers(user.id)
    items = list(first_page.items)
    for page in range(2, first_page.pages + 1):
        items.extend((await management.list_servers(user.id, page=page)).items)
    ids: Sequence[UUID] = ()
    if is_superadmin(user, get_settings().telegram_superadmin_user_ids):
        from cloud_platform.db.base import Server

        async with container.session_factory() as session:
            ids = (
                (
                    await session.execute(
                        select(Server.id).where(
                            Server.provider_server_id == TEST_PROVIDER_ID,
                            Server.ipv4 == TEST_IPV4,
                            Server.funding_source == "operator",
                        )
                    )
                )
                .scalars()
                .all()
            )
    else:
        factory = getattr(container, "server_access_repository", None)
        if factory is not None:
            ids = await factory().server_ids(user.id)
    existing = {item.server_id for item in items}
    for server_id in ids:
        if server_id not in existing:
            try:
                owner = await target_owner(container, user, server_id)
                items.append(await management.get_server(owner, server_id))
                existing.add(server_id)
            except (ApiError, ServerNotFoundError):
                # A concurrent revoke fails closed, not a partial-authority row.
                continue
    projected = []
    for item in items:
        stored = await container.server_repository().get(item.server_id)
        if is_test_resource(stored):
            if not await test_resource_visible(container, user, stored):
                continue
            item = replace(item, display_name=TEST_NAME)
        projected.append(asdict(item))
    return {"data": jsonable_encoder(projected)}


@router.get("/servers/{server_id}")
async def detail(auth: Read, server_id: UUID) -> dict[str, Any]:
    container = await get_container()
    user = await active_user(container, auth)
    owner = await target_owner(container, user, server_id)
    service = container.server_management_service(actor_user_id=user.id)
    view = await service.get_server(owner, server_id)
    shared_test = is_test_server(await container.server_repository().get(server_id))
    if shared_test:
        view = replace(view, display_name=TEST_NAME)
    operations = await service.available_operations(owner, server_id)
    if shared_test:
        operations -= {ServerOperation.DELETE}
    return {
        "data": {
            "server": jsonable_encoder(asdict(view)),
            "operations": jsonable_encoder(operations),
            "credentialsAllowed": not shared_test
            or is_superadmin(user, get_settings().telegram_superadmin_user_ids),
        }
    }


@router.get("/servers/{server_id}/images")
async def images(auth: Read, server_id: UUID) -> dict[str, Any]:
    container = await get_container()
    user = await active_user(container, auth)
    owner = await target_owner(container, user, server_id, operation="reinstall")
    result = await container.server_management_service(actor_user_id=user.id).reinstall_images(
        owner, server_id
    )
    return {"data": jsonable_encoder([asdict(item) for item in result])}


@router.get("/servers/{server_id}/operations")
async def operation_history(auth: Read, server_id: UUID) -> dict[str, Any]:
    from cloud_platform.modules.servers.management_lifecycle import management_history

    container = await get_container()
    user = await active_user(container, auth)
    await target_owner(container, user, server_id)
    return {"data": await management_history(container, server_id)}


@router.post("/servers/{server_id}/confirmations/{action}")
async def confirmation(
    auth: Write, server_id: UUID, action: str, body: Command, key: Key
) -> dict[str, Any]:
    del key
    container = await get_container()
    user = await active_user(container, auth)
    owner = await target_owner(
        container,
        user,
        server_id,
        operation={"power-on": "start", "power-off": "stop"}.get(action, action),
    )
    if action == "ssh-port":
        from cloud_platform.modules.servers.guest import port_binding

        server = await container.server_repository().get(server_id)
        if body.port is None:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "پورت جدید الزامی است")
        try:
            binding = port_binding(user, server, body.port)
        except (ValueError, PermissionError):
            raise ApiError(
                ErrorCode.ACTION_NOT_ALLOWED, "مدیریت پورت این سرور پیکربندی یا مجاز نشده است"
            ) from None
        return {
            "data": {"confirmationToken": container.confirmation_verifier().issue(binding).token}
        }
    operations = {
        "power-off": ServerOperation.STOP,
        "reinstall": ServerOperation.REINSTALL,
        "reset-password": ServerOperation.PASSWORD_RESET,
        "delete": ServerOperation.DELETE,
    }
    if action not in operations:
        raise ApiError(ErrorCode.ACTION_NOT_ALLOWED, "عملیات تأییدپذیر نیست")
    token = await container.server_management_service(actor_user_id=user.id).issue_confirmation(
        owner,
        server_id,
        operations[action],
        arguments={"image": body.image} if action == "reinstall" else None,
    )
    return {"data": {"confirmationToken": token}}


@router.post("/servers/{server_id}/actions/{action}", status_code=202)
async def action(
    auth: Write, server_id: UUID, action: str, body: Command, key: Key
) -> dict[str, Any]:
    container = await get_container()
    user = await active_user(container, auth)
    return await run_action(
        container, user, server_id, action, body, command_key(auth.user_id, key)
    )


@router.get("/notifications")
async def notifications(auth: Read) -> dict[str, Any]:
    container = await get_container()
    # Frozen/banned users must still be able to see their account notice.
    async with container.session_factory() as session:
        records = (
            (
                await session.execute(
                    select(BusinessLogEvent)
                    .where(
                        BusinessLogEvent.event_type.in_(
                            ["customer.notice", "customer.server_provisioned"]
                        ),
                        BusinessLogEvent.payload["user_id"].astext == str(auth.user_id),
                    )
                    .order_by(BusinessLogEvent.created_at.desc())
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        return {
            "data": [
                {
                    "id": str(row.id),
                    "text": BusinessLogDispatcher.render(row),
                    "status": row.status,
                    "createdAt": row.created_at.isoformat(),
                }
                for row in records
            ]
        }


@router.get("/offers")
async def offers(auth: CatalogRead) -> dict[str, Any]:
    container = await get_container()
    await active_user(container, auth)
    rows = await container.sellable_offer_repository().list_sellable()
    # The bot's eligibility/currency/provenance checks remain authoritative.
    from cloud_platform.modules.fx.formatting import format_minor
    from cloud_platform.modules.offers.domain import is_sellable_in_currency

    view = container.offer_catalog_view_service()
    result = []
    for offer in rows:
        if not is_sellable_in_currency(offer, container.catalog_currency):
            continue
        try:
            toman = await view.toman_price(offer)
        except CheckoutError:
            toman = None
        result.append(
            {
                "id": str(offer.id),
                "name": offer.name,
                "provider": offer.provider_key,
                "location": offer.location_id,
                "billingModel": offer.billing_model,
                "priceMinor": offer.selling_price_minor,
                "currency": offer.selling_currency,
                "displayCurrency": "IRT",
                "displayPriceMinor": toman,
                "formattedPrice": format_minor(toman, "IRT")
                if toman is not None
                else "قیمت تومانی موقتاً در دسترس نیست",
                "cpu": offer.vcpu,
                "ramGb": offer.ram_gb,
                "diskGb": offer.disk_gb,
            }
        )
    return {"data": result}


@router.get("/offers/{offer_id}/images")
async def offer_images(auth: CatalogRead, offer_id: UUID) -> dict[str, Any]:
    container = await get_container()
    await active_user(container, auth)
    offer = await container.sellable_offer_repository().get(offer_id)
    if offer is None or not offer.sellable:
        raise ApiError(ErrorCode.NOT_FOUND, "محصول یافت نشد")
    if offer.billing_model != "prepaid_monthly_fixed":
        resolver = container.hourly_cloud_resolver()
        adapter = (
            resolver.adapter_for(offer.provider_key, offer.provider_account_id)
            if resolver
            else None
        )
        if adapter is None:
            raise ApiError(ErrorCode.ACTION_NOT_ALLOWED, "فهرست تصاویر در دسترس نیست")
        options = await adapter.list_images(offer.location_id)
        return {"data": [{"id": str(item.id), "name": str(item.label)} for item in options]}
    options = await container.offer_catalog_view_service().os_options(offer)
    return {"data": [{"id": item.name, "name": item.name} for item in options]}


@router.post("/orders", status_code=202)
async def purchase(auth: Write, body: Purchase, key: Key) -> dict[str, Any]:
    if not auth.has(TokenScope.CATALOG_READ):
        raise ApiError(ErrorCode.FORBIDDEN, "catalog scope required")
    container = await get_container()
    user = await active_user(container, auth)
    offer = await container.sellable_offer_repository().get(body.offerId)
    if offer is None or not offer.sellable:
        raise ApiError(ErrorCode.NOT_FOUND, "محصول یافت نشد")
    args = {
        "user": user,
        "offer_id": offer.id,
        "idempotency_key": command_key(auth.user_id, key),
        "expected_selling_price_minor": body.expectedMinor,
        "expected_selling_currency": body.expectedCurrency,
    }
    if offer.billing_model == "prepaid_monthly_fixed":
        result = await container.monthly_checkout_service().create_order(**args, os_name=body.image)
    else:
        result = await container.hourly_cloud_service().create_instance(
            **args, image_id=body.image, image_label=body.image
        )
    return {
        "data": {
            "serverId": str(result.server.id),
            "replayed": result.replayed,
            "status": "requested",
        }
    }
