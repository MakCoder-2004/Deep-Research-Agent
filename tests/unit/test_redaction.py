"""Redaction tests proving secrets never reach logs (M1.39)."""

from __future__ import annotations

import io
import logging

from research_agent.observability.logging import (
    JsonFormatter,
    clear_context,
    set_job_id,
    set_secrets,
)
from research_agent.observability.redaction import redact_mapping, redact_text


def test_redact_text_removes_explicit_and_pattern_secrets() -> None:
    secret = "my-very-secret-value-999"  # noqa: S105
    text = f"token is {secret} contact a@b.com Bearer abc.def.ghi sk-1234567890abcdef"
    redacted = redact_text(text, [secret])
    assert secret not in redacted
    assert "a@b.com" not in redacted
    assert "***" in redacted


def test_redact_text_removes_telegram_token() -> None:
    token = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"  # noqa: S105
    assert token not in redact_text(f"bot {token}")


def test_redact_mapping_hides_sensitive_keys() -> None:
    data = {"authorization": "Bearer xyz", "nested": {"api_key": "abc"}, "safe": "hello"}
    redacted = redact_mapping(data, ["xyz", "abc"])
    assert redacted["authorization"] == "***"
    assert redacted["nested"] == {"api_key": "***"}
    assert redacted["safe"] == "hello"
    assert "xyz" not in str(redacted)


def test_json_formatter_redacts_log_output() -> None:
    secret = "secret-bot-token-abc-123"  # noqa: S105
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("test.redaction")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    set_job_id("job-1")
    set_secrets([secret])
    try:
        logger.info("starting with %s", secret)
        output = stream.getvalue()
    finally:
        logger.handlers.clear()
        clear_context()
    assert secret not in output
    assert "job-1" in output
