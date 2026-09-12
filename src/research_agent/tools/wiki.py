"""Wikipedia and Wikimedia background search adapter (M3.14)."""

from __future__ import annotations

import re

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
    parse_datetime,
    strip_html,
)
from research_agent.tools.base import ToolError

__all__ = ["WikipediaTool"]

_LANG_RE = re.compile(r"^[a-z]{2,3}(?:-[a-z]+)?$")


class WikipediaTool(AsyncHttpTool):
    """Background facts and terminology through a public MediaWiki API."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        base_url: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(timeout_seconds=timeout_seconds, client=client)
        self._base_url = base_url.rstrip("/")

    @property
    def name(self) -> str:
        return "wikipedia"

    def _language(self, task: SearchTask) -> str:
        override = str(task.filters.get("lang", "")).strip().lower()
        if _LANG_RE.fullmatch(override):
            return override
        return "ar" if task.language == Language.ARABIC else "en"

    def _api_url(self, lang: str, task: SearchTask | None = None) -> str:
        if self._base_url:
            return (
                self._base_url
                if self._base_url.endswith("api.php")
                else f"{self._base_url}/w/api.php"
            )
        host = self._project_host(task) if task is not None else None
        return f"https://{host or f'{lang}.wikipedia.org'}/w/api.php"

    def _project_label(self, task: SearchTask) -> str:
        project = str(task.filters.get("project", "wikipedia")).strip().lower()
        return {
            "commons": "Wikimedia Commons",
            "wikimedia_commons": "Wikimedia Commons",
            "wikidata": "Wikidata",
        }.get(project, "Wikipedia")

    def _project_host(self, task: SearchTask) -> str | None:
        project = str(task.filters.get("project", "wikipedia")).strip().lower()
        if project in ("commons", "wikimedia_commons"):
            return "commons.wikimedia.org"
        if project == "wikidata":
            return "www.wikidata.org"
        return None

    async def search(self, task: SearchTask) -> list[SearchHit]:
        if not task.query.strip():
            raise ToolError(
                "Query must not be empty.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        limit = max_results_from_filters(task.filters, default=5, maximum=10)
        lang = self._language(task)
        response = await self._request(
            task,
            "GET",
            self._api_url(lang, task),
            error_message="Wikipedia request",
            headers={"Accept": "application/json", "User-Agent": TOOL_USER_AGENT},
            params={
                "action": "query",
                "list": "search",
                "srsearch": task.query,
                "srlimit": str(limit),
                "format": "json",
                "utf8": "1",
            },
        )
        data = self._json(response, message="Wikipedia")
        query = data.get("query") if isinstance(data, dict) else None
        raw_results = query.get("search") if isinstance(query, dict) else None
        if not isinstance(raw_results, list):
            raise ToolError(
                "Wikipedia returned an unexpected payload.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        host = self._project_host(task) or f"{lang}.wikipedia.org"
        publisher = self._project_label(task)
        hits: list[SearchHit] = []
        for index, item in enumerate(raw_results[:limit]):
            if not isinstance(item, dict) or not isinstance(item.get("pageid"), int):
                continue
            page_url = f"https://{host}/?curid={item['pageid']}"
            hit = build_hit(
                url=page_url,
                title=item.get("title"),
                snippet=strip_html(clean_text(item.get("snippet"))),
                publisher=publisher,
                published_at=parse_datetime(item.get("timestamp")),
                source_type=SourceType.WIKI,
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
                self._api_url("en"),
                error_message="Wikipedia health request",
                headers={"Accept": "application/json", "User-Agent": TOOL_USER_AGENT},
                params={"action": "query", "meta": "siteinfo", "format": "json"},
            )
        except ToolError:
            return False
        return True
