"""Replaceable asynchronous LLM provider interface (M1.23, M1.24, M1.26)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from research_agent.errors import AgentError, ErrorCategory

__all__ = [
    "ErrorCategory",
    "LLMProvider",
    "ProviderCapability",
    "ProviderError",
    "ProviderMetadata",
]


class ProviderCapability(StrEnum):
    FAST_MULTILINGUAL = "fast_multilingual"
    LONG_CONTEXT = "long_context"
    REASONING = "reasoning"
    STRUCTURED_OUTPUT = "structured_output"
    ARABIC_CAPABLE = "arabic_capable"
    TOOL_CALLING = "tool_calling"


class ProviderError(AgentError):
    """Normalized provider failure for routing, budgets, and metrics."""

    def __init__(
        self,
        message: str,
        *,
        category: ErrorCategory = ErrorCategory.UNKNOWN,
        provider: str = "",
    ) -> None:
        super().__init__(message, category=category, source=provider)
        self.provider = provider


@dataclass(frozen=True)
class ProviderMetadata:
    """Capability advertisement used for routing without hard-coded model names.

    ``model_id`` MUST be injected from ``Settings.model_map`` (M3.11),
    never a literal in an adapter. ``capabilities`` is a frozenset so
    metadata stays hashable for M3.8 registries and M9.9 cache keys.
    """

    name: str
    model_id: str
    capabilities: frozenset[ProviderCapability] = field(default_factory=frozenset)
    priority: int = 0

    def supports(self, required: frozenset[ProviderCapability] | set[ProviderCapability]) -> bool:
        """Return True when all required capabilities are advertised."""
        return frozenset(required) <= self.capabilities


ResponseT = TypeVar("ResponseT", bound=BaseModel)


@runtime_checkable
class LLMProvider(Protocol):
    """Replaceable async LLM provider contract.

    Implementations MUST propagate ``asyncio.CancelledError`` (M2.11/M9.7),
    honor caller deadlines, and surface failures as ``ProviderError`` with
    an accurate ``ErrorCategory`` (bad structured output is INVALID_REQUEST,
    not TRANSIENT). ``model_id`` in metadata comes from config, and usage
    accounting (ProviderUsage) is threaded by the M3 router/M9 budgets —
    adapters expose provider/model via ``metadata`` for that purpose.
    """

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
