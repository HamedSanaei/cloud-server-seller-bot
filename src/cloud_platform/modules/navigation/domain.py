"""Telegram navigation domain (M08-001).

Design: the bot's screens form a small state machine that lives in the
domain layer (it must not import the Telegram framework). The UI layer maps
Telegram ``callback_query.data`` values through :func:`decode_callback` into
a :class:`Callback`, dispatches the action through
:func:`transition`, and renders the resulting screen.

Callback scheme (stable and tamper-resistant):

- ``<version>|<flow>:<screen>[:<arg>...]|<hmac-sha256[:16]>``
- The key is deterministic, so the same screen/args always produce the same
  callback string (stable deep links, safe for button caches).
- The truncated HMAC signature (keyed by a server secret) makes any
  modification of the flow, screen, or args detectable: a tampered or
  expired-format callback is rejected with :class:`CallbackError` and the
  bot shows a "link expired" notice instead of acting on it.
- Fields must match ``[A-Za-z0-9._-]+`` (UUIDs/hex ids and slugs); anything
  else is rejected.

Wire aliases (Telegram's 64-byte button limit):

- Application code always uses canonical screen names (``Callback.screen``).
  The codec maps long screens to compact wire aliases on encode and back on
  decode, so navigation semantics never change while ``callback_data`` stays
  within :data:`TELEGRAM_CALLBACK_DATA_LIMIT_BYTES`.
- The HMAC always covers the CANONICAL key, so an aliased callback and its
  literal pre-alias form verify identically: callbacks signed before an
  alias existed keep working, and no signature strength is lost.

Every screen supports **cancel** (back to the main menu, except the main
menu and the terminal done screen). Screens inside a flow also support
**back** (to their parent). Forward actions are declared explicitly per
screen, so an undeclared action raises
:class:`InvalidNavigationTransition` instead of silently navigating.
"""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass
from enum import StrEnum

CALLBACK_VERSION = "v1"
_CALLBACK_FIELD = re.compile(r"^[A-Za-z0-9._-]+$")
_SIGNATURE_LEN = 16  # truncated HMAC-SHA256 (64 bits) - ample for bot buttons

#: Hard byte limit of Telegram's ``InlineKeyboardButton.callback_data``.
TELEGRAM_CALLBACK_DATA_LIMIT_BYTES = 64

#: Canonical screen -> compact wire alias. Screen segments only: args are
#: never shortened, converted, or inferred here. Provider-neutral and
#: currency-neutral by construction.
_SCREEN_WIRE_ALIASES: dict[str, str] = {
    "product_locations": "pl",
}
_WIRE_SCREEN_CANONICAL: dict[str, str] = {
    wire: canonical for canonical, wire in _SCREEN_WIRE_ALIASES.items()
}

for _wire_alias in _SCREEN_WIRE_ALIASES.values():
    if not _CALLBACK_FIELD.fullmatch(_wire_alias):
        raise ValueError(f"invalid wire alias: {_wire_alias!r}")
if len(_WIRE_SCREEN_CANONICAL) != len(_SCREEN_WIRE_ALIASES):
    raise ValueError("wire aliases must be unique")


class CallbackError(ValueError):
    """Raised when a callback string cannot be decoded or fails verification."""


class UnknownFlowError(ValueError):
    """Raised when a flow name is not a known entry flow."""


class InvalidNavigationTransition(ValueError):
    """Raised for an action that is not allowed on the current screen."""


@dataclass(frozen=True, slots=True)
class NavScreen:
    """One screen of the bot: which flow it belongs to and its name."""

    flow: str
    name: str

    def __str__(self) -> str:
        return f"{self.flow}.{self.name}"


MAIN = NavScreen("main", "menu")
DONE = NavScreen("done", "completed")


