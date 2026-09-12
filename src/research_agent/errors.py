"""Shared error taxonomy for providers and tools (M1.26)."""

from __future__ import annotations

from enum import StrEnum


class ErrorCategory(StrEnum):
    TRANSIENT = "transient"
    RATE_LIMITED = "rate_limited"
    AUTH = "auth"
    INVALID_REQUEST = "invalid_request"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    CONTENT_FILTERED = "content_filtered"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"
    SSRF = "ssrf"
    DNS = "dns"
    REDIRECT = "redirect"
    SIZE = "size"
    MIME = "mime"
    ROBOTS = "robots"
    EXTRACTION = "extraction"
    HTTP = "http"


_RETRYABLE = frozenset({ErrorCategory.TRANSIENT, ErrorCategory.TIMEOUT, ErrorCategory.UNAVAILABLE})
_COOLDOWN_ELIGIBLE = frozenset(
    {ErrorCategory.RATE_LIMITED, ErrorCategory.TRANSIENT, ErrorCategory.UNAVAILABLE}
)
_FALLBACK_ELIGIBLE = frozenset(
    {
        ErrorCategory.TRANSIENT,
        ErrorCategory.RATE_LIMITED,
        ErrorCategory.TIMEOUT,
        ErrorCategory.UNAVAILABLE,
        ErrorCategory.BUDGET_EXHAUSTED,
    }
)


def is_retryable(category: ErrorCategory) -> bool:
    """Return True for transient failures worth a bounded retry (M9.16)."""
    return category in _RETRYABLE


def needs_cooldown(category: ErrorCategory) -> bool:
    """Return True when the provider needs a cooldown after 429/5xx (M9.17)."""
    return category in _COOLDOWN_ELIGIBLE


def is_fallback_eligible(category: ErrorCategory) -> bool:
    """Return True when routing should try the next provider/tool (M9.14)."""
    return category in _FALLBACK_ELIGIBLE


def to_trace_error_type(category: ErrorCategory) -> str:
    """Sanitized error_type for LangSmith metadata (M6.8). Never includes secrets."""
    return category.value


class AgentError(Exception):
    """Base class for normalized provider/tool failures (routing, budgets, metrics)."""

    def __init__(
        self,
        message: str,
        *,
        category: ErrorCategory = ErrorCategory.UNKNOWN,
        source: str = "",
        http_status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.source = source
        self.http_status = http_status
        self.retry_after = retry_after


class ClarificationRequiredError(AgentError):
    """Raised when execution is attempted before an ambiguous request is clarified."""

    def __init__(self, question: str) -> None:
        self.question = question
        super().__init__(
            question,
            category=ErrorCategory.INVALID_REQUEST,
            source="analyzer",
        )


class ExtractionError(AgentError):
    """Normalized failure raised by the safe URL extraction boundary."""

    def __init__(
        self,
        message: str,
        *,
        category: ErrorCategory = ErrorCategory.UNKNOWN,
        url: str = "",
        http_status: int | None = None,
    ) -> None:
        super().__init__(
            message,
            category=category,
            source="extraction",
            http_status=http_status,
        )
        self.url = url
