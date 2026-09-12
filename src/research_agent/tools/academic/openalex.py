"""OpenAlex academic discovery adapter (M3.22, optional)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from research_agent.errors import ErrorCategory
from research_agent.models import SourceType
from research_agent.models.research import SearchHit, SearchTask
from research_agent.tools._common import (
    TOOL_USER_AGENT,
    AsyncHttpTool,
    build_hit,
    clean_text,
    doi_url,
    max_results_from_filters,
    normalize_doi,
    parse_datetime,
    truncate,
)
from research_agent.tools.base import ToolError

__all__ = ["OpenAlexTool", "reconstruct_abstract"]

_DEFAULT_BASE_URL = "https://api.openalex.org"
_SELECT = (
    "id,doi,title,publication_year,publication_date,primary_location,"
    "authorships,cited_by_count,abstract_inverted_index"
)


def reconstruct_abstract(inverted: object) -> str:
    """Rebuild an abstract from OpenAlex's inverted index when present."""
    if not isinstance(inverted, dict):
        return ""
    positioned: dict[int, str] = {}
    for word, positions in inverted.items():
        if not isinstance(word, str) or not isinstance(positions, list):
            continue
        for position in positions:
            if isinstance(position, int) and not isinstance(position, bool) and position >= 0:
                positioned.setdefault(position, word)
    return " ".join(positioned[index] for index in sorted(positioned))


class OpenAlexTool(AsyncHttpTool):
    """Academic discovery and metadata through the public OpenAlex index."""

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
        return "openalex"

    def _headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "User-Agent": TOOL_USER_AGENT}

    async def search(self, task: SearchTask) -> list[SearchHit]:
        if not task.query.strip():
            raise ToolError(
                "Query must not be empty.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        limit = max_results_from_filters(task.filters, default=5, maximum=10)
        params = {"search": task.query, "per-page": str(limit), "select": _SELECT}
        if self._mailto:
            params["mailto"] = self._mailto
        response = await self._request(
            task,
            "GET",
            f"{self._base_url}/works",
            error_message="OpenAlex request",
            headers=self._headers(),
            params=params,
        )
        data = self._json(response, message="OpenAlex")
        raw_results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(raw_results, list):
            raise ToolError(
                "OpenAlex returned an unexpected payload.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        hits: list[SearchHit] = []
        for index, item in enumerate(raw_results[:limit]):
            if not isinstance(item, dict):
                continue
            work: dict[str, Any] = item
            location = work.get("primary_location")
            landing = ""
            publisher = ""
            if isinstance(location, dict):
                landing = clean_text(location.get("landing_page_url"))
                source = location.get("source")
                if isinstance(source, dict):
                    publisher = clean_text(source.get("display_name"))
            doi = normalize_doi(work.get("doi"))
            page_url = doi_url(doi) or landing or clean_text(work.get("id"))
            published_at = parse_datetime(work.get("publication_date"))
            year = work.get("publication_year")
            if published_at is None and isinstance(year, int) and 1500 <= year <= 2100:
                published_at = datetime(year, 1, 1, tzinfo=UTC)
            hit = build_hit(
                url=page_url,
                title=work.get("title"),
                snippet=truncate(reconstruct_abstract(work.get("abstract_inverted_index"))),
                publisher=publisher or None,
                published_at=published_at,
                source_type=SourceType.PAPER,
                tool_name=self.name,
                score=max(0.0, 1.0 - index * 0.1),
                doi=doi,
            )
            if hit is not None:
                hits.append(hit)
        return hits

    async def health_check(self) -> bool:
        try:
            await self._request(
                None,
                "GET",
                f"{self._base_url}/works",
                error_message="OpenAlex health request",
                headers=self._headers(),
                params={"per-page": "1", "search": "health"},
            )
        except ToolError:
            return False
        return True
