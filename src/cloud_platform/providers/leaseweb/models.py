"""Shared Leaseweb DTO primitives (LEASEWEB-VPS-API §8).

Small, provider-scoped building blocks used by every Leaseweb API family:

- :class:`LeasewebModel` — the DTO base: camelCase aliases, tolerant of
  unknown provider fields (``extra="allow"``) so a Leaseweb schema addition
  never breaks a read, while core *domain* models stay strict;
- :class:`LeasewebRequestModel` — the request base: extra fields are
  FORBIDDEN, because a typo in a request body must fail loudly instead of
  being sent to the provider;
- :class:`OpenStrEnum` — a documented closed value set that still accepts
  unknown future provider values verbatim (never misclassifying a state we
  do not know yet);
- :class:`PaginationMetadata` / :class:`LeasewebPage` — the reusable
  pagination envelope (``_metadata``) with a bounded page iterator;
- timestamp and money helpers.

Money rule: provider prices are represented as :class:`decimal.Decimal`
parsed from the *text* form of the provider number (never binary float
arithmetic). Conversion to integer minor units is the caller's explicit
step (see :func:`~cloud_platform.providers.leaseweb.ordering_api.to_minor_units`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum, StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, SecretStr
from pydantic.alias_generators import to_camel

__all__ = [
    "LeasewebModel",
    "LeasewebPage",
    "LeasewebRequestModel",
    "Money",
    "OpenStrEnum",
    "PaginationMetadata",
    "collect_pages",
    "paginate",
    "parse_leaseweb_datetime",
    "to_decimal",
    "to_minor_units",
]


class OpenStrEnum(StrEnum):
    """A documented value set that tolerates unknown future values.

    Subclasses declare the values the local Leaseweb documentation defines.
    A value the provider adds later is preserved verbatim as a pseudo-member
    instead of raising, so the platform never misclassifies a state it does
    not know yet (and never silently drops it).
    """

    @classmethod
    def _missing_(cls, value: object) -> OpenStrEnum | None:
        if isinstance(value, str) and value:
            member = str.__new__(cls, value)
            member._name_ = value
            member._value_ = value
            return member
        return None

    @property
    def is_documented(self) -> bool:
        """Whether this value is part of the documented closed set."""
        return self.value in {member.value for member in type(self)}


def to_decimal(value: Any) -> Any:
    """Parse a provider numeric into :class:`Decimal` (via its text form).

    Accepts int/float/str/Decimal. Returns the value unchanged when it is
    already a Decimal; raises ``ValueError`` for unusable input so pydantic
    reports a precise validation error instead of silently producing 0.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError("boolean is not a valid decimal amount")
    try:
        # str() first: Decimal(0.1) would inherit the binary-float error.
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:  # pragma: no cover - defensive
        raise ValueError(f"not a parseable decimal amount: {value!r}") from exc


#: A monetary/provider-numeric value parsed without binary-float arithmetic.
Money = Annotated[Decimal, BeforeValidator(to_decimal)]

#: Number of minor units in one major unit (EUR/USD cents).
MINOR_UNITS_PER_MAJOR = Decimal(100)


