"""Secret and PII redaction for logs and traces."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

_TELEGRAM_TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}|%3A[A-Za-z0-9_-]{30,}")
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/=]+")
_URL_CREDS_RE = re.compile(r"(?i)(https?://[^/\s:@]+:)[^/\s@]+@")
_API_KEY_RE = re.compile(r"(?i)(api[_-]?key|secret|token)\s*['\"]?\s*[:=]\s*['\"]?([^'\"\s,}]+)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{7,}\d")
_LONG_SECRET_RE = re.compile(
    r"(?:sk-|gsk_|tvly-|BSA[A-Za-z0-9]{8,}|lsv2_[A-Za-z0-9_-]{8,}|xox[bap]-[A-Za-z0-9-]+)"
    r"[A-Za-z0-9\-_]{4,}"
)
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_ID_DOC_RE = re.compile(
    r"(?i)(ssn|social.?security|national.?id|id.?number|passport(?:.?no)?|civil.?id|"
    r"driver.?licen[sc]e|iqama|\u0631\u0642\u0645.?(\u0627\u0644\u0647\u0648\u064a\u0629|\u0627\u0644\u062c\u0648\u0627\u0632|\u0627\u0644\u0648\u0637\u0646\u064a))"
    r"\s*['\"]?\s*[:=]\s*['\"]?([^'\"\s,}]+)"
)
_COOKIE_RE = re.compile(
    r"""(?i)["']?(cookie|set-cookie|authorization|x-api-key)["']?\s*[:=]\s*("[^"]*"|'[^']*'|[^\s;,\n]+)"""
)


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Redact explicit secrets plus common secret/PII patterns."""
    redacted = text
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        redacted = redacted.replace(secret, "***")
    redacted = _TELEGRAM_TOKEN_RE.sub("***", redacted)
    redacted = _BEARER_RE.sub("Bearer ***", redacted)
    redacted = _URL_CREDS_RE.sub(r"\1***@", redacted)
    redacted = _LONG_SECRET_RE.sub("***", redacted)
    redacted = _API_KEY_RE.sub(r"\1=***", redacted)
    redacted = _COOKIE_RE.sub(r"\1: ***", redacted)
    redacted = _ID_DOC_RE.sub(r"\1=***", redacted)
    redacted = _CARD_RE.sub("***", redacted)
    redacted = _EMAIL_RE.sub("***@***", redacted)
    redacted = _PHONE_RE.sub("***", redacted)
    return redacted


def _redact_any(item: Any, secrets: list[str]) -> Any:
    """Recursively redact nested collections, preserving container types."""
    if isinstance(item, dict):
        return redact_mapping(item, secrets)
    if isinstance(item, str):
        return redact_text(item, secrets)
    if isinstance(item, bytes):
        try:
            return redact_text(item.decode("utf-8", "ignore"), secrets).encode("utf-8")
        except Exception:  # noqa: S110, BLE001 - never leak undecodable bytes
            return b"***"
    if isinstance(item, list):
        return [_redact_any(entry, secrets) for entry in item]
    if isinstance(item, tuple):
        return tuple(_redact_any(entry, secrets) for entry in item)
    if isinstance(item, set):
        return {_redact_any(entry, secrets) for entry in item}
    return item


def redact_mapping(data: dict[str, Any], secrets: Iterable[str] = ()) -> dict[str, Any]:
    """Return a copy of a mapping with secret values redacted."""
    secret_list = [secret for secret in secrets if secret]
    redacted: dict[str, Any] = {}
    sensitive_keys = {
        "authorization",
        "proxy-authorization",
        "api_key",
        "api-key",
        "x-api-key",
        "apikey",
        "token",
        "bot_token",
        "telegram_bot_token",
        "groq_api_key",
        "openrouter_api_key",
        "cloudflare_api_key",
        "tavily_api_key",
        "brave_api_key",
        "exa_api_key",
        "serpapi_api_key",
        "github_token",
        "stackexchange_api_key",
        "semantic_scholar_api_key",
        "ncbi_api_key",
        "langsmith_api_key",
        "access_token",
        "refresh_token",
        "client_secret",
        "api_secret",
        "secret",
        "cookie",
        "set-cookie",
        "password",
        "passwd",
        "pwd",
        "card",
        "card_number",
        "ssn",
        "passport",
        "national_id",
    }
    for key, value in data.items():
        normalized = key.lower().replace("_", "-")
        if key.lower() in sensitive_keys or normalized in sensitive_keys:
            redacted[key] = "***"
        else:
            redacted[key] = _redact_any(value, secret_list)
    return redacted
