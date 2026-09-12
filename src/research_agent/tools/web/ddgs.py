"""DDGS development fallback adapter (M3.22).

DuckDuckGo offers no stable official general web-search API (PLAN section 8).
The ``ddgs`` library scrapes or aggregates public endpoints and may break, be
blocked, or conflict with upstream terms. This adapter is therefore a
replaceable local-development fallback only — never a production dependency.

The optional ``ddgs`` package is imported lazily so the MVP installs without
new heavy dependencies. When it is absent, :meth:`search` raises a normalized
:class:`ToolError` instead of failing at import time.
"""

from __future__ import annotations

import asyncio
from typing import Any

from research_agent.errors import ErrorCategory
from research_agent.models import SourceType
from research_agent.models.research import SearchHit, SearchTask
from research_agent.tools._common import (
    build_hit,
    clean_text,
    max_results_from_filters,
    timeout_for_task,
)
from research_agent.tools.base import ToolError

__all__ = ["DdgsTool"]


class DdgsTool:
    """Local-development DDG fallback behind the common tool interface."""

    def __init__(self, *, timeout_seconds: float = 15.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")
        self._timeout_seconds = timeout_seconds

    @property
    def name(self) -> str:
        return "ddgs"

    async def aclose(self) -> None:
        return None

    def _run_blocking(
        self, query: str, limit: int, filters: dict[str, str]
    ) -> list[dict[str, Any]]:
        """Execute the blocking ddgs call (runs inside ``to_thread``)."""
        from ddgs import DDGS  # type: ignore[import-not-found]

        kwargs: dict[str, Any] = {"max_results": limit}
        region = str(filters.get("region", "")).strip()
        if region:
            kwargs["region"] = region
        safesearch = str(filters.get("safesearch", "")).strip().lower()
        if safesearch in ("on", "moderate", "off"):
            kwargs["safesearch"] = safesearch
        timelimit = str(filters.get("timelimit", "")).strip().lower()
        if timelimit in ("d", "w", "m", "y"):
            kwargs["timelimit"] = timelimit
        with DDGS() as ddgs:
            results = ddgs.text(query, **kwargs)
            if results is None:
                return []
            return [dict(item) for item in results if isinstance(item, dict)]

    async def search(self, task: SearchTask) -> list[SearchHit]:
        if not task.query.strip():
            raise ToolError(
                "Query must not be empty.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        try:
            import ddgs  # noqa: F401
        except ImportError as exc:
            raise ToolError(
                "ddgs package is not installed; development fallback unavailable.",
                category=ErrorCategory.UNAVAILABLE,
                tool_name=self.name,
            ) from exc
        _ = ddgs
        limit = max_results_from_filters(task.filters, default=5, maximum=10)
        filters = {str(k).lower(): str(v) for k, v in task.filters.items()}
        try:
            async with asyncio.timeout(timeout_for_task(task, self._timeout_seconds)):
                raw_items = await asyncio.to_thread(self._run_blocking, task.query, limit, filters)
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            raise ToolError(
                "DDGS request timed out.",
                category=ErrorCategory.TIMEOUT,
                tool_name=self.name,
            ) from exc
        except Exception as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise ToolError(
                "DDGS request failed.",
                category=ErrorCategory.TRANSIENT,
                tool_name=self.name,
            ) from exc
        hits: list[SearchHit] = []
        for index, item in enumerate(raw_items[:limit]):
            hit = build_hit(
                url=item.get("href"),
                title=clean_text(item.get("title")) or clean_text(item.get("href")),
                snippet=item.get("body", ""),
                publisher=None,
                published_at=None,
                source_type=SourceType.WEB,
                tool_name=self.name,
                score=max(0.0, 1.0 - float(index) * 0.1),
            )
            if hit is not None:
                hits.append(hit)
        return hits

    async def health_check(self) -> bool:
        try:
            import ddgs  # noqa: F401
        except ImportError:
            return False
        return True