def to_minor_units(value: Decimal) -> int:
    """Decimal major units -> integer minor units, half-up.

    The explicit, auditable boundary between provider prices (major units)
    and the platform's integer minor-unit money model. Never float.
    """
    return int((value * MINOR_UNITS_PER_MAJOR).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_leaseweb_datetime(value: str | None) -> datetime | None:
    """Parse a Leaseweb timestamp into an aware UTC datetime.

    Provider timestamps are kept as raw strings on the DTOs (so nothing is
    silently normalized), and this helper provides a best-effort parse for
    application logic. ``None`` for absent/unparseable values; naive values
    are assumed UTC.
    """
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class LeasewebModel(BaseModel):
    """Read-model base: camelCase aliases, forward-compatible extras.

    ``extra="allow"`` keeps any provider field the DTO does not model yet
    (reachable through ``model_extra``), so a Leaseweb schema addition never
    breaks a read.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="allow",
        frozen=True,
        str_strip_whitespace=False,
    )


class LeasewebRequestModel(BaseModel):
    """Request-body base: camelCase aliases, unknown fields rejected.

    ``extra="forbid"`` makes a misspelled request field fail loudly instead
    of being sent to Leaseweb. :meth:`body` is the ONLY sanctioned way to
    turn a request model into wire JSON (it is also what reveals a
    ``SecretStr`` field into the request body — never log its result).
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )

    def body(self) -> dict[str, Any]:
        """Serialize for transport: camelCase, omit unset optionals.

        ``SecretStr`` fields are unwrapped to their real value here (this is
        the ONLY place a request secret is revealed — pydantic's JSON mode
        would serialize ``**********`` and silently send a redacted password
        to Leaseweb). The result may contain secret request fields; it must
        never be logged, persisted or asserted on in a snapshot.
        """
        dumped = self.model_dump(by_alias=True, exclude_none=True)
        return {key: _wire_value(value) for key, value in dumped.items()}


def _wire_value(value: Any) -> Any:
    """Recursively convert a dumped request value into wire JSON.

    Money rule: a ``Decimal`` in a request body is converted to an integer
    when it is integral and is otherwise REJECTED — the platform never
    encodes a billable amount as a binary float.
    """
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, BaseModel):
        return _wire_value(value.model_dump(by_alias=True, exclude_none=True))
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        raise ValueError(
            "request bodies must not carry fractional Decimal amounts; convert "
            "to integer minor units first"
        )
    if isinstance(value, Mapping):
        return {key: _wire_value(item) for key, item in value.items()}
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list | tuple | set):
        return [_wire_value(item) for item in value]
    return value


class PaginationMetadata(LeasewebModel):
    """The ``_metadata`` envelope Leaseweb returns for list endpoints.

    The three fields are documented as required for collections
    (``totalCount``, ``offset``, ``limit``); the model still tolerates a
    provider that omits them by falling back to page-derived values.
    """

    total_count: int = 0
    offset: int = 0
    limit: int = 0

    def has_more(self, *, page_size: int | None = None) -> bool:
        """Whether more rows exist after the page this metadata describes."""
        consumed = self.offset + (page_size if page_size is not None else self.limit)
        return consumed < self.total_count


class LeasewebPage[T: BaseModel](LeasewebModel):
    """One typed page of a Leaseweb collection."""

    items: list[T] = Field(default_factory=list)
    metadata: PaginationMetadata | None = None

    @property
    def total_count(self) -> int:
        return self.metadata.total_count if self.metadata else len(self.items)

    @property
    def has_more(self) -> bool:
        if self.metadata is None:
            return False
        # Trust the provider's own metadata; fall back to the page size only
        # when the provider did not report a limit.
        return self.metadata.has_more(page_size=self.metadata.limit or len(self.items))


#: A coroutine-returning page fetcher, as used by :func:`paginate`.
type PageFetcher[T: BaseModel] = Callable[[int, int], Coroutine[Any, Any, LeasewebPage[T]]]


async def paginate[T: BaseModel](
    fetch: PageFetcher[T],
    *,
    page_size: int = 100,
    start_offset: int = 0,
    max_pages: int = 50,
) -> AsyncIterator[T]:
    """Iterate every row of a paginated Leaseweb collection.

    Reusable across families; bounded by ``max_pages`` so a provider that
    keeps reporting more rows cannot spin forever. Stops when the provider
    reports no further rows or returns an empty page.
    """
    offset = start_offset
    for _ in range(max_pages):
        page = await fetch(page_size, offset)
        if not page.items:
            return
        for item in page.items:
            yield item
        offset += len(page.items)
        if not page.has_more:
            return


async def collect_pages[T: BaseModel](
    fetch: PageFetcher[T],
    *,
    page_size: int = 100,
    start_offset: int = 0,
    max_pages: int = 50,
) -> list[T]:
    """Materialize :func:`paginate` into a list."""
    return [
        item
        async for item in paginate(
            fetch, page_size=page_size, start_offset=start_offset, max_pages=max_pages
        )
    ]
