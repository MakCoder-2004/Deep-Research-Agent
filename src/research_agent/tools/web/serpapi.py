"""SerpApi search-engine fallback adapter (M3.22, optional secondary tool)."""

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
    clean_text,
    max_results_from_filters,
    parse_datetime,
)
from research_agent.tools.base import ToolError

__all__ = ["SerpApiTool"]

_DEFAULT_BASE_URL = "https://serpapi.com"


class SerpApiTool(AsyncHttpTool):
    """Search-engine fallback via SerpApi. Gracefully disabled without a key."""

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
        return "serpapi"

    async def search(self, task: SearchTask) -> list[SearchHit]:
        if not task.query.strip():
            raise ToolError(
                "Query must not be empty.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        if not self._api_key.get_secret_value():
            raise ToolError(
                "SerpApi key is not configured.",
                category=ErrorCategory.AUTH,
                tool_name=self.name,
            )
        limit = max_results_from_filters(task.filters, default=5, maximum=10)
        engine = str(task.filters.get("engine", "google")).strip() or "google"
        response = await self._request(
            task,
            "GET",
            f"{self._base_url}/search.json",
            error_message="SerpApi request",
            headers={"User-Agent": TOOL_USER_AGENT},
            params={
                "q": task.query,
                "api_key": self._api_key.get_secret_value(),
                "num": str(limit),
                "engine": engine,
            },
        )
        data = self._json(response, message="SerpApi")
        raw_results = data.get("organic_results") if isinstance(data, dict) else None
        if not isinstance(raw_results, list):
            raise ToolError(
                "SerpApi returned an unexpected payload.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        hits: list[SearchHit] = []
        for index, item in enumerate(raw_results[:limit]):
            if not isinstance(item, dict):
                continue
            hit = build_hit(
                url=item.get("link"),
                title=clean_text(item.get("title")) or clean_text(item.get("link")),
                snippet=item.get("snippet", ""),
                publisher=item.get("source") or item.get("displayed_link"),
                published_at=parse_datetime(item.get("date")),
                source_type=SourceType.WEB,
                tool_name=self.name,
                score=max(0.0, 1.0 - float(index) * 0.1),
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
                "GET",
                f"{self._base_url}/search.json",
                error_message="SerpApi health request",
                headers={"User-Agent": TOOL_USER_AGENT},
                params={
                    "q": "health check",
                    "api_key": self._api_key.get_secret_value(),
                    "num": "1",
                    "engine": "google",
                },
            )
        except ToolError:
            return False
        return True
