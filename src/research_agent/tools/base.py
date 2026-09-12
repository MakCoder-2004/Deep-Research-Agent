"""Replaceable asynchronous research-tool interface (M1.25, M1.26)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from research_agent.errors import AgentError, ErrorCategory
from research_agent.models.research import SearchHit, SearchTask

__all__ = ["ErrorCategory", "ResearchTool", "ToolError"]


class ToolError(AgentError):
    """Normalized tool failure for routing, budgets, and metrics."""

    def __init__(
        self,
        message: str,
        *,
        category: ErrorCategory = ErrorCategory.UNKNOWN,
        tool_name: str = "",
        http_status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(
            message,
            category=category,
            source=tool_name,
            http_status=http_status,
            retry_after=retry_after,
        )
        self.tool_name = tool_name


@runtime_checkable
class ResearchTool(Protocol):
    """Replaceable async search-tool contract.

    Implementations MUST propagate ``asyncio.CancelledError`` and honor
    caller deadlines (M3.25/M9.7). Routing metadata (domains, cost, limits)
    lives in the M3 tools/router layer; adapters expose identity via
    ``name`` and provenance via ``SearchHit.tool_name``.
    """

    @property
    def name(self) -> str: ...

    async def search(self, task: SearchTask) -> list[SearchHit]:
        """Execute a search task and return normalized hits."""
        ...

    async def health_check(self) -> bool:
        """Return True when the tool is reachable and usable."""
        ...
