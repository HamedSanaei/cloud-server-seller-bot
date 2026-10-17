"""Private account pairing and shared-superadmin test VPS controls."""

from __future__ import annotations

import secrets
from dataclasses import asdict
from typing import Any

from aiogram import Dispatcher, F
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from cloud_platform.core.config import get_settings
from cloud_platform.core.session_store import NS_REFERENCE
from cloud_platform.modules.notifications.customer import enqueue_notice
from cloud_platform.modules.servers.models import ServerOperation
from cloud_platform.modules.servers.test_fixture import find_test_server, register_test_server
from cloud_platform.modules.tokens.website import issue_pairing
from cloud_platform.modules.users.superadmins import is_superadmin


def register_bridge_handlers(dp: Dispatcher, container: Any, resolve_user: Any) -> None:
    async def user_for(message: Message, sender: Any) -> Any:
        if message.chat.type != ChatType.PRIVATE or sender is None or message.chat.id != sender.id:
            return None
        return await resolve_user(container, sender)

    @dp.message(Command("website"))
    async def website(message: Message) -> None:
        user = await user_for(message, message.from_user)
        if user is None:
            return
        try:
            code = await issue_pairing(container, user)
            await message.answer(
                "اتصال حساب سایت به همین حساب تلگرام\n"
                "در سایت، بخش «پلتفرم مشترک» این کد را وارد کنید.\n"
                "کد یک‌بارمصرف است و پنج دقیقه اعتبار دارد؛ آن را برای دیگران ارسال نکنید.\n\n"
                + code,
                parse_mode=None,
            )
        except Exception:
            await message.answer("صدور کد اتصال فعلاً ممکن نیست؛ وضعیت حساب را بررسی کنید.")

    async def button(user: Any, action: str, **values: Any) -> InlineKeyboardButton:
        nonce = secrets.token_urlsafe(16)
        await container.session_store().put(
            NS_REFERENCE,
            "bridge:" + nonce,
            {"user_id": str(user.id), "action": action, **values},
            ttl_seconds=300,
        )
        return InlineKeyboardButton(
            text=values.get("label", action), callback_data="bridge:" + nonce
        )

    @dp.message(Command("testserver"))
    async def testserver(message: Message) -> None:
        user = await user_for(message, message.from_user)
        if not is_superadmin(user, get_settings().telegram_superadmin_user_ids):
            await message.answer("دسترسی سوپرادمین تأییدشده لازم است.")
            return
        try:
            server = await find_test_server(container) or await register_test_server(
                container, actor=user
            )
            management = container.server_management_service(actor_user_id=user.id)
            view = await management.get_server(server.user_id, server.id)
            rows = []
            for action, label in [
                ("start", "روشن کردن"),
                ("stop", "خاموش کردن"),
                ("reboot", "راه‌اندازی مجدد"),
                ("images", "نصب مجدد سیستم‌عامل"),
                ("password_reset", "بازنشانی رمز عبور"),
                ("credentials", "دریافت رمز یک‌بارمصرف"),
                ("history", "سابقه و نتیجه واقعی عملیات"),
            ]:
                rows.append([await button(user, action, label=label, server_id=str(server.id))])
            await message.answer(
                f"سرور آزمایشی مشترک leaseweb-test\nوضعیت: {view.state}\n"
                f"IPv4: {server.ipv4}\nسیستم‌عامل: {view.operating_system or 'نامشخص'}\n"
                "نتیجه درخواست و وضعیت نهایی در سابقه سایت و تلگرام قابل مشاهده است.\n"
                "تغییر پورت آزمایشی، پس از تنظیم اتصال SSH: /testport 2222",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
        except Exception:
            await message.answer(
                "ثبت یا خواندن سرور آزمایشی تأیید نشد؛ اتصال ارائه‌دهنده و پین حساب را بررسی کنید."
            )

    @dp.message(Command("testport"))
    async def testport(message: Message) -> None:
        user = await user_for(message, message.from_user)
        if not is_superadmin(user, get_settings().telegram_superadmin_user_ids):
            await message.answer("دسترسی سوپرادمین لازم است")
            return
        try:
            from cloud_platform.modules.servers.guest import port_binding

            port = int((message.text or "").split()[1])
            server = await find_test_server(container)
            if server is None:
                raise ValueError("verified test server is not registered")
            binding = port_binding(user, server, port)
            token = container.confirmation_verifier().issue(binding).token
            confirm = await button(
                user,
                "ssh_port",
                label="تأیید افزودن پورت SSH",
                confirmed=True,
                token=token,
                port=port,
                server_id=str(server.id),
            )
            await message.answer(
                f"افزودن پورت SSH {port} به سرور آزمایشی تأیید شود؟\nپورت قبلی حفظ می‌شود.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[confirm]]),
            )
        except Exception:
            await message.answer(
                "پورت معتبر ۱۰۲۴ تا ۶۵۵۳۵ وارد کنید؛ اتصال SSH سرور آزمایشی "
                "باید از قبل پیکربندی شده باشد."
            )

    @dp.callback_query(F.data.startswith("bridge:"))
    async def callback(query: CallbackQuery) -> None:
        if not isinstance(query.message, Message):
            return
        user = await user_for(query.message, query.from_user)
        if not is_superadmin(user, get_settings().telegram_superadmin_user_ids):
            await query.answer("دسترسی مجاز نیست", show_alert=True)
            return
        record = await container.session_store().get(NS_REFERENCE, query.data or "")
        # Stored namespace key intentionally equals callback data.
        if not record or record.get("user_id") != str(user.id):
            await query.answer("درخواست منقضی شده است", show_alert=True)
            return
        await query.answer()
        server = await find_test_server(container)
        if server is None or str(server.id) != record.get("server_id"):
            await query.message.answer("سرور آزمایشی یافت نشد")
            return
        action = str(record["action"])
        management = container.server_management_service(actor_user_id=user.id)
        try:
            if action == "history":
                from cloud_platform.modules.servers.management_lifecycle import management_history

                history = await management_history(container, server.id)
                lines = [
                    f"{item['action'] or item['type']} · {item['status']}"
                    + (f" · {item['detail']}" if item["detail"] else "")
                    for item in history[:15]
                ]
                await query.message.answer("\n".join(lines) or "هنوز عملیاتی ثبت نشده است.")
                return
            if action == "credentials":
                claimed_secret = await management.claim_ssh_password(server.user_id, server.id)
                if claimed_secret is None:
                    await query.message.answer("رمز یک‌بارمصرف قابل دریافت نیست")
                    return
                try:
                    await query.message.answer(
                        f"نام کاربری: {claimed_secret.username}\nرمز: {claimed_secret.reveal()}",
                        parse_mode=None,
                        protect_content=True,
                    )
                except Exception:
                    await management.finish_ssh_password(
                        server.user_id, server.id, claimed_secret.claim_id, delivered=False
                    )
                    return
                await management.finish_ssh_password(
                    server.user_id, server.id, claimed_secret.claim_id, delivered=True
                )
                return
            if action == "images":
                images = await management.reinstall_images(server.user_id, server.id)
                rows = [
                    [
                        await button(
                            user,
                            "reinstall",
                            label=image.name,
                            server_id=str(server.id),
                            image=image.ref,
                        )
                    ]
                    for image in images
                ]
                await query.message.answer(
                    "سیستم‌عامل را انتخاب کنید؛ نصب مجدد اطلاعات را پاک می‌کند.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                )
                return
            if not record.get("confirmed"):
                op = ServerOperation(action)
                args = {"image": record["image"]} if action == "reinstall" else None
                token = await management.issue_confirmation(
                    server.user_id, server.id, op, arguments=args
                )
                confirm = await button(
                    user,
                    action,
                    label="تأیید اجرای عملیات",
                    confirmed=True,
                    token=token,
                    server_id=str(server.id),
                    image=record.get("image"),
                )
                await query.message.answer(
                    f"عملیات {action} روی leaseweb-test اجرا شود؟\n"
                    "نصب مجدد اطلاعات دیسک را پاک می‌کند.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[confirm]]),
                )
                return
            claimed = await container.session_store().take(NS_REFERENCE, query.data or "")
            if not claimed:
                await query.message.answer("این درخواست قبلاً استفاده شده است")
                return
            key = "bot-test:" + (query.data or "")

            async def dispatch() -> Any:
                if action == "ssh_port":
                    from cloud_platform.modules.servers.guest import change_test_ssh_port

                    return await change_test_ssh_port(
                        container, user, server, record["port"], record["token"], key
                    )
                if action == "start":
                    return await management.start_server(
                        server.user_id, server.id, idempotency_key=key
                    )
                if action == "stop":
                    return await management.stop_server(
                        server.user_id,
                        server.id,
                        confirmation_token=record["token"],
                        idempotency_key=key,
                    )
                if action == "reboot":
                    return await management.reboot_server(
                        server.user_id, server.id, idempotency_key=key
                    )
                if action == "reinstall":
                    return await management.reinstall(
                        server.user_id,
                        server.id,
                        image_ref=record["image"],
                        confirmation_token=record["token"],
                        idempotency_key=key,
                    )
                if action == "password_reset":
                    return await management.reset_password(
                        server.user_id,
                        server.id,
                        confirmation_token=record["token"],
                        idempotency_key=key,
                    )
                raise ValueError("unsupported test operation")

            from cloud_platform.modules.operations.repository import SqlAlchemyOperationRepository
            from cloud_platform.modules.servers.commands import execute_management
            from cloud_platform.modules.servers.management_lifecycle import prepare_observation

            if action in {"reinstall", "password_reset"}:
                result = asdict(await dispatch())
            else:
                result = await execute_management(
                    SqlAlchemyOperationRepository(container.session_factory),
                    key=key,
                    user_id=user.id,
                    server=server,
                    action=action,
                    arguments={"image": record.get("image"), "port": record.get("port")},
                    prepare=lambda: prepare_observation(
                        container.provider_registry, server, user.id, action, record
                    ),
                    dispatch=dispatch,
                )
            from cloud_platform.modules.audit.domain import ActorType
            from cloud_platform.modules.audit.service import AuditTrail

            await AuditTrail(container.audit_repository()).record_mutation(
                actor_type=ActorType.ADMIN,
                actor_id=user.id,
                action="telegram.test_server.operation",
                resource_type="server",
                resource_id=str(server.id),
                reason="Explicit superadmin Telegram request",
                metadata={
                    "action": action,
                    "accepted": bool(result.get("accepted")),
                    "owner_id": str(server.user_id),
                },
            )
            text = f"درخواست {action} برای leaseweb-test پذیرفته شد؛ وضعیت نهایی را بررسی کنید."
            if result.get("outcome_unknown"):
                text = "نتیجه عملیات هنوز قطعی نیست؛ عملیات را تکرار نکنید."
            await enqueue_notice(
                container.business_log_repository(),
                key=key,
                user_id=user.id,
                title=text,
                server_id=str(server.id),
            )
            await query.message.answer(text)
        except Exception:
            text = "عملیات تکمیل نشد یا نتیجه نیازمند بررسی است؛ وضعیت سرور را بررسی کنید."
            await enqueue_notice(
                container.business_log_repository(),
                key=str(query.data) + ":failed",
                user_id=user.id,
                title=text,
                server_id=str(server.id),
            )
            await query.message.answer(text)
