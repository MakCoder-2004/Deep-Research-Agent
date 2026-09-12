"""Academic search subpackage."""

from __future__ import annotations

from research_agent.tools.academic.arxiv import ArxivTool
from research_agent.tools.academic.crossref import CrossrefTool
from research_agent.tools.academic.openalex import OpenAlexTool
from research_agent.tools.academic.semantic_scholar import SemanticScholarTool

__all__ = ["ArxivTool", "CrossrefTool", "OpenAlexTool", "SemanticScholarTool"]
