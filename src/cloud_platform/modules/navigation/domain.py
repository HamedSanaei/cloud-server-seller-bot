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
    """Encode a callback into its stable, signed wire form."""
    if not signing_key:
        raise ValueError("signing_key must not be empty")
    for value in (callback.flow, callback.screen, *callback.args):
        if not value or not _CALLBACK_FIELD.fullmatch(value):
            raise CallbackError(f"invalid callback field: {value!r}")
    return f"{CALLBACK_VERSION}|{callback.key}|{_sign(callback.key, signing_key)}"


def decode_callback(data: str, signing_key: str) -> Callback:
    """Decode and verify a callback string; raises CallbackError on any fault."""
    if not signing_key:
        raise ValueError("signing_key must not be empty")
    parts = data.split("|")
    if len(parts) != 3:
        raise CallbackError("malformed callback")
    version, key, signature = parts
    if version != CALLBACK_VERSION:
        raise CallbackError("unsupported callback version")
    expected = _sign(key, signing_key)
    if not hmac.compare_digest(signature, expected):
        raise CallbackError("callback signature mismatch")
    fields = key.split(":")
    if len(fields) < 2:
        raise CallbackError("callback needs flow and screen")
    for field in fields:
        if not _CALLBACK_FIELD.fullmatch(field):
            raise CallbackError(f"invalid callback field: {field!r}")
    flow, screen, *args = fields
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
