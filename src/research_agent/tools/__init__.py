"""Research tool interfaces."""

from research_agent.errors import ErrorCategory
from research_agent.tools.base import ResearchTool, ToolError

__all__ = ["ErrorCategory", "ResearchTool", "ToolError"]
