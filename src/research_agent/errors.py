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
    UNKNOWN = "unknown"


class AgentError(Exception):
    """Base class for normalized provider/tool failures (routing, budgets, metrics)."""

    def __init__(
        self,
        message: str,
        *,
        category: ErrorCategory = ErrorCategory.UNKNOWN,
        source: str = "",
    ) -> None:
        super().__init__(message)
        self.category = category
        self.source = source
