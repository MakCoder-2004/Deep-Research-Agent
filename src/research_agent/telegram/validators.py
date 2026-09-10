"""Input validators for Telegram text and URL queries (EN/AR preserved verbatim)."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

MAX_URL_CHARS = 2000


def classify_input(text: str) -> Literal["text", "url"]:
    """Classify stripped input as a single URL or plain text.

    Returns ``"url"`` only when the entire stripped input is a single
    ``http://`` or ``https://`` token (no whitespace). Everything else,
    including Arabic, mixed-language, and multi-word queries, is ``"text"``.
    Surrounding whitespace is ignored; inner content (including Arabic RTL)
    is preserved verbatim by callers.
    """
    stripped = text.strip()
    if not stripped:
        return "text"
    if " " in stripped or "\n" in stripped or "\t" in stripped or "\r" in stripped:
        return "text"
    lower = stripped.lower()
    if lower.startswith("http://") or lower.startswith("https://"):
        return "url"
    return "text"


def is_accepted_url(value: str) -> bool:
    """Return True for acceptable research URLs (http/https only).

    Rejects empty values, overlong values, non-http(s) schemes
    (javascript/file/ftp/data/...), embedded credentials, and missing hosts.
    Preserves Arabic/IDN content verbatim (no normalization beyond stripping).
    """
    stripped = value.strip()
    if not stripped or len(stripped) > MAX_URL_CHARS:
        return False
    if any(ch.isspace() for ch in stripped):
        return False
    lower = stripped.lower()
    # Explicitly reject dangerous/unsupported schemes even without urlsplit.
    if lower.startswith(("javascript:", "file:", "ftp:", "data:", "vbscript:")):
        return False
    try:
        parts = urlsplit(stripped)
    except ValueError:
        return False
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return False
    # Reject embedded credentials (user:pass@host).
    if parts.username or parts.password:
        return False
    if "@" in parts.netloc and (parts.username is None):
        # Covers malformed userinfo that urlsplit did not parse as username.
        return False
    if not parts.hostname:
        return False
    return True
