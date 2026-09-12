"""Tavily general web search adapter (M3.12)."""

from __future__ import annotations

import httpx
from pydantic import SecretStr

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

__all__ = ["TavilyTool"]

_DEFAULT_BASE_URL = "https://api.tavily.com"


class TavilyTool(AsyncHttpTool):
    """Primary general web search via the Tavily API."""

    def __init__(
        self,
        *,
        api_key: str | SecretStr,
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
        return "tavily"

    def _payload(self, task: SearchTask, limit: int) -> dict[str, object]:
        filters = {str(k).lower(): v for k, v in task.filters.items()}
        depth = filters.get("search_depth", "basic")
        if depth not in ("basic", "advanced", "fast", "ultra-fast"):
            depth = "basic"
        topic = (
            "news"
            if filters.get("channel", "").strip().lower() == "news"
            or filters.get("source_category", "").strip().lower() == "news"
            else "general"
        )
        body: dict[str, object] = {
            "query": task.query,
            "search_depth": depth,
            "max_results": limit,
            "topic": topic,
            "include_answer": False,
            "include_raw_content": False,
        }
        time_range = filters.get("time_range", "")
        if time_range in ("day", "week", "month", "year", "d", "w", "m", "y"):
            body["time_range"] = time_range
        if topic == "news":
            body["include_published_date"] = True
        for key in ("include_domains", "exclude_domains"):
            values = [part.strip() for part in filters.get(key, "").split(",") if part.strip()]
            if values:
                body[key] = values
        return body

    async def search(self, task: SearchTask) -> list[SearchHit]:
        if not task.query.strip():
            raise ToolError(
                "Query must not be empty.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        if not self._api_key.get_secret_value():
            raise ToolError(
                "Tavily API key is not configured.",
                category=ErrorCategory.AUTH,
                tool_name=self.name,
            )
        limit = max_results_from_filters(task.filters, default=5, maximum=10)
        filters = {str(k).lower(): str(v) for k, v in task.filters.items()}
        is_news = filters.get("channel", "").strip().lower() == "news" or (
            filters.get("source_category", "").strip().lower() == "news"
        )
        response = await self._request(
            task,
            "POST",
            f"{self._base_url}/search",
            error_message="Tavily request",
            headers={
                "Authorization": f"Bearer {self._api_key.get_secret_value()}",
                "Content-Type": "application/json",
                "User-Agent": TOOL_USER_AGENT,
            },
            json=self._payload(task, limit),
        )
        data = self._json(response, message="Tavily")
        raw_results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(raw_results, list):
            raise ToolError(
                "Tavily returned an unexpected payload.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        hits: list[SearchHit] = []
        for item in raw_results[:limit]:
            if not isinstance(item, dict):
                continue
            try:
                score = float(item.get("score", 0.0))
            except (TypeError, ValueError):
                score = 0.0
            hit = build_hit(
                url=item.get("url"),
                title=item.get("title"),
                snippet=item.get("content", ""),
                publisher=item.get("publisher"),
                published_at=parse_datetime(item.get("published_date")),
                source_type=SourceType.NEWS if is_news else SourceType.WEB,
                tool_name=self.name,
                score=score,
            )
            if hit is not None:
                hits.append(hit)
        return hits

    async def health_check(self) -> bool:
        if not self._api_key.get_secret_value():
            return False
        try:
            await self._request(
                None,
                "POST",
                f"{self._base_url}/search",
                error_message="Tavily health request",
                headers={
                    "Authorization": f"Bearer {self._api_key.get_secret_value()}",
                    "Content-Type": "application/json",
                    "User-Agent": TOOL_USER_AGENT,
                },
                json={
                    "query": "health check",
                    "max_results": 1,
                    "topic": "general",
                    "include_answer": False,
                },
            )
        except ToolError:
            return False
        return True
