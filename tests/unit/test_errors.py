"""Unit tests for the shared provider/tool error taxonomy (M1.26)."""

from __future__ import annotations

from research_agent.errors import AgentError, ErrorCategory
from research_agent.llm.base import ProviderError
from research_agent.tools.base import ToolError


def test_error_categories_cover_routing_needs() -> None:
    assert {category.value for category in ErrorCategory} >= {
        "transient",
        "rate_limited",
        "auth",
        "timeout",
        "unavailable",
        "unknown",
    }


def test_provider_and_tool_errors_share_base() -> None:
    provider_error = ProviderError("boom", category=ErrorCategory.RATE_LIMITED, provider="groq")
    tool_error = ToolError("bust", category=ErrorCategory.TIMEOUT, tool_name="tavily")
    assert isinstance(provider_error, AgentError)
    assert isinstance(tool_error, AgentError)
    assert provider_error.category is ErrorCategory.RATE_LIMITED
    assert provider_error.provider == "groq"
    assert provider_error.source == "groq"
    assert tool_error.category is ErrorCategory.TIMEOUT
    assert tool_error.tool_name == "tavily"
    assert tool_error.source == "tavily"
