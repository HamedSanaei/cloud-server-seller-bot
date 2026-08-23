"""Tests for the Telegram navigation state machine (M08-001).

Acceptance: flows have stable callbacks and cancel/back behavior.
"""

from __future__ import annotations

import pytest

from cloud_platform.modules.navigation.domain import (
    CALLBACK_VERSION,
    DONE,
    ENTRIES,
    MAIN,
    Callback,
    CallbackError,
    InvalidNavigationTransition,
    NavAction,
    NavScreen,
    UnknownFlowError,
    all_screens,
    can_back,
    can_cancel,
    decode_callback,
    encode_callback,
    start_flow,
    transition,
)

KEY = "test-signing-key"


class TestStableCallbacks:
    async def test_roundtrip(self) -> None:
        cb = Callback(flow="buy", screen="plans", args=("fsn1",))
        assert decode_callback(encode_callback(cb, KEY), KEY) == cb

    async def test_no_args_roundtrip(self) -> None:
        cb = Callback(flow="servers", screen="list")
        assert decode_callback(encode_callback(cb, KEY), KEY) == cb

    async def test_stable_across_encodes(self) -> None:
        cb = Callback(flow="buy", screen="os", args=("img-12", "fsn1"))
        assert encode_callback(cb, KEY) == encode_callback(cb, KEY)

    async def test_different_callbacks_differ(self) -> None:
        a = encode_callback(Callback("buy", "plans", ("fsn1",)), KEY)
        b = encode_callback(Callback("buy", "plans", ("hel1",)), KEY)
        assert a != b

    async def test_wire_form_has_version_and_signature(self) -> None:
        data = encode_callback(Callback("buy", "plans", ("fsn1",)), KEY)
        version, key, signature = data.split("|")
        assert version == CALLBACK_VERSION
        assert key == "buy:plans:fsn1"
        assert len(signature) == 16


class TestTamperResistance:
    async def test_tampered_flow_rejected(self) -> None:
        data = encode_callback(Callback("buy", "plans", ("fsn1",)), KEY)
        version, key, sig = data.split("|")
        _, screen, arg = key.split(":")
        forged = f"{version}|servers:{screen}:{arg}|{sig}"
        with pytest.raises(CallbackError, match="signature mismatch"):
            decode_callback(forged, KEY)

    async def test_tampered_arg_rejected(self) -> None:
        data = encode_callback(Callback("buy", "plans", ("fsn1",)), KEY)
        version, key, sig = data.split("|")
        flow, screen = key.split(":")[:2]
        forged = f"{version}|{flow}:{screen}:hel1|{sig}"
        with pytest.raises(CallbackError, match="signature mismatch"):
            decode_callback(forged, KEY)

    async def test_tampered_signature_rejected(self) -> None:
        data = encode_callback(Callback("buy", "plans", ("fsn1",)), KEY)
        version, key, sig = data.split("|")
        forged_sig = ("0" if sig[0] != "0" else "1") + sig[1:]
        with pytest.raises(CallbackError, match="signature mismatch"):
            decode_callback(f"{version}|{key}|{forged_sig}", KEY)

    async def test_wrong_signing_key_rejected(self) -> None:
        data = encode_callback(Callback("buy", "plans", ("fsn1",)), KEY)
        with pytest.raises(CallbackError, match="signature mismatch"):
            decode_callback(data, "other-key")

    async def test_wrong_version_rejected(self) -> None:
        data = encode_callback(Callback("buy", "plans", ("fsn1",)), KEY)
        _version, key, sig = data.split("|")
        with pytest.raises(CallbackError, match="unsupported callback version"):
            decode_callback(f"v0|{key}|{sig}", KEY)

    @pytest.mark.parametrize(
        "data",
        [
            "",
            "just-a-string",
            "v1|buy:plans",
            "v1|buy:plans|sig|extra",
            "v1|:plans:fsn1|" + "0" * 16,
            "v1|buy:|fsn1|" + "0" * 16,
            "v1|buy:plans:bad value|" + "0" * 16,
            "v1|buy:plans:fsn1|short",
        ],
    )
    async def test_malformed_rejected(self, data: str) -> None:
        with pytest.raises(CallbackError):
            decode_callback(data, KEY)

    async def test_empty_signing_key_rejected(self) -> None:
        with pytest.raises(ValueError, match="signing_key"):
            encode_callback(Callback("buy", "plans"), "")
        with pytest.raises(ValueError, match="signing_key"):
            decode_callback("v1|buy:plans|abc", "")

    async def test_invalid_field_on_encode_rejected(self) -> None:
        with pytest.raises(CallbackError, match="invalid callback field"):
            encode_callback(Callback("buy", "plans", ("fsn 1")), KEY)


