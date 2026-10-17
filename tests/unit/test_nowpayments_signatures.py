"""Hermetic NOWPayments native-IPN signature verification (HMAC-SHA512).

These tests pin the canonicalization contract: object keys sorted recursively,
arrays order-preserving, JavaScript number formatting for signing, and
non-object/duplicate/non-finite/non-UTF-8 bodies rejected outright. The
verifier is a pure function: it proves the shared secret signed the payload and
returns data, and never credits anything from a payload field.
"""

from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal

import pytest

from cloud_platform.providers.nowpayments import (
    SignatureError,
    canonical_json,
    verify_ipn_signature,
)

SECRET = "ipn-fixture-secret-0001"  # pragma: allowlist secret
DUMMY_SIGNATURE = "0" * 128


def sign(raw_body: bytes | str, secret: str = SECRET) -> str:
    canonical = canonical_json(raw_body)
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha512).hexdigest()


def test_verify_ipn_signature_accepts_valid_nested_payload() -> None:
    raw = (
        b'{"payment_status":"finished","amount":1.10,"user_id":42,'
        b'"items":[3,1,2],"nested":{"b":2,"a":1}}'
    )
    signature = sign(raw)

    data = verify_ipn_signature(raw, signature, SECRET)

    assert data["payment_status"] == "finished"
    assert isinstance(data["amount"], Decimal)
    assert data["amount"] == Decimal("1.10")
    assert str(data["amount"]) == "1.10"
    assert data["user_id"] == 42
    assert data["items"] == [3, 1, 2]
    assert data["nested"] == {"a": 1, "b": 2}

    reordered = (
        '{"nested":{"a":1,"b":2},"items":[3,1,2],"user_id":42,'
        '"amount":1.10,"payment_status":"finished"}'
    )
    assert sign(reordered) == signature


def test_canonical_json_sorts_keys_recursively_and_is_compact() -> None:
    canonical = canonical_json('{"b":1,"a":{"d":2,"c":[3,{"z":1,"y":2}]}}')

    assert canonical == '{"a":{"c":[3,{"y":2,"z":1}],"d":2},"b":1}'
    assert ": " not in canonical
    assert ", " not in canonical


def test_canonical_json_normalizes_javascript_number_tokens() -> None:
    canonical = canonical_json('{"x":1.10,"y":1e3,"z":-0}')

    assert canonical == '{"x":1.1,"y":1000,"z":0}'


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"a":1.0}', '{"a":1}'),
        ('{"a":1e0}', '{"a":1}'),
        ('{"a":1.10}', '{"a":1.1}'),
        ('{"a":-0.0}', '{"a":0}'),
        ('{"a":1e-7}', '{"a":1e-7}'),
        ('{"a":1e-6}', '{"a":0.000001}'),
        ('{"a":1e21}', '{"a":1e+21}'),
        ('{"a":1e20}', '{"a":100000000000000000000}'),
        ('{"z":{"b":1.0,"a":"فارسی"},"a":[1e0,1.10]}', '{"a":[1,1.1],"z":{"a":"فارسی","b":1}}'),
        ('{"10":1,"2":2,"a":3}', '{"2":2,"10":1,"a":3}'),
    ],
)
def test_independent_javascript_canonical_vectors(raw: str, expected: str) -> None:
    # Expected wire bytes are literal Node JSON.stringify(sortObjectDeep()) vectors,
    # not produced by the implementation under test.
    signature = hmac.new(SECRET.encode(), expected.encode(), hashlib.sha512).hexdigest()
    assert canonical_json(raw) == expected
    verify_ipn_signature(raw, signature, SECRET)


@pytest.mark.parametrize("raw", ['{"a":"\\ud800"}', '{"\\udfff":1}'])
def test_malformed_unicode_is_a_signature_error(raw: str) -> None:
    with pytest.raises(SignatureError):
        canonical_json(raw)
    with pytest.raises(SignatureError):
        verify_ipn_signature(raw, DUMMY_SIGNATURE, SECRET)


