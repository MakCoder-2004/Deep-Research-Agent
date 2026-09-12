"""Research tool interfaces."""

from research_agent.tools.base import ResearchTool, ToolError
from research_agent.errors import ErrorCategory

__all__ = ["ErrorCategory", "ResearchTool", "ToolError"]
