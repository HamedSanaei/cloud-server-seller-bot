"""Implemented support, sales, health and durable message management screens."""

from __future__ import annotations

import secrets
from uuid import UUID

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cloud_platform.bot.ui import BotScreen
from cloud_platform.modules.navigation.domain import (
    Callback,
    decode_offer_ref,
    encode_offer_ref,
    encode_telegram_callback,
)


class _Section:
    def __init__(self, key, service, store, translator, *, ttl_seconds=900):
        self._key, self._service, self._store, self._t, self._ttl = (
            key,
            service,
            store,
            translator,
            ttl_seconds,
        )

    def button(self, label, screen, *args):
        return InlineKeyboardButton(
            text=label,
            callback_data=encode_telegram_callback(Callback("admin", screen, args), self._key),
        )

    def screen(self, text, rows=None):
        rows = rows or []
        rows.append([self.button(self._t.t("nav.cancel"), "menu")])
        return BotScreen(text, InlineKeyboardMarkup(inline_keyboard=rows))


class AdminSupportUi(_Section):
    async def clear_prompt(self, admin):
        await self._service.clear(admin)
        await self._store.delete("admin-support-input", str(admin.id))

    async def _detail(self, admin, server_id):
        view = await self._service.detail(admin, server_id)
        ref = secrets.token_urlsafe(6)
        await self._store.put(
            "admin-support-target",
            f"{admin.id}:{ref}",
            {"server_id": str(server_id)},
            ttl_seconds=self._ttl,
        )
        rows = [
            [self.button(self._t.t(f"admin.action.{action}"), "s_reason", ref, action)]
            for action in view.actions
        ]
        return self.screen(
            self._t.t(
                "admin.support_detail",
                id=view.server_id,
                owner=view.owner_id,
                provider=view.provider_key,
                status=view.state,
                ip=view.ipv4 or "—",
            ),
            rows,
        )

    async def handle(self, cb, admin):
        if cb.screen == "services":
            await self._store.put(
                "admin-support-input", str(admin.id), {"step": "server"}, ttl_seconds=self._ttl
            )
            return self.screen(self._t.t("admin.support_prompt"))
        if cb.screen == "s_reason" and len(cb.args) == 2:
            target = await self._store.get("admin-support-target", f"{admin.id}:{cb.args[0]}")
            if target is None:
                return self.screen(self._t.t("nav.expired"))
            await self._store.put(
                "admin-support-input",
                str(admin.id),
                {"step": "reason", "server_id": target["server_id"], "action": cb.args[1]},
                ttl_seconds=self._ttl,
            )
            return self.screen(self._t.t("admin.reason_prompt"))
        if cb.screen == "s_apply" and len(cb.args) == 1:
            status = await self._service.apply(admin, cb.args[0])
            return self.screen(self._t.t("admin.operation_status", status=status.value))
        return None

    async def handle_text(self, text, admin):
        pending = await self._store.get("admin-support-input", str(admin.id))
        if pending is None:
            return None
        if pending["step"] == "server":
            screen = await self._detail(admin, UUID(text.strip()))
        else:
            nonce = await self._service.prepare(
                admin, UUID(pending["server_id"]), pending["action"], text
            )
            screen = self.screen(
                self._t.t(
                    "admin.support_confirm",
                    id=pending["server_id"],
                    action=pending["action"],
                    reason=text.strip(),
                ),
                [[self.button(self._t.t("admin.confirm_action"), "s_apply", nonce)]],
            )
        await self._store.delete("admin-support-input", str(admin.id))
        return screen


