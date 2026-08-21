from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


@dataclass(frozen=True, slots=True)
class Money:
    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        if not self.currency or len(self.currency) != 3:
            raise ValueError("currency must be an ISO-like 3-letter code")
        object.__setattr__(self, "amount", Decimal(self.amount))
        object.__setattr__(self, "currency", self.currency.upper())

    def quantized(self, exponent: str = "0.000001") -> "Money":
        return Money(self.amount.quantize(Decimal(exponent), rounding=ROUND_HALF_UP), self.currency)

    def __add__(self, other: "Money") -> "Money":
        self._assert_currency(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._assert_currency(other)
        return Money(self.amount - other.amount, self.currency)

    def _assert_currency(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise ValueError("currency mismatch")
