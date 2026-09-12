"""Europe PMC biomedical literature search adapter (M3.20)."""

from __future__ import annotations

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

__all__ = ["EuropePMCTool", "EuropePmcTool"]

_DEFAULT_BASE_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest"


class EuropePMCTool(AsyncHttpTool):
    """Biomedical discovery and abstract metadata through Europe PMC."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        base_url: str = _DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(timeout_seconds=timeout_seconds, client=client)
        self._base_url = base_url.rstrip("/")

    @property
    def name(self) -> str:
        return "europe_pmc"

    def _result_url(self, item: dict[str, object]) -> str:
        doi = normalize_doi(item.get("doi"))
        pmcid = clean_text(item.get("pmcid"))
        pmid = clean_text(item.get("pmid") or item.get("id"))
        if doi:
            return doi_url(doi) or ""
        if pmcid:
            return f"https://europepmc.org/articles/{pmcid}"
        if pmid:
            return f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        return clean_text(item.get("journalInfo"))

    def _hits(self, data: object, limit: int) -> list[SearchHit]:
        result_list = data.get("resultList") if isinstance(data, dict) else None
        raw_results = result_list.get("result") if isinstance(result_list, dict) else None
        if not isinstance(raw_results, list):
            raise ToolError(
                "Europe PMC returned an unexpected payload.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        hits: list[SearchHit] = []
        for index, item in enumerate(raw_results[:limit]):
            if not isinstance(item, dict):
                continue
            abstract = clean_text(item.get("abstractText"))
            doi = normalize_doi(item.get("doi"))
            journal_info = item.get("journalInfo")
            journal = ""
            if isinstance(journal_info, dict):
                journal_node = journal_info.get("journal")
                if isinstance(journal_node, dict):
                    journal = clean_text(journal_node.get("title"))
            publisher = journal or clean_text(item.get("journalTitle")) or "Europe PMC"
            published_at = parse_datetime(item.get("firstPublicationDate"))
            if published_at is None:
                journal_info = item.get("journalInfo")
                if isinstance(journal_info, dict):
                    published_at = parse_datetime(
                        journal_info.get("printPublicationDate")
                        or journal_info.get("journalIssueDate")
                    )
            hit = build_hit(
                url=self._result_url(item),
                title=item.get("title"),
                snippet=truncate(abstract),
                publisher=publisher,
                published_at=published_at,
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
        response = await self._request(
            task,
            "GET",
            f"{self._base_url}/search",
            error_message="Europe PMC request",
            headers={"Accept": "application/json", "User-Agent": TOOL_USER_AGENT},
            params={
                "query": task.query,
                "format": "json",
                "resultType": "core",
                "pageSize": str(limit),
            },
        )
        return self._hits(self._json(response, message="Europe PMC"), limit)

    async def health_check(self) -> bool:
        try:
            await self._request(
                None,
                "GET",
                f"{self._base_url}/search",
                error_message="Europe PMC health request",
                headers={"Accept": "application/json", "User-Agent": TOOL_USER_AGENT},
                params={"query": "health check", "format": "json", "pageSize": "1"},
            )
        except ToolError:
            return False
        return True


# Keep both spellings available because the service name is commonly written
# as either Europe PMC or EuropePMCTool in integrations.
EuropePmcTool = EuropePMCTool