class AdminSalesUi(_Section):
    async def clear_prompt(self, admin):
        await self._service.clear(admin)
        await self._store.delete("admin-sales-input", str(admin.id))

    async def handle(self, cb, admin):
        if cb.screen in {"sales", "c_page"}:
            offset = int(cb.args[0]) if cb.args else 0
            offers = await self._service.list_offers(admin, offset)
            rows = [
                [
                    self.button(
                        f"{offer.name[:40]} · {offer.provider_key}",
                        "c_offer",
                        encode_offer_ref(offer.id),
                    )
                ]
                for offer in offers
            ]
            if offset:
                rows.append(
                    [self.button(self._t.t("nav.prev"), "c_page", str(max(0, offset - 10)))]
                )
            if len(offers) == 10 and offset < 10000:
                rows.append([self.button(self._t.t("nav.next"), "c_page", str(offset + 10))])
            return self.screen(self._t.t("admin.sales"), rows)
        if cb.screen == "c_offer" and len(cb.args) == 1:
            offer = await self._service.detail(admin, decode_offer_ref(cb.args[0]))
            actions = (
                ["hide"]
                if offer.enabled
                else (
                    ["show"] if offer.provider_available and offer.selling_price_minor > 0 else []
                )
            )
            actions += [
                "set_price",
                "block_location",
                "unblock_location",
                "block_provider",
                "unblock_provider",
            ]
            ref = secrets.token_urlsafe(6)
            await self._store.put(
                "admin-sales-target",
                f"{admin.id}:{ref}",
                {"offer_id": str(offer.id), "currency": offer.selling_currency},
                ttl_seconds=self._ttl,
            )
            rows = [
                [self.button(self._t.t(f"admin.action.{action}"), "c_reason", ref, action)]
                for action in actions
            ]
            return self.screen(
                f"{offer.name}\n{offer.provider_key} / {offer.location_id}\n"
                f"{offer.selling_price_minor} {offer.selling_currency}",
                rows,
            )
        if cb.screen == "c_reason" and len(cb.args) == 2:
            record = await self._store.get("admin-sales-target", f"{admin.id}:{cb.args[0]}")
            if record is None:
                return self.screen(self._t.t("nav.expired"))
            await self._store.put(
                "admin-sales-input",
                str(admin.id),
                {
                    **record,
                    "action": cb.args[1],
                    "step": "price" if cb.args[1] == "set_price" else "reason",
                },
                ttl_seconds=self._ttl,
            )
            return self.screen(
                self._t.t("admin.price_prompt", currency=record["currency"])
                if cb.args[1] == "set_price"
                else self._t.t("admin.reason_prompt")
            )
        if cb.screen == "c_apply" and len(cb.args) == 1:
            action = await self._service.apply(admin, cb.args[0])
            return self.screen(self._t.t("admin.operation_status", status=action))
        return None

    async def handle_text(self, text, admin):
        record = await self._store.get("admin-sales-input", str(admin.id))
        if record is None:
            return None
        if record.get("step") == "price":
            value = text.strip()
            if (
                not value.isascii()
                or not value.isdigit()
                or len(value) > 19
                or not 0 < int(value) < 2**63
            ):
                return self.screen(self._t.t("admin.price_prompt", currency=record["currency"]))
            await self._store.put(
                "admin-sales-input",
                str(admin.id),
                {**record, "step": "reason", "price_minor": int(value)},
                ttl_seconds=self._ttl,
            )
            return self.screen(self._t.t("admin.reason_prompt"))
        nonce = await self._service.prepare(
            admin,
            UUID(record["offer_id"]),
            record["action"],
            text,
            price_minor=record.get("price_minor"),
        )
        await self._store.delete("admin-sales-input", str(admin.id))
        return self.screen(
            self._t.t(
                "admin.support_confirm",
                id=record["offer_id"],
                action=record["action"]
                + (
                    f" {record['price_minor']} {record['currency']} minor"
                    if record.get("price_minor")
                    else ""
                ),
                reason=text.strip(),
            ),
            [[self.button(self._t.t("admin.confirm_action"), "c_apply", nonce)]],
        )


class AdminHealthUi(_Section):
    async def clear_prompt(self, admin):
        return None

    async def handle_text(self, text, admin):
        return None

    async def handle(self, cb, admin):
        if cb.screen == "health":
            facts = await self._service.snapshot(admin)
            lines = [self._t.t("admin.health"), f"DB: {facts['database']}"]
            lines.extend(
                f"{name}: {status}" for name, status in facts.get("dependencies", {}).items()
            )
            lines.extend(
                f"server/{kind}: {count}" for kind, count in facts.get("servers", {}).items()
            )
            lines.extend(f"{kind}: {count}" for kind, count in facts["operations"].items())
            lines.extend(
                f"message/{kind}: {count}" for kind, count in facts["message_deliveries"].items()
            )
            return self.screen(
                "\n".join(lines), [[self.button(self._t.t("admin.failed_operations"), "h_failed")]]
            )
        if cb.screen == "h_failed":
            facts = await self._service.failed(admin)
            return self.screen(
                "\n".join(
                    [self._t.t("admin.failed_operations")]
                    + [
                        f"{op} · {resource} · {kind} · {status} · {attempts}"
                        for op, resource, kind, status, attempts in facts
                    ]
                )
            )
        return None


class AdminMessagingUi(_Section):
    async def clear_prompt(self, admin):
        await self._service.clear(admin)
        await self._store.delete("admin-message-input", str(admin.id))

    async def handle(self, cb, admin):
        if cb.screen == "messages":
            await self._store.put(
                "admin-message-input", str(admin.id), {"step": "targets"}, ttl_seconds=self._ttl
            )
            return self.screen(self._t.t("admin.message_targets"))
        if cb.screen == "m_apply" and len(cb.args) == 1:
            identity = await self._service.apply(admin, cb.args[0])
            return self.screen(self._t.t("admin.message_queued", id=identity))
        return None

    async def handle_text(self, text, admin):
        record = await self._store.get("admin-message-input", str(admin.id))
        if record is None:
            return None
        if record["step"] == "targets":
            parts = text.replace(",", " ").split()
            if not 1 <= len(parts) <= 100 or any(
                not word.isascii() or not word.isdigit() or len(word) > 19 for word in parts
            ):
                return self.screen(self._t.t("admin.message_targets"))
            tids = tuple(int(word) for word in parts)
            if len(set(tids)) != len(tids) or any(not 0 < tid < 2**63 for tid in tids):
                return self.screen(self._t.t("admin.message_targets"))
            await self._store.put(
                "admin-message-input",
                str(admin.id),
                {"step": "body", "targets": tids},
                ttl_seconds=self._ttl,
            )
            return self.screen(self._t.t("admin.message_body"))
        if record["step"] == "body":
            if not 1 <= len(text.strip()) <= 3000:
                return self.screen(self._t.t("admin.message_body"))
            await self._store.put(
                "admin-message-input",
                str(admin.id),
                {**record, "step": "reason", "body": text.strip()},
                ttl_seconds=self._ttl,
            )
            return self.screen(self._t.t("admin.reason_prompt"))
        nonce = await self._service.prepare(admin, tuple(record["targets"]), record["body"], text)
        await self._store.delete("admin-message-input", str(admin.id))
        return self.screen(
            self._t.t(
                "admin.message_confirm",
                count=len(record["targets"]),
                body=record["body"],
                reason=text.strip(),
            ),
            [[self.button(self._t.t("admin.confirm_action"), "m_apply", nonce)]],
        )
