"""GDELT current-event search adapter (M3.18)."""

from __future__ import annotations

import httpx

from research_agent.errors import ErrorCategory
from research_agent.models import SourceType
from research_agent.models.research import SearchHit, SearchTask
from research_agent.tools._common import (
    TOOL_USER_AGENT,
    AsyncHttpTool,
    build_hit,
    max_results_from_filters,
    parse_datetime,
)
from research_agent.tools.base import ToolError

__all__ = ["GdeltTool"]

_DEFAULT_BASE_URL = "https://api.gdeltproject.org/api/v2"


class GdeltTool(AsyncHttpTool):
    """Current events and regional/global news via GDELT DOC 2.0."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        base_url: str = _DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(timeout_seconds=timeout_seconds, client=client)
        self._base_url = base_url.rstrip("/")

    @property
    def name(self) -> str:
        return "gdelt"

    async def search(self, task: SearchTask) -> list[SearchHit]:
        if not task.query.strip():
            raise ToolError(
                "Query must not be empty.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        limit = max_results_from_filters(task.filters, default=5, maximum=10)
        filters = {str(k).lower(): str(v) for k, v in task.filters.items()}
        mode = filters.get("mode", "artlist").strip() or "artlist"
        if mode not in ("artlist", "timelinevol", "tonechart", "wordcloud"):
            mode = "artlist"
        params = {
            "query": task.query,
            "mode": mode,
            "maxrecords": str(min(limit, 100)),
            "format": "json",
            "sort": filters.get("sort", "datedesc").strip() or "datedesc",
        }
        if filters.get("timespan", "").strip():
            params["timespan"] = filters["timespan"].strip()
        response = await self._request(
            task,
            "GET",
            f"{self._base_url}/doc",
            error_message="GDELT request",
            headers={"Accept": "application/json", "User-Agent": TOOL_USER_AGENT},
            params=params,
        )
        data = self._json(response, message="GDELT")
        raw_results = data.get("articles") if isinstance(data, dict) else None
        if not isinstance(raw_results, list):
            raise ToolError(
                "GDELT returned an unexpected payload.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        hits: list[SearchHit] = []
        for index, item in enumerate(raw_results[:limit]):
            if not isinstance(item, dict):
                continue
            url = item.get("url") or item.get("documentidentifier")
            hit = build_hit(
                url=url,
                title=item.get("title"),
                snippet=item.get("snippet", ""),
                publisher=item.get("domain"),
                published_at=parse_datetime(item.get("seendate")),
                source_type=SourceType.NEWS,
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
                f"{self._base_url}/doc",
                error_message="GDELT health request",
                headers={"Accept": "application/json", "User-Agent": TOOL_USER_AGENT},
                params={
                    "query": "health check",
                    "mode": "artlist",
                    "maxrecords": "1",
                    "format": "json",
                },
            )
        except ToolError:
            return False
        return True
