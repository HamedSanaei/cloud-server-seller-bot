from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PaymentRequest:
    user_id: str
    amount: Decimal
    currency: str
    callback_url: str


@dataclass(frozen=True, slots=True)
class PaymentSession:
    external_id: str
    redirect_url: str


class PaymentGateway(Protocol):
    key: str

    async def create_payment(self, request: PaymentRequest) -> PaymentSession: ...
    async def verify_callback(self, payload: dict[str, str]) -> str: ...
