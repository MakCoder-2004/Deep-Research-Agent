"""Research tool interfaces."""

from research_agent.errors import ErrorCategory
from research_agent.models import AttemptOutcome
from research_agent.tools.academic import ArxivTool, CrossrefTool, OpenAlexTool, SemanticScholarTool
from research_agent.tools.base import ResearchTool, ToolError
from research_agent.tools.medical import EuropePMCTool, EuropePmcTool, PubMedTool
from research_agent.tools.news import GdeltTool
from research_agent.tools.official import OfficialDomainTool
from research_agent.tools.router import (
    SOURCE_PRIORITIES,
    AttemptRecorder,
    ToolAttempt,
    ToolExecutionResult,
    ToolRouter,
    ToolRunRecorder,
    normalize_hit,
    normalize_hits,
    route_plan,
)
from research_agent.tools.technical import GithubTool, StackExchangeTool
from research_agent.tools.web import (
    BraveTool,
    DdgsTool,
    ExaTool,
    SearXNGTool,
    SerpApiTool,
    TavilyTool,
)
from research_agent.tools.wiki import WikipediaTool

__all__ = [
    "ArxivTool",
    "AttemptRecorder",
    "AttemptOutcome",
    "BraveTool",
    "CrossrefTool",
    "DdgsTool",
    "ErrorCategory",
    "EuropePMCTool",
    "EuropePmcTool",
    "ExaTool",
    "GdeltTool",
    "GithubTool",
    "OfficialDomainTool",
    "OpenAlexTool",
    "PubMedTool",
    "ResearchTool",
    "SearXNGTool",
    "SemanticScholarTool",
    "SerpApiTool",
    "StackExchangeTool",
    "TavilyTool",
    "ToolAttempt",
    "ToolExecutionResult",
    "ToolRouter",
    "ToolRunRecorder",
    "ToolError",
    "WikipediaTool",
    "SOURCE_PRIORITIES",
    "normalize_hit",
    "normalize_hits",
    "route_plan",
]
