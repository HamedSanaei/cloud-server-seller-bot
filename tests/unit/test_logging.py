"""Tests for structured logging and sensitive field redaction."""

from __future__ import annotations

import json
import logging
from io import StringIO

import structlog

from cloud_platform.core.logging import (
    _REDACTED_PLACEHOLDER,
    _redact_value,
    _should_redact_key,
    get_logger,
    redact_sensitive_fields,
    setup_logging,
)


def _make_test_logger() -> structlog.stdlib.BoundLogger:
    """Create a structlog logger that captures log entries for testing."""
    return structlog.get_logger()


def test_should_redact_key_for_authorization() -> None:
    assert _should_redact_key("authorization") is True


def test_should_redact_key_for_token() -> None:
    assert _should_redact_key("token") is True


def test_should_redact_key_for_password() -> None:
    assert _should_redact_key("password") is True


def test_should_redact_key_for_secret() -> None:
    assert _should_redact_key("secret") is True


def test_should_redact_key_for_api_key() -> None:
    assert _should_redact_key("api_key") is True


def test_should_redact_key_for_credential() -> None:
    assert _should_redact_key("credential") is True


def test_should_not_redact_key_for_user_id() -> None:
    assert _should_redact_key("user_id") is False


def test_should_not_redact_key_for_message() -> None:
    assert _should_redact_key("message") is False


def test_redact_value_replaces_token() -> None:
    result = _redact_value({"token": "abc123"})
    assert result["token"] == _REDACTED_PLACEHOLDER


def test_redact_value_replaces_password() -> None:
    result = _redact_value({"password": "mypassword"})
    assert result["password"] == _REDACTED_PLACEHOLDER


def test_redact_value_preserves_non_sensitive() -> None:
    result = _redact_value({"user_id": 123, "action": "login"})
    assert result["user_id"] == 123
    assert result["action"] == "login"


def test_redact_value_handles_nested_dicts() -> None:
    data = {
        "user": {"user_id": 123, "token": "secret123"},
        "metadata": {"action": "create"},
    }
    result = _redact_value(data)
    assert result["user"]["user_id"] == 123
    assert result["user"]["token"] == _REDACTED_PLACEHOLDER
    assert result["metadata"]["action"] == "create"


def test_redact_value_handles_lists() -> None:
    data = [{"token": "a"}, {"user_id": 1}]
    result = _redact_value(data)
    assert result[0]["token"] == _REDACTED_PLACEHOLDER
    assert result[1]["user_id"] == 1


def test_redact_sensitive_fields_processor() -> None:
    event_dict = {"token": "secret", "user_id": 123, "event": "test"}
    result = redact_sensitive_fields(None, None, event_dict)
    assert result["token"] == _REDACTED_PLACEHOLDER
    assert result["user_id"] == 123
    assert result["event"] == "test"


def test_bearer_token_redaction_in_string() -> None:
    value = "Bearer abc123def456"
    result = _redact_value(value)
    assert _REDACTED_PLACEHOLDER in str(result)
    assert "abc123def456" not in str(result)


def test_password_pattern_in_string() -> None:
    value = "password=supersecret"
    result = _redact_value(value)
    assert _REDACTED_PLACEHOLDER in str(result)
    assert "supersecret" not in str(result)


def test_json_logging_redacts_structured_secrets_and_sdk_payloads() -> None:
    output = StringIO()
    handler = logging.StreamHandler(output)
    root = logging.getLogger()
    root.addHandler(handler)
    previous_level = root.level
    try:
        # Configure the actual destination, not a handler added after startup.
        setup_logging()
        get_logger("test").info("payment_unavailable", token="fixture-secret")
        try:
            raise ValueError("fixture-private-message token=fixture-secret")
        except ValueError:
            logging.getLogger("aiogram.event").exception(
                "update 999 from user 123: fixture-private-message"
            )
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        assert len(events) == 2
        assert events[0]["token"] == _REDACTED_PLACEHOLDER
        assert events[1]["level"] == "error"
        assert events[1]["exception_type"] == "ValueError"
        assert events[1]["frames"][-1]["function"] == (
            "test_json_logging_redacts_structured_secrets_and_sdk_payloads"
        )
        assert "fixture-secret" not in output.getvalue()
        assert "fixture-private-message" not in output.getvalue()
        assert "999" not in events[1]["event"]
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


def test_redaction_in_nested_structures() -> None:
    data = {
        "event": "user_action",
        "user": {
            "id": 123,
            "credentials": {"token": "should_be_redacted", "password": "also_redacted"},
        },
        "safe_field": "this should remain",
    }
    result = _redact_value(data)
    assert result["safe_field"] == "this should remain"
    assert result["user"]["credentials"]["token"] == _REDACTED_PLACEHOLDER
    assert result["user"]["credentials"]["password"] == _REDACTED_PLACEHOLDER
    assert result["user"]["id"] == 123


def test_empty_dict_redaction() -> None:
    assert _redact_value({}) == {}


def test_list_of_strings_redaction() -> None:
    result = _redact_value(["password=mysecret", "user_id=42"])
    assert _REDACTED_PLACEHOLDER in str(result)