def test_exact_decimal_materialization_is_separate_from_signing() -> None:
    raw = b'{"amount":1.10}'

    data = verify_ipn_signature(raw, sign(raw), SECRET)

    assert data == {"amount": Decimal("1.10")}


def test_signature_rejects_tampering_wrong_secret_and_malformed_signature() -> None:
    raw = b'{"a":1,"b":2}'
    signature = sign(raw)

    assert verify_ipn_signature(raw, signature, SECRET) == {"a": 1, "b": 2}

    with pytest.raises(SignatureError):
        verify_ipn_signature(b'{"a":1,"b":3}', signature, SECRET)
    with pytest.raises(SignatureError):
        verify_ipn_signature(raw, signature, "wrong-secret")
    with pytest.raises(SignatureError):
        verify_ipn_signature(raw, "abc", SECRET)
    with pytest.raises(SignatureError):
        verify_ipn_signature(raw, "z" * 128, SECRET)
    with pytest.raises(SignatureError):
        verify_ipn_signature(raw, "", SECRET)


def test_signature_comparison_accepts_uppercase_hex() -> None:
    raw = b'{"a":1}'

    assert verify_ipn_signature(raw, sign(raw).upper(), SECRET) == {"a": 1}


@pytest.mark.parametrize(
    "raw",
    ["[]", '"hi"', "42", "{}"],
    ids=["array", "string", "number", "empty"],
)
def test_non_object_or_empty_bodies_are_rejected(raw: str) -> None:
    with pytest.raises(SignatureError):
        canonical_json(raw)
    with pytest.raises(SignatureError):
        verify_ipn_signature(raw, DUMMY_SIGNATURE, SECRET)


@pytest.mark.parametrize(
    "raw",
    ['{"a":1,"a":2}', '{"a":{"b":1,"b":2}}'],
    ids=["top-level", "nested"],
)
def test_duplicate_keys_are_rejected(raw: str) -> None:
    with pytest.raises(SignatureError):
        canonical_json(raw)
    with pytest.raises(SignatureError):
        verify_ipn_signature(raw, DUMMY_SIGNATURE, SECRET)


@pytest.mark.parametrize(
    "raw",
    ['{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}'],
    ids=["nan", "infinity", "negative-infinity"],
)
def test_non_finite_numbers_are_rejected(raw: str) -> None:
    with pytest.raises(SignatureError):
        canonical_json(raw)
    with pytest.raises(SignatureError):
        verify_ipn_signature(raw, DUMMY_SIGNATURE, SECRET)


def test_invalid_utf8_body_is_rejected() -> None:
    raw = b'{"a":"\xff"}'

    with pytest.raises(SignatureError):
        canonical_json(raw)
    with pytest.raises(SignatureError):
        verify_ipn_signature(raw, DUMMY_SIGNATURE, SECRET)


def test_bytes_and_text_bodies_are_equivalent() -> None:
    text = '{"a":1,"b":[2,3]}'
    signature = sign(text)

    assert canonical_json(text) == canonical_json(text.encode("utf-8"))
    assert verify_ipn_signature(text, signature, SECRET) == verify_ipn_signature(
        text.encode("utf-8"), signature, SECRET
    )


def test_payload_fields_are_data_only_and_verify_returns_a_new_dict() -> None:
    raw = b'{"user_id":"attacker","amount":999999,"payment_status":"finished"}'
    signature = sign(raw)

    first = verify_ipn_signature(raw, signature, SECRET)
    second = verify_ipn_signature(raw, signature, SECRET)

    assert first == {
        "user_id": "attacker",
        "amount": 999999,
        "payment_status": "finished",
    }
    assert first == second
    assert first is not second


def test_signature_error_is_a_value_error() -> None:
    assert issubclass(SignatureError, ValueError)