class NavAction(StrEnum):
    """The fixed action vocabulary of the state machine."""

    BACK = "back"
    CANCEL = "cancel"
    SELECT = "select"
    OPEN = "open"
    CONFIRM = "confirm"
    HISTORY = "history"
    DELETE = "delete"
    POWER_ON = "power_on"
    POWER_OFF = "power_off"
    REBOOT = "reboot"


@dataclass(frozen=True, slots=True)
class Callback:
    """A decoded, verified callback: flow + screen + positional args."""

    flow: str
    screen: str
    args: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        """The deterministic signing base (also the human-readable id)."""
        parts = [self.flow, self.screen, *self.args]
        return ":".join(parts)

    def nav_screen(self) -> NavScreen:
        return NavScreen(self.flow, self.screen)


def _sign(key: str, signing_key: str) -> str:
    return hmac.new(signing_key.encode("utf-8"), key.encode("utf-8"), "sha256").hexdigest()[
        :_SIGNATURE_LEN
    ]


def encode_callback(callback: Callback, signing_key: str) -> str:
    """Encode a callback into its stable, signed wire form.

    Long canonical screens travel under their wire alias so the emitted
    ``callback_data`` fits Telegram's button limit; the signature still
    covers the canonical key, so aliased and literal forms verify
    identically. Use :func:`encode_telegram_callback` when the payload must
    be guaranteed to fit on a Telegram button.
    """
    if not signing_key:
        raise ValueError("signing_key must not be empty")
    for value in (callback.flow, callback.screen, *callback.args):
        if not value or not _CALLBACK_FIELD.fullmatch(value):
            raise CallbackError(f"invalid callback field: {value!r}")
    wire_screen = _SCREEN_WIRE_ALIASES.get(callback.screen, callback.screen)
    wire_key = ":".join((callback.flow, wire_screen, *callback.args))
    return f"{CALLBACK_VERSION}|{wire_key}|{_sign(callback.key, signing_key)}"


def ensure_telegram_callback_size(encoded: str) -> str:
    """Fail fast when generated ``callback_data`` exceeds Telegram's limit.

    The limit applies to newly generated button payloads only; decoding
    stays permissive so previously issued callbacks keep working.
    """
    size = len(encoded.encode("utf-8"))
    if size > TELEGRAM_CALLBACK_DATA_LIMIT_BYTES:
        raise CallbackError(
            "telegram callback_data exceeds "
            f"{TELEGRAM_CALLBACK_DATA_LIMIT_BYTES} bytes (got {size}): {encoded!r}"
        )
    return encoded


def encode_telegram_callback(callback: Callback, signing_key: str) -> str:
    """Encode a callback guaranteed to fit on a Telegram button.

    Raises :class:`CallbackError` when the wire form exceeds
    :data:`TELEGRAM_CALLBACK_DATA_LIMIT_BYTES` instead of emitting a
    button Telegram would reject.
    """
    return ensure_telegram_callback_size(encode_callback(callback, signing_key))


def decode_callback(data: str, signing_key: str) -> Callback:
    """Decode and verify a callback string; raises CallbackError on any fault.

    Wire aliases map back to their canonical screen, and the literal
    pre-alias form keeps decoding: the signature covers the canonical key
    either way, so both forms verify through the same path.
    """
    if not signing_key:
        raise ValueError("signing_key must not be empty")
    parts = data.split("|")
    if len(parts) != 3:
        raise CallbackError("malformed callback")
    version, wire_key, signature = parts
    if version != CALLBACK_VERSION:
        raise CallbackError("unsupported callback version")
    fields = wire_key.split(":")
    if len(fields) < 2:
        raise CallbackError("callback needs flow and screen")
    flow, wire_screen, *args = fields
    screen = _WIRE_SCREEN_CANONICAL.get(wire_screen, wire_screen)
    for field in (flow, screen, *args):
        if not field or not _CALLBACK_FIELD.fullmatch(field):
            raise CallbackError(f"invalid callback field: {field!r}")
    expected = _sign(":".join((flow, screen, *args)), signing_key)
    if not hmac.compare_digest(signature, expected):
        raise CallbackError("callback signature mismatch")
    return Callback(flow=flow, screen=screen, args=tuple(args))


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

