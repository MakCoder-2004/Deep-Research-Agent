"""Brave general web and news search adapter (M3.13)."""

from __future__ import annotations

import httpx
from pydantic import SecretStr

from research_agent.errors import ErrorCategory
from research_agent.models import Language, SourceType
from research_agent.models.research import SearchHit, SearchTask
from research_agent.tools._common import (
    TOOL_USER_AGENT,
    AsyncHttpTool,
    build_hit,
    clean_text,
    domain_of,
    max_results_from_filters,
    parse_datetime,
)
from research_agent.tools.base import ToolError

__all__ = ["BraveTool"]

_DEFAULT_BASE_URL = "https://api.search.brave.com/res/v1"


class BraveTool(AsyncHttpTool):
    """General web search via the Brave Search API."""

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
        return "brave_search"

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "X-Subscription-Token": self._api_key.get_secret_value(),
            "User-Agent": TOOL_USER_AGENT,
        }

    def _params(self, task: SearchTask, limit: int) -> dict[str, str]:
        filters = {str(k).lower(): v for k, v in task.filters.items()}
        language = "ar" if task.language == Language.ARABIC else "en"
        if filters.get("search_lang", "").strip():
            language = filters["search_lang"].strip()
        params = {
            "q": task.query,
            "count": str(limit),
            "text_decorations": "0",
            "search_lang": language,
        }
        for key in ("country", "freshness"):
            value = filters.get(key, "").strip()
            if value and (key == "country" or value in ("pd", "pw", "pm", "py")):
                params[key] = value
        return params

    async def search(self, task: SearchTask) -> list[SearchHit]:
        if not task.query.strip():
            raise ToolError(
                "Query must not be empty.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        if not self._api_key.get_secret_value():
            raise ToolError(
                "Brave API key is not configured.", category=ErrorCategory.AUTH, tool_name=self.name
            )
        limit = max_results_from_filters(task.filters, default=5, maximum=10)
        channel = str(task.filters.get("channel", "")).strip().lower()
        is_news = channel == "news"
        response = await self._request(
            task,
            "GET",
            f"{self._base_url}/{'news/search' if is_news else 'web/search'}",
            error_message="Brave request",
            headers=self._headers(),
            params=self._params(task, limit),
        )
        data = self._json(response, message="Brave")
        raw_results: object = None
        if isinstance(data, dict):
            if is_news:
                raw_results = data.get("results")
            else:
                web = data.get("web")
                raw_results = web.get("results") if isinstance(web, dict) else data.get("results")
        if not isinstance(raw_results, list):
            raise ToolError(
                "Brave returned an unexpected payload.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        hits: list[SearchHit] = []
        for index, item in enumerate(raw_results[:limit]):
            if not isinstance(item, dict):
                continue
            publisher = ""
            profile = item.get("profile")
            if isinstance(profile, dict):
                publisher = clean_text(profile.get("name"))
            meta = item.get("meta_url")
            if isinstance(meta, dict) and not publisher:
                publisher = clean_text(meta.get("hostname")) or clean_text(meta.get("netloc"))
            raw_url = item.get("url")
            if not publisher and isinstance(raw_url, str):
                publisher = domain_of(raw_url)
            hit = build_hit(
                url=raw_url,
                title=item.get("title"),
                snippet=item.get("description", ""),
                publisher=publisher or None,
                published_at=parse_datetime(item.get("page_age") or item.get("age")),
                source_type=SourceType.NEWS if is_news else SourceType.WEB,
                tool_name=self.name,
                score=max(0.0, 1.0 - index * 0.1),
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
                f"{self._base_url}/web/search",
                error_message="Brave health request",
                headers=self._headers(),
                params={"q": "health check", "count": "1"},
            )
        except ToolError:
            return False
        return True
