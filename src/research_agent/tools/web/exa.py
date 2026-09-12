"""Exa semantic search adapter (M3.22, optional secondary general tool)."""

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

__all__ = ["ExaTool"]

_DEFAULT_BASE_URL = "https://api.exa.ai"


class ExaTool(AsyncHttpTool):
    """Semantic/technical search via Exa. Gracefully disabled without a key."""

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
        return "exa"

    async def search(self, task: SearchTask) -> list[SearchHit]:
        if not task.query.strip():
            raise ToolError(
                "Query must not be empty.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        if not self._api_key.get_secret_value():
            raise ToolError(
                "Exa API key is not configured.",
                category=ErrorCategory.UNAVAILABLE,
                tool_name=self.name,
            )
        limit = max_results_from_filters(task.filters, default=5, maximum=10)
        search_type = str(task.filters.get("type", "auto")).strip() or "auto"
        response = await self._request(
            task,
            "POST",
            f"{self._base_url}/search",
            error_message="Exa request",
            headers={
                "Content-Type": "application/json",
                "x-api-key": self._api_key.get_secret_value(),
                "User-Agent": TOOL_USER_AGENT,
            },
            json={"query": task.query, "numResults": limit, "type": search_type},
        )
        data = self._json(response, message="Exa")
        raw_results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(raw_results, list):
            raise ToolError(
                "Exa returned an unexpected payload.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        hits: list[SearchHit] = []
        for item in raw_results[:limit]:
            if not isinstance(item, dict):
                continue
            try:
                score = float(item.get("score", 0.0))
            except (ValueError, TypeError):
                score = 0.0
            hit = build_hit(
                url=item.get("url"),
                title=clean_text(item.get("title")) or clean_text(item.get("url")),
                snippet=item.get("text", ""),
                publisher=item.get("publisher"),
                published_at=parse_datetime(item.get("publishedDate")),
                source_type=SourceType.WEB,
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
                error_message="Exa health request",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": self._api_key.get_secret_value(),
                    "User-Agent": TOOL_USER_AGENT,
                },
                json={"query": "health check", "numResults": 1},
            )
        except ToolError:
            return False
        return True
