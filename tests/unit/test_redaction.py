"""Redaction tests proving secrets never reach logs (M1.39)."""

from __future__ import annotations

import io
import logging

from research_agent.config import Settings
from research_agent.main import _collect_secrets
from research_agent.observability.logging import (
    JsonFormatter,
    clear_context,
    configure_logging,
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
    # Assembled at runtime so no token-shaped literal exists in source:
    # GitHub secret scanning flags even Telegram's documented example token.
    bot_id = "123456789"  # noqa: S105
    token = bot_id + ":" + "AAHdqTcvCH1v" + "WGJxfSeofSAs0K5PALDsaw"  # noqa: S105
    assert token not in redact_text(f"bot {token}")


def test_redact_text_removes_payment_and_government_ids() -> None:
    card = "4111 1111 1111 1111"  # noqa: S105
    text = f"charge {card} ssn: 123-45-6789 passport=AB123456"
    redacted = redact_text(text)
    assert card not in redacted
    assert "123-45-6789" not in redacted
    assert "AB123456" not in redacted


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


def test_jina_reader_key_is_in_log_secrets_and_redacted() -> None:
    key = "jina-reader-test-secret-123"  # noqa: S105
    settings = Settings(_env_file=None, JINA_READER_API_KEY=key)
    assert key in _collect_secrets(settings)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("test.jina-redaction")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    set_secrets(_collect_secrets(settings))
    try:
        logger.info("reader failed with key %s", key)
        output = stream.getvalue()
    finally:
        logger.handlers.clear()
        clear_context()
    assert key not in output
    assert redact_mapping({"jina_reader_api_key": key})["jina_reader_api_key"] == "***"


def test_configure_logging_preserves_host_handlers() -> None:
    root = logging.getLogger()
    probe = logging.StreamHandler(io.StringIO())
    root.addHandler(probe)
    try:
        configure_logging()
        assert probe in root.handlers
        assert any(isinstance(handler.formatter, JsonFormatter) for handler in root.handlers)
    finally:
        root.removeHandler(probe)
