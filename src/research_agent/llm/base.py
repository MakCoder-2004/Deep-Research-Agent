"""Replaceable asynchronous LLM provider interface (M1.23, M1.24, M1.26)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, TypeVar

from pydantic import BaseModel


class ProviderCapability(StrEnum):
    FAST_MULTILINGUAL = "fast_multilingual"
    LONG_CONTEXT = "long_context"
    REASONING = "reasoning"
    STRUCTURED_OUTPUT = "structured_output"
    ARABIC_CAPABLE = "arabic_capable"
    TOOL_CALLING = "tool_calling"


class ErrorCategory(StrEnum):
    TRANSIENT = "transient"
    RATE_LIMITED = "rate_limited"
    AUTH = "auth"
    INVALID_REQUEST = "invalid_request"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    CONTENT_FILTERED = "content_filtered"
    UNKNOWN = "unknown"


class ProviderError(Exception):
    """Normalized provider failure for routing, budgets, and metrics."""

    def __init__(
        self,
        message: str,
        *,
        category: ErrorCategory = ErrorCategory.UNKNOWN,
        provider: str = "",
    ) -> None:
        super().__init__(message)
        self.category = category
        self.provider = provider


@dataclass(frozen=True)
class ProviderMetadata:
    """Capability advertisement used for routing without hard-coded model names."""

    name: str
    model_id: str
    capabilities: set[ProviderCapability] = field(default_factory=set)
    priority: int = 0


ResponseT = TypeVar("ResponseT", bound=BaseModel)


class LLMProvider(Protocol):
    """Replaceable async LLM provider contract."""

    @property
    def metadata(self) -> ProviderMetadata: ...

    async def complete(self, prompt: str) -> str:
        """Return free-form completion text for a prompt."""
        ...

    async def structured(self, prompt: str, response_model: type[ResponseT]) -> ResponseT:
        """Return a validated structured response for a prompt."""
        ...

    async def health_check(self) -> bool:
        """Return True when the provider can serve the configured capabilities."""
        ...