#: Entry screen of each top-level flow (started from the main menu).
ENTRIES: dict[str, NavScreen] = {
    "buy": NavScreen("buy", "locations"),
    "servers": NavScreen("servers", "list"),
    "wallet": NavScreen("wallet", "balance"),
    "recharge": NavScreen("recharge", "amount"),
}

#: Explicit forward/back transitions per screen. Cancel is implicit (to
#: MAIN) for every screen except MAIN and DONE.
_TRANSITIONS: dict[NavScreen, dict[str, NavScreen]] = {
    # Purchase flow (M08-002..005)
    NavScreen("buy", "locations"): {"select": NavScreen("buy", "plans"), "back": MAIN},
    NavScreen("buy", "plans"): {
        "select": NavScreen("buy", "os"),
        "back": NavScreen("buy", "locations"),
    },
    NavScreen("buy", "os"): {
        "select": NavScreen("buy", "confirm"),
        "back": NavScreen("buy", "plans"),
    },
    NavScreen("buy", "confirm"): {
        "confirm": DONE,
        "back": NavScreen("buy", "os"),
    },
    # My servers (M08-007, M08-008, M08-009)
    NavScreen("servers", "list"): {"back": MAIN},
    NavScreen("servers", "detail"): {
        "back": NavScreen("servers", "list"),
        "delete": NavScreen("delete", "confirm"),
        "power_on": DONE,
        "power_off": DONE,
        "reboot": DONE,
    },
    # Delete confirmation (M08-009)
    NavScreen("delete", "confirm"): {
        "confirm": DONE,
        "back": NavScreen("servers", "detail"),
    },
    # Wallet (M08-010)
    NavScreen("wallet", "balance"): {
        "history": NavScreen("wallet", "history"),
        "back": MAIN,
    },
    NavScreen("wallet", "history"): {"back": NavScreen("wallet", "balance")},
    # Recharge (M08-011)
    NavScreen("recharge", "amount"): {"select": NavScreen("recharge", "method")},
    NavScreen("recharge", "method"): {
        "confirm": DONE,
        "back": NavScreen("recharge", "amount"),
    },
}


def start_flow(flow: str) -> NavScreen:
    """The entry screen of a top-level flow."""
    try:
        return ENTRIES[flow]
    except KeyError:
        raise UnknownFlowError(f"unknown flow: {flow!r}") from None


def transition(screen: NavScreen, action: str) -> NavScreen:
    """The screen reached by ``action`` from ``screen``.

    Rules:
    - ``cancel`` returns MAIN from any screen except MAIN and DONE.
    - ``back`` and forward actions must be declared for the screen.
    - MAIN and DONE have no outgoing transitions.
    """
    if screen is MAIN:
        raise InvalidNavigationTransition("the main menu has no transitions")
    if screen is DONE:
        raise InvalidNavigationTransition("the done screen is terminal")
    if action == NavAction.CANCEL:
        return MAIN
    table = _TRANSITIONS.get(screen)
    if table is None or action not in table:
        raise InvalidNavigationTransition(f"action {action!r} is not allowed on screen {screen}")
    return table[action]


def can_cancel(screen: NavScreen) -> bool:
    return screen not in (MAIN, DONE)


def can_back(screen: NavScreen) -> bool:
    table = _TRANSITIONS.get(screen)
    return table is not None and NavAction.BACK in table


def all_screens() -> tuple[NavScreen, ...]:
    """Every screen the machine knows (main, done, and all declared)."""
    seen: list[NavScreen] = [MAIN, DONE]
    for screen in _TRANSITIONS:
        if screen not in seen:
            seen.append(screen)
        for target in _TRANSITIONS[screen].values():
            if target not in seen:
                seen.append(target)
    return tuple(seen)
