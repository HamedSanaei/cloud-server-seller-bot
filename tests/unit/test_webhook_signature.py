"""Tests for gateway webhook HMAC signature helpers."""

from __future__ import annotations

import pytest

from cloud_platform.core.webhooks import sign_gateway_payload, verify_gateway_signature

SECRET = "test-gateway-secret"
BODY = b'{"gateway_payment_id": "ext-1", "status": "succeeded"}'


class TestSignVerify:
    def test_roundtrip(self) -> None:
        sig = sign_gateway_payload(SECRET, BODY)
        assert verify_gateway_signature(SECRET, BODY, sig) is True

    def test_wrong_secret_fails(self) -> None:
        sig = sign_gateway_payload(SECRET, BODY)
        assert verify_gateway_signature("other-secret", BODY, sig) is False

    def test_tampered_body_fails(self) -> None:
        sig = sign_gateway_payload(SECRET, BODY)
        tampered = BODY.replace(b"succeeded", b"failed")
        assert verify_gateway_signature(SECRET, tampered, sig) is False

    def test_case_insensitive_signature(self) -> None:
        sig = sign_gateway_payload(SECRET, BODY)
        assert verify_gateway_signature(SECRET, BODY, sig.upper()) is True

    def test_signature_with_whitespace_accepted(self) -> None:
        sig = sign_gateway_payload(SECRET, BODY)
        assert verify_gateway_signature(SECRET, BODY, f"  {sig}\n") is True


class TestFailureModes:
    def test_empty_secret_fails(self) -> None:
        assert verify_gateway_signature("", BODY, "abc") is False

    def test_empty_signature_fails(self) -> None:
        assert verify_gateway_signature(SECRET, BODY, "") is False

    def test_non_ascii_signature_fails_without_error(self) -> None:
        assert verify_gateway_signature(SECRET, BODY, "s\xfff") is False

    def test_sign_requires_non_empty_secret(self) -> None:
        with pytest.raises(ValueError, match="secret"):
            sign_gateway_payload("", BODY)
