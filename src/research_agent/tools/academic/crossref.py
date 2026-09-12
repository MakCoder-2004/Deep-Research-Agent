"""Crossref DOI and publication metadata adapter (M3.16)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

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

__all__ = ["CrossrefTool"]

_DEFAULT_BASE_URL = "https://api.crossref.org"


def _crossref_date(item: dict[str, Any]) -> datetime | None:
    """Extract the first usable Crossref publication date."""
    for key in ("published", "published-print", "published-online", "issued", "created"):
        node = item.get(key)
        if not isinstance(node, dict):
            continue
        parts = node.get("date-parts")
        if not isinstance(parts, list) or not parts or not isinstance(parts[0], list):
            continue
        numbers = [
            number
            for number in parts[0]
            if isinstance(number, int) and not isinstance(number, bool)
        ]
        if not numbers:
            continue
        try:
            return datetime(
                numbers[0],
                numbers[1] if len(numbers) > 1 else 1,
                numbers[2] if len(numbers) > 2 else 1,
                tzinfo=UTC,
            )
        except ValueError:
            continue
    return None


def _doi_query(query: str) -> str:
    value = query.strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if value.lower().startswith(prefix):
            return value[len(prefix) :].strip()
    return value


class CrossrefTool(AsyncHttpTool):
    """DOI lookup and publication search through the public Crossref API."""

    def __init__(
        self,
        *,
        mailto: str = "",
        timeout_seconds: float = 15.0,
        base_url: str = _DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(timeout_seconds=timeout_seconds, client=client)
        self._mailto = mailto.strip()
        self._base_url = base_url.rstrip("/")

    @property
    def name(self) -> str:
        return "crossref"

    def _headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "User-Agent": TOOL_USER_AGENT}

    def _items(self, data: object) -> list[object]:
        message = data.get("message") if isinstance(data, dict) else None
        if not isinstance(message, dict):
            return []
        item = message.get("items")
        if isinstance(item, list):
            return item
        return [message]

    async def search(self, task: SearchTask) -> list[SearchHit]:
        if not task.query.strip():
            raise ToolError(
                "Query must not be empty.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        query = _doi_query(task.query)
        limit = max_results_from_filters(task.filters, default=5, maximum=10)
        params: dict[str, str] = {
            "select": "DOI,title,publisher,URL,published,issued,created,container-title",
        }
        if self._mailto:
            params["mailto"] = self._mailto
        if query.lower().startswith("10."):
            url = f"{self._base_url}/works/{quote(query, safe='')}"
        else:
            url = f"{self._base_url}/works"
            params.update({"query": task.query, "rows": str(limit)})
        response = await self._request(
            task,
            "GET",
            url,
            error_message="Crossref request",
            headers=self._headers(),
            params=params,
        )
        data = self._json(response, message="Crossref")
        message = data.get("message") if isinstance(data, dict) else None
        if not isinstance(message, dict):
            raise ToolError(
                "Crossref returned an unexpected payload.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        raw_results = self._items(data)
        hits: list[SearchHit] = []
        for index, item in enumerate(raw_results[:limit]):
            if not isinstance(item, dict):
                continue
            titles = item.get("title")
            title = titles[0] if isinstance(titles, list) and titles else ""
            containers = item.get("container-title")
            snippet = containers[0] if isinstance(containers, list) and containers else ""
            doi = clean_text(item.get("DOI"))
            page_url = clean_text(item.get("URL")) or (f"https://doi.org/{doi}" if doi else "")
            hit = build_hit(
                url=page_url,
                title=title,
                snippet=snippet,
                publisher=item.get("publisher"),
                published_at=_crossref_date(item),
                source_type=SourceType.ACADEMIC,
                tool_name=self.name,
                score=max(0.0, 1.0 - index * 0.1),
            )
            if hit is not None:
                hits.append(hit)
        return hits

    async def health_check(self) -> bool:
        params = {"rows": "1", "query": "health check"}
        if self._mailto:
            params["mailto"] = self._mailto
        try:
            await self._request(
                None,
                "GET",
                f"{self._base_url}/works",
                error_message="Crossref health request",
                headers=self._headers(),
                params=params,
            )
        except ToolError:
            return False
        return True
