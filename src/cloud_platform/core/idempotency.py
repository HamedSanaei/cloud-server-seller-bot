from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IdempotencyKey:
    value: str

    def __post_init__(self) -> None:
        value = self.value.strip()
        if len(value) < 8 or len(value) > 128:
            raise ValueError("idempotency key must be 8..128 chars")
        object.__setattr__(self, "value", value)
