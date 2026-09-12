"""Stack Exchange technical Q&A adapter (M3.22, optional)."""

from __future__ import annotations

import html as html_lib

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
    strip_html,
)
from research_agent.tools.base import ToolError

__all__ = ["StackExchangeTool"]

_DEFAULT_BASE_URL = "https://api.stackexchange.com/2.3"


class StackExchangeTool(AsyncHttpTool):
    """Technical questions and answers via the Stack Exchange API."""

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
        return "stack_exchange"

    async def search(self, task: SearchTask) -> list[SearchHit]:
        if not task.query.strip():
            raise ToolError(
                "Query must not be empty.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        limit = max_results_from_filters(task.filters, default=5, maximum=10)
        site = str(task.filters.get("site", "stackoverflow")).strip() or "stackoverflow"
        params = {
            "order": "desc",
            "sort": "relevance",
            "q": task.query,
            "site": site,
            "pagesize": str(limit),
            "filter": "withbody",
        }
        tagged = str(task.filters.get("tagged", "")).strip()
        if tagged:
            params["tagged"] = tagged
        if self._api_key.get_secret_value():
            params["key"] = self._api_key.get_secret_value()
        response = await self._request(
            task,
            "GET",
            f"{self._base_url}/search/advanced",
            error_message="Stack Exchange request",
            headers={"Accept": "application/json", "User-Agent": TOOL_USER_AGENT},
            params=params,
        )
        data = self._json(response, message="Stack Exchange")
        raw_results = data.get("items") if isinstance(data, dict) else None
        if not isinstance(raw_results, list):
            raise ToolError(
                "Stack Exchange returned an unexpected payload.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        hits: list[SearchHit] = []
        for index, item in enumerate(raw_results[:limit]):
            if not isinstance(item, dict):
                continue
            title = html_lib.unescape(clean_text(item.get("title")))
            snippet = strip_html(clean_text(item.get("body")))
            hit = build_hit(
                url=item.get("link"),
                title=title,
                snippet=snippet,
                publisher=f"Stack Exchange ({site})",
                published_at=parse_datetime(item.get("creation_date")),
                source_type=SourceType.TECHNICAL,
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
                f"{self._base_url}/info",
                error_message="Stack Exchange health request",
                headers={"Accept": "application/json", "User-Agent": TOOL_USER_AGENT},
                params={"site": "stackoverflow"},
            )
        except ToolError:
            return False
        return True
