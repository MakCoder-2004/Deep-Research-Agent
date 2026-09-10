"""Secret and PII redaction for logs and traces."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

_TELEGRAM_TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/=]+")
_API_KEY_RE = re.compile(r"(?i)(api[_-]?key|secret|token)\s*[:=]\s*['\"]?([^'\"\s,}]+)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{7,}\d")
_LONG_SECRET_RE = re.compile(r"sk-[A-Za-z0-9\-_]{8,}")
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_ID_DOC_RE = re.compile(
    r"(?i)(ssn|social.?security|national.?id|id.?number|passport(?:.?no)?|civil.?id)"
    r"\s*[:=]\s*['\"]?([^'\"\s,}]+)"
)


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Redact explicit secrets plus common secret/PII patterns."""
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "***")
    redacted = _TELEGRAM_TOKEN_RE.sub("***", redacted)
    redacted = _BEARER_RE.sub("Bearer ***", redacted)
    redacted = _LONG_SECRET_RE.sub("***", redacted)
    redacted = _API_KEY_RE.sub(r"\1=***", redacted)
    redacted = _ID_DOC_RE.sub(r"\1=***", redacted)
    redacted = _CARD_RE.sub("***", redacted)
    redacted = _EMAIL_RE.sub("***@***", redacted)
    redacted = _PHONE_RE.sub("***", redacted)
    return redacted


def redact_mapping(data: dict[str, Any], secrets: Iterable[str] = ()) -> dict[str, Any]:
    """Return a copy of a mapping with secret values redacted."""
    secret_list = [secret for secret in secrets if secret]
    redacted: dict[str, Any] = {}
    sensitive_keys = {
        "authorization",
        "api_key",
        "apikey",
        "token",
        "secret",
        "cookie",
        "password",
        "card",
        "card_number",
        "ssn",
        "passport",
        "national_id",
    }
    for key, value in data.items():
        if key.lower() in sensitive_keys:
            redacted[key] = "***"
        elif isinstance(value, str):
            redacted[key] = redact_text(value, secret_list)
        elif isinstance(value, dict):
            redacted[key] = redact_mapping(value, secret_list)
        elif isinstance(value, list):
            redacted[key] = [
                redact_mapping(item, secret_list)
                if isinstance(item, dict)
                else redact_text(item, secret_list)
                if isinstance(item, str)
                else item
                for item in value
            ]
        else:
            redacted[key] = value
    return redacted