class TestFlowStart:
    async def test_every_flow_starts_at_its_entry(self) -> None:
        assert start_flow("buy") == NavScreen("buy", "locations")
        assert start_flow("servers") == NavScreen("servers", "list")
        assert start_flow("wallet") == NavScreen("wallet", "balance")
        assert start_flow("recharge") == NavScreen("recharge", "amount")

    async def test_unknown_flow_rejected(self) -> None:
        with pytest.raises(UnknownFlowError):
            start_flow("nope")


class TestPurchaseFlow:
    async def test_forward_path(self) -> None:
        screen = start_flow("buy")
        screen = transition(screen, NavAction.SELECT)
        assert screen == NavScreen("buy", "plans")
        screen = transition(screen, NavAction.SELECT)
        assert screen == NavScreen("buy", "os")
        screen = transition(screen, NavAction.SELECT)
        assert screen == NavScreen("buy", "confirm")
        screen = transition(screen, NavAction.CONFIRM)
        assert screen is DONE

    async def test_back_at_each_step(self) -> None:
        assert transition(NavScreen("buy", "plans"), NavAction.BACK) == NavScreen(
            "buy", "locations"
        )
        assert transition(NavScreen("buy", "os"), NavAction.BACK) == NavScreen("buy", "plans")
        assert transition(NavScreen("buy", "confirm"), NavAction.BACK) == NavScreen("buy", "os")

    async def test_top_level_back_goes_to_main(self) -> None:
        assert transition(NavScreen("buy", "locations"), NavAction.BACK) is MAIN


class TestCancelBehavior:
    async def test_cancel_from_every_screen_reaches_main(self) -> None:
        for screen in all_screens():
            if screen in (MAIN, DONE):
                continue
            assert can_cancel(screen), screen
            assert transition(screen, NavAction.CANCEL) is MAIN, screen

    async def test_cancel_from_main_and_done_rejected(self) -> None:
        with pytest.raises(InvalidNavigationTransition):
            transition(MAIN, NavAction.CANCEL)
        with pytest.raises(InvalidNavigationTransition):
            transition(DONE, NavAction.CANCEL)


class TestBackBehavior:
    async def test_every_non_entry_screen_has_back(self) -> None:
        entries = set(ENTRIES.values())
        for screen in all_screens():
            if screen in (MAIN, DONE) or screen in entries:
                continue
            assert can_back(screen), f"screen {screen} lacks back"
            back = transition(screen, NavAction.BACK)
            assert back is not screen

    async def test_entry_screens_can_back_to_main(self) -> None:
        for entry in ENTRIES.values():
            if can_back(entry):
                assert transition(entry, NavAction.BACK) is MAIN


class TestUndeclaredActions:
    @pytest.mark.parametrize(
        "screen,action",
        [
            (MAIN, NavAction.BACK),
            (DONE, NavAction.BACK),
            (DONE, NavAction.SELECT),
            (NavScreen("buy", "locations"), NavAction.CONFIRM),
            (NavScreen("buy", "confirm"), NavAction.SELECT),
            (NavScreen("servers", "list"), NavAction.OPEN),
            (NavScreen("wallet", "balance"), NavAction.DELETE),
        ],
    )
    async def test_rejected(self, screen: NavScreen, action: str) -> None:
        with pytest.raises(InvalidNavigationTransition):
            transition(screen, action)


class TestServerFlow:
    async def test_detail_power_and_delete_paths(self) -> None:
        detail = NavScreen("servers", "detail")
        assert transition(detail, NavAction.POWER_ON) is DONE
        assert transition(detail, NavAction.POWER_OFF) is DONE
        assert transition(detail, NavAction.REBOOT) is DONE
        confirm = transition(detail, NavAction.DELETE)
        assert confirm == NavScreen("delete", "confirm")
        assert transition(confirm, NavAction.CONFIRM) is DONE
        assert transition(confirm, NavAction.BACK) == detail

    async def test_wallet_history_roundtrip(self) -> None:
        balance = NavScreen("wallet", "balance")
        history = transition(balance, NavAction.HISTORY)
        assert history == NavScreen("wallet", "history")
        assert transition(history, NavAction.BACK) == balance

    async def test_recharge_flow(self) -> None:
        amount = start_flow("recharge")
        method = transition(amount, NavAction.SELECT)
        assert method == NavScreen("recharge", "method")
        assert transition(method, NavAction.CONFIRM) is DONE
        assert transition(method, NavAction.BACK) == amount


class TestCallbackNavIntegration:
    async def test_decode_yields_a_navigable_screen(self) -> None:
        data = encode_callback(Callback(flow="buy", screen="plans", args=("fsn1",)), KEY)
        cb = decode_callback(data, KEY)
        screen = cb.nav_screen()
        assert transition(screen, NavAction.SELECT) == NavScreen("buy", "os")
        assert cb.args == ("fsn1",)
