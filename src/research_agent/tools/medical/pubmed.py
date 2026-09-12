"""PubMed search through the NCBI E-utilities API (M3.20)."""

from __future__ import annotations

import asyncio
import time

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
    normalize_doi,
    parse_datetime,
    timeout_for_task,
    truncate,
)
from research_agent.tools.base import ToolError

__all__ = ["PubMedTool"]

_DEFAULT_BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_DEFAULT_INTERVAL_SECONDS = 1.0 / 3.0


class PubMedTool(AsyncHttpTool):
    """Biomedical literature discovery using NCBI's public E-utilities."""

    def __init__(
        self,
        *,
        email: str = "",
        tool: str = "deep-research-agent",
        api_key: str | SecretStr = "",
        timeout_seconds: float = 15.0,
        base_url: str = _DEFAULT_BASE_URL,
        min_interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(timeout_seconds=timeout_seconds, client=client)
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must not be negative.")
        raw_key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        self._email = email.strip()
        self._tool = tool.strip() or "deep-research-agent"
        self._api_key = SecretStr(raw_key)
        self._base_url = base_url.rstrip("/")
        self._min_interval = min_interval_seconds
        self._throttle_lock = asyncio.Lock()
        self._last_call_monotonic = 0.0

    @property
    def name(self) -> str:
        return "pubmed"

    def _params(self, **values: str) -> dict[str, str]:
        params = {"db": "pubmed", "retmode": "json", "tool": self._tool}
        params.update(values)
        if self._email:
            params["email"] = self._email
        if self._api_key.get_secret_value():
            params["api_key"] = self._api_key.get_secret_value()
        return params

    async def _get(self, task: SearchTask | None, endpoint: str, params: dict[str, str]) -> object:
        total_timeout = timeout_for_task(task, self._timeout_seconds + self._min_interval + 1.0)
        try:
            async with asyncio.timeout(total_timeout):
                async with self._throttle_lock:
                    wait = self._min_interval - (time.monotonic() - self._last_call_monotonic)
                    if wait > 0:
                        await asyncio.sleep(wait)
                    try:
                        response = await self._request(
                            task,
                            "GET",
                            f"{self._base_url}/{endpoint}",
                            error_message="PubMed request",
                            headers={"Accept": "application/json", "User-Agent": TOOL_USER_AGENT},
                            params=params,
                        )
                    finally:
                        self._last_call_monotonic = time.monotonic()
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            raise ToolError(
                "PubMed request timed out.",
                category=ErrorCategory.TIMEOUT,
                tool_name=self.name,
            ) from exc
        return self._json(response, message="PubMed")

    def _summary_hits(self, data: object, ids: list[str], limit: int) -> list[SearchHit]:
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, dict):
            raise ToolError(
                "PubMed returned an unexpected payload.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        if result.get("error") or result.get("ERROR"):
            raise ToolError(
                "PubMed returned an API error.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        ordered_ids = result.get("uids")
        uids = [clean_text(uid) for uid in ordered_ids] if isinstance(ordered_ids, list) else ids
        hits: list[SearchHit] = []
        for index, uid in enumerate(uids[:limit]):
            item = result.get(uid)
            if not isinstance(item, dict):
                continue
            publication_date = next(
                (
                    clean_text(item.get(field))
                    for field in ("epubdate", "pubdate", "sortpubdate")
                    if clean_text(item.get(field))
                ),
                "",
            )
            title = clean_text(item.get("title"))
            journal = clean_text(item.get("fulljournalname") or item.get("source"))
            authors = item.get("sortfirstauthor") or item.get("authors")
            author = clean_text(authors)
            snippet = " by ".join(part for part in (author, journal) if part)
            url = f"https://pubmed.ncbi.nlm.nih.gov/{uid}/" if uid else ""
            doi: str | None = None
            article_ids = item.get("articleids")
            if isinstance(article_ids, list):
                for article_id in article_ids:
                    if not isinstance(article_id, dict):
                        continue
                    if clean_text(article_id.get("idtype")).lower() == "doi":
                        doi = normalize_doi(article_id.get("value"))
                        if doi:
                            break
            if doi is None:
                doi = normalize_doi(item.get("elocationid"))
            hit = build_hit(
                url=url,
                title=title,
                snippet=truncate(snippet),
                publisher=journal or "PubMed",
                published_at=parse_datetime(publication_date),
                source_type=SourceType.MEDICAL,
                tool_name=self.name,
                score=max(0.0, 1.0 - index * 0.1),
                doi=doi,
            )
            if hit is not None:
                hits.append(hit)
        return hits

    async def search(self, task: SearchTask) -> list[SearchHit]:
        if not task.query.strip():
            raise ToolError(
                "Query must not be empty.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        limit = max_results_from_filters(task.filters, default=5, maximum=10)
        search_data = await self._get(
            task,
            "esearch.fcgi",
            self._params(term=task.query, retmax=str(limit), sort="relevance"),
        )
        search_result = search_data.get("esearchresult") if isinstance(search_data, dict) else None
        if not isinstance(search_result, dict) or search_result.get("ERROR"):
            raise ToolError(
                "PubMed returned an unexpected payload.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        idlist = search_result.get("idlist")
        if not isinstance(idlist, list):
            raise ToolError(
                "PubMed returned an unexpected payload.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        ids = [clean_text(uid) for uid in idlist if clean_text(uid)]
        if not ids:
            return []
        summary_data = await self._get(
            task,
            "esummary.fcgi",
            self._params(id=",".join(ids), retmax=str(limit)),
        )
        return self._summary_hits(summary_data, ids, limit)

    async def health_check(self) -> bool:
        try:
            data = await self._get(
                None,
                "esearch.fcgi",
                self._params(term="health check", retmax="1"),
            )
        except ToolError:
            return False
        return isinstance(data, dict) and isinstance(data.get("esearchresult"), dict)
