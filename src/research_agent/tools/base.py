"""Replaceable asynchronous research-tool interface (M1.25, M1.26)."""

from __future__ import annotations

from typing import Protocol

from research_agent.errors import AgentError, ErrorCategory
from research_agent.models.research import SearchHit, SearchTask

__all__ = ["ResearchTool", "ToolError"]


class ToolError(AgentError):
    """Normalized tool failure for routing, budgets, and metrics."""

    def __init__(
        self,
        message: str,
        *,
        category: ErrorCategory = ErrorCategory.UNKNOWN,
        tool_name: str = "",
    ) -> None:
        super().__init__(message, category=category, source=tool_name)
        self.tool_name = tool_name


class ResearchTool(Protocol):
    """Replaceable async search-tool contract."""

    @property
    def name(self) -> str: ...

    async def search(self, task: SearchTask) -> list[SearchHit]:
        """Execute a search task and return normalized hits."""
        ...

    async def health_check(self) -> bool:
        """Return True when the tool is reachable and usable."""
        ...
