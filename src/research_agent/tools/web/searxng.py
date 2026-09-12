"""Self-hosted SearXNG metasearch adapter (M3.22, experimental fallback).

Public SearXNG instances must not be treated as production infrastructure
(PLAN section 8). A private instance may run on Oracle, but upstream engines
can still throttle the server IP. The adapter stays disabled until
``SEARXNG_BASE_URL`` is configured.
"""

from __future__ import annotations

import httpx

from research_agent.errors import ErrorCategory
from research_agent.models import Language, SourceType
from research_agent.models.research import SearchHit, SearchTask
from research_agent.tools._common import (
    TOOL_USER_AGENT,
    AsyncHttpTool,
    build_hit,
    clean_text,
    max_results_from_filters,
)
from research_agent.tools.base import ToolError

__all__ = ["SearXNGTool"]


class SearXNGTool(AsyncHttpTool):
    """Quota-free metasearch abstraction over a self-hosted SearXNG instance."""

    def __init__(
        self,
        *,
        base_url: str = "",
        timeout_seconds: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(timeout_seconds=timeout_seconds, client=client)
        self._base_url = base_url.rstrip("/")

    @property
    def name(self) -> str:
        return "searxng"

    async def search(self, task: SearchTask) -> list[SearchHit]:
        if not task.query.strip():
            raise ToolError(
                "Query must not be empty.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        if not self._base_url:
            raise ToolError(
                "SearXNG base URL is not configured.",
                category=ErrorCategory.UNAVAILABLE,
                tool_name=self.name,
            )
        limit = max_results_from_filters(task.filters, default=5, maximum=10)
        language = "ar" if task.language == Language.ARABIC else "en"
        override = str(task.filters.get("language", "")).strip()
        if override:
            language = override
        response = await self._request(
            task,
            "GET",
            f"{self._base_url}/search",
            error_message="SearXNG request",
            headers={"Accept": "application/json", "User-Agent": TOOL_USER_AGENT},
            params={
                "q": task.query,
                "format": "json",
                "language": language,
                "categories": "general",
            },
        )
        data = self._json(response, message="SearXNG")
        raw_results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(raw_results, list):
            raise ToolError(
                "SearXNG returned an unexpected payload.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        hits: list[SearchHit] = []
        for index, item in enumerate(raw_results[:limit]):
            if not isinstance(item, dict):
                continue
            hit = build_hit(
                url=item.get("url"),
                title=clean_text(item.get("title")) or clean_text(item.get("url")),
                snippet=item.get("content", ""),
                publisher=item.get("publisher") or item.get("engine"),
                published_at=None,
                source_type=SourceType.WEB,
                tool_name=self.name,
                score=max(0.0, 1.0 - float(index) * 0.1),
            )
            if hit is not None:
                hits.append(hit)
        return hits

    async def health_check(self) -> bool:
        if not self._base_url:
            return False
        try:
            await self._request(
                None,
                "GET",
                self._base_url,
                error_message="SearXNG health request",
                headers={"User-Agent": TOOL_USER_AGENT},
            )
        except ToolError:
            return False
        return True
