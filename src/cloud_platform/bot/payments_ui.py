"""Payment status Telegram UX (M09-006).

Pending/success/failure states are explicit and rendered from the message
catalog (Persian default): a pending screen with a gateway redirect button
+ a signed "check status" button, a success screen, a failure screen and a
still-pending nudge. No literals are scattered in handlers.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from cloud_platform.core.i18n import Translator
from cloud_platform.modules.payments.domain import PaymentSession, PaymentSessionStatus


@dataclass(frozen=True, slots=True)
class PaymentScreen:
    text: str
    keyboard: InlineKeyboardMarkup | None = None


def _check_keyboard(translator: Translator, check_callback: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=translator.t("payment.check"), callback_data=check_callback)]
        ]
    )


def _url_keyboard(
    translator: Translator, redirect_url: str, check_callback: str
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=translator.t("menu.recharge"), url=redirect_url)],
            [
                InlineKeyboardButton(
                    text=translator.t("payment.check"), callback_data=check_callback
                )
            ],
        ]
    )


def render_payment_status(
    session: PaymentSession,
    *,
    amount_text: str,
    check_callback: str = "pay:check",
    translator: Translator | None = None,
) -> PaymentScreen:
    """Render the Telegram screen for a payment session state."""
    t = translator or Translator()
    if session.status is PaymentSessionStatus.PENDING:
        return PaymentScreen(
            text=t.t("payment.pending", amount=amount_text),
            keyboard=_check_keyboard(t, check_callback),
        )
    if session.status is PaymentSessionStatus.SUCCEEDED:
        if session.credited_at is None:
            return PaymentScreen(
                text=t.t("payment.still_pending"),
                keyboard=_check_keyboard(t, check_callback),
            )
        return PaymentScreen(text=t.t("payment.success", amount=amount_text))
    return PaymentScreen(text=t.t("payment.failed"))


def render_payment_created(
    redirect_url: str,
    *,
    amount_text: str,
    check_callback: str = "pay:check",
    translator: Translator | None = None,
) -> PaymentScreen:
    """Render the screen right after gateway creation (pending + pay button)."""
    t = translator or Translator()
    return PaymentScreen(
        text=t.t("payment.pending", amount=amount_text),
        keyboard=_url_keyboard(t, redirect_url, check_callback),
    )
