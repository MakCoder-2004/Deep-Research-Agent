"""Semantic Scholar paper search adapter (M3.15)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from pydantic import SecretStr

from research_agent.errors import ErrorCategory
from research_agent.models import SourceType
from research_agent.models.research import SearchHit, SearchTask
from research_agent.tools._common import (
    TOOL_USER_AGENT,
    AsyncHttpTool,
    build_hit,
    clean_text,
    max_results_from_filters,
)
from research_agent.tools.base import ToolError

__all__ = ["SemanticScholarTool"]

_DEFAULT_BASE_URL = "https://api.semanticscholar.org/graph/v1"
_FIELDS = "title,abstract,url,authors,year,venue,externalIds,openAccessPdf,tldr"


class SemanticScholarTool(AsyncHttpTool):
    """Paper, citation, and related-research discovery via Semantic Scholar."""

    def __init__(
        self,
        *,
        api_key: str | SecretStr = "",
        timeout_seconds: float = 15.0,
        base_url: str = _DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        super().__init__(timeout_seconds=timeout_seconds, client=client)
        self._api_key = SecretStr(key)
        self._base_url = base_url.rstrip("/")

    @property
    def name(self) -> str:
        return "semantic_scholar"

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": TOOL_USER_AGENT}
        if self._api_key.get_secret_value():
            headers["x-api-key"] = self._api_key.get_secret_value()
        return headers

    async def search(self, task: SearchTask) -> list[SearchHit]:
        if not task.query.strip():
            raise ToolError(
                "Query must not be empty.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        limit = max_results_from_filters(task.filters, default=5, maximum=10)
        response = await self._request(
            task,
            "GET",
            f"{self._base_url}/paper/search",
            error_message="Semantic Scholar request",
            headers=self._headers(),
            params={"query": task.query, "limit": str(limit), "fields": _FIELDS},
        )
        data = self._json(response, message="Semantic Scholar")
        raw_results = data.get("data") if isinstance(data, dict) else None
        if not isinstance(raw_results, list):
            raise ToolError(
                "Semantic Scholar returned an unexpected payload.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        hits: list[SearchHit] = []
        for index, item in enumerate(raw_results[:limit]):
            if not isinstance(item, dict):
                continue
            external = item.get("externalIds")
            doi = clean_text(external.get("DOI")) if isinstance(external, dict) else ""
            pdf = item.get("openAccessPdf")
            pdf_url = clean_text(pdf.get("url")) if isinstance(pdf, dict) else ""
            direct = clean_text(item.get("url"))
            paper_id = clean_text(item.get("paperId"))
            candidates = (
                direct,
                f"https://doi.org/{doi}" if doi else "",
                pdf_url,
                f"https://www.semanticscholar.org/paper/{paper_id}" if paper_id else "",
            )
            page_url = next((candidate for candidate in candidates if candidate), "")
            tldr = item.get("tldr")
            snippet = clean_text(tldr.get("text")) if isinstance(tldr, dict) else ""
            if not snippet:
                snippet = clean_text(item.get("abstract"))
            year = item.get("year")
            published_at = (
                datetime(year, 1, 1, tzinfo=UTC)
                if isinstance(year, int) and 1500 <= year <= 2100
                else None
            )
            hit = build_hit(
                url=page_url,
                title=item.get("title"),
                snippet=snippet,
                publisher=item.get("venue"),
                published_at=published_at,
                source_type=SourceType.PAPER,
                tool_name=self.name,
                score=max(0.0, 1.0 - index * 0.1),
            )
            if hit is not None:
                hits.append(hit)
        return hits

    async def health_check(self) -> bool:
        try:
            await self._request(
                None,
                "GET",
                f"{self._base_url}/paper/search",
                error_message="Semantic Scholar health request",
                headers=self._headers(),
                params={"query": "health check", "limit": "1", "fields": "title"},
            )
        except ToolError:
            return False
        return True
