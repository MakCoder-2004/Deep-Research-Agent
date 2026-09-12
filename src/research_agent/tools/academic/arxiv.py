"""arXiv preprint search adapter (M3.17) with rate-limit etiquette."""

from __future__ import annotations

import asyncio
import time
import xml.etree.ElementTree as ET

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
    parse_datetime,
    timeout_for_task,
    truncate,
)
from research_agent.tools.base import ToolError

__all__ = ["ArxivTool", "reset_arxiv_throttle_for_tests"]

_DEFAULT_BASE_URL = "https://export.arxiv.org/api"
_MIN_INTERVAL_SECONDS = 3.0
_ATOM_NS = "http://www.w3.org/2005/Atom"
_throttle_lock = asyncio.Lock()
_last_call_monotonic = 0.0


def _atom_text(entry: ET.Element, tag: str) -> str:
    node = entry.find(f"{{{_ATOM_NS}}}{tag}")
    return clean_text(node.text if node is not None else "")


def reset_arxiv_throttle_for_tests() -> None:
    """Reset the shared arXiv throttle clock for isolated tests."""
    global _last_call_monotonic
    _last_call_monotonic = 0.0


class ArxivTool(AsyncHttpTool):
    """Technical and scientific preprint discovery through the arXiv API."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        base_url: str = _DEFAULT_BASE_URL,
        min_interval_seconds: float = _MIN_INTERVAL_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(timeout_seconds=timeout_seconds, client=client)
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must not be negative.")
        self._base_url = base_url.rstrip("/")
        self._min_interval = min_interval_seconds

    @property
    def name(self) -> str:
        return "arxiv"

    def _parse_feed(self, payload: str, limit: int) -> list[SearchHit]:
        try:
            root = ET.fromstring(payload)  # noqa: S314 - ElementTree does not resolve external entities.
        except ET.ParseError as exc:
            raise ToolError(
                "arXiv returned invalid XML.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            ) from exc
        hits: list[SearchHit] = []
        for index, entry in enumerate(root.findall(f"{{{_ATOM_NS}}}entry")[:limit]):
            page_url = _atom_text(entry, "id")
            title = " ".join(_atom_text(entry, "title").split())
            hit = build_hit(
                url=page_url,
                title=title,
                snippet=truncate(_atom_text(entry, "summary")),
                publisher="arXiv",
                published_at=parse_datetime(_atom_text(entry, "published")),
                source_type=SourceType.PAPER,
                tool_name=self.name,
                score=max(0.0, 1.0 - index * 0.1),
            )
            if hit is not None:
                hits.append(hit)
        return hits

    async def _throttled_request(self, task: SearchTask | None, limit: int) -> httpx.Response:
        global _last_call_monotonic
        total_timeout = timeout_for_task(task, self._timeout_seconds + self._min_interval + 5.0)
        try:
            async with asyncio.timeout(total_timeout):
                async with _throttle_lock:
                    wait = self._min_interval - (time.monotonic() - _last_call_monotonic)
                    if wait > 0:
                        await asyncio.sleep(wait)
                    try:
                        return await self._request(
                            task,
                            "GET",
                            f"{self._base_url}/query",
                            error_message="arXiv request",
                            headers={"User-Agent": TOOL_USER_AGENT},
                            params={
                                "search_query": (
                                    f"all:{task.query}" if task is not None else "all:health"
                                ),
                                "start": "0",
                                "max_results": str(limit),
                                "sortBy": "relevance",
                                "sortOrder": "descending",
                            },
                        )
                    finally:
                        _last_call_monotonic = time.monotonic()
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            raise ToolError(
                "arXiv request timed out.", category=ErrorCategory.TIMEOUT, tool_name=self.name
            ) from exc

    async def search(self, task: SearchTask) -> list[SearchHit]:
        if not task.query.strip():
            raise ToolError(
                "Query must not be empty.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        limit = max_results_from_filters(task.filters, default=5, maximum=10)
        response = await self._throttled_request(task, limit)
        return self._parse_feed(response.text, limit)

    async def health_check(self) -> bool:
        try:
            await self._throttled_request(None, 1)
        except ToolError:
            return False
        return True
