"""Discovery of candidate pages on configured official domains (M3.21)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from html.parser import HTMLParser
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

from research_agent.errors import ErrorCategory
from research_agent.models import SourceType
from research_agent.models.research import SearchHit, SearchTask
from research_agent.tools._common import (
    TOOL_USER_AGENT,
    AsyncHttpTool,
    build_hit,
    clean_text,
    coerce_url,
    domain_of,
    max_results_from_filters,
)
from research_agent.tools.base import ToolError

if TYPE_CHECKING:
    from research_agent.config import Settings

__all__ = ["OfficialDomainTool"]

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


class _LinkParser(HTMLParser):
    """Extract links and a useful title without executing page content."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.loc_parts: list[str] = []
        self._in_loc = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        elif tag.lower().endswith("loc"):
            self._in_loc = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower().endswith("loc"):
            self._in_loc = False

    def handle_data(self, data: str) -> None:
        if self._in_loc:
            self.loc_parts.append(data)


def _valid_domain(value: str) -> str | None:
    domain = value.strip().lower().rstrip(".")
    if not domain or ":" in domain or any(char.isspace() for char in domain):
        return None
    parsed = urlsplit(f"//{domain}")
    if parsed.hostname != domain or parsed.path or parsed.query or parsed.fragment:
        return None
    return domain


class OfficialDomainTool(AsyncHttpTool):
    """Find sitemap or homepage candidates restricted to configured domains.

    There is no universal government-site search API. The adapter therefore
    uses each configured site's sitemap, falling back to its homepage, and
    never returns a URL outside the configured hostname or its subdomains.
    """

    def __init__(
        self,
        domains: Iterable[str] = (),
        *,
        timeout_seconds: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(timeout_seconds=timeout_seconds, client=client)
        normalized = {_valid_domain(str(domain)) for domain in domains}
        self._domains = tuple(sorted(domain for domain in normalized if domain is not None))

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        timeout_seconds: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> OfficialDomainTool:
        """Build an adapter from ``Settings.OFFICIAL_ALLOWED_DOMAINS``."""

        return cls(
            settings.official_allowed_domains,
            timeout_seconds=timeout_seconds,
            client=client,
        )

    @property
    def name(self) -> str:
        return "official_domains"

    def _allowed(self, url: str, domain: str) -> bool:
        host = domain_of(url)
        return host == domain or host.endswith(f".{domain}")

    def _urls_from_response(self, response: httpx.Response, domain: str) -> list[str]:
        parser = _LinkParser()
        parser.feed(response.text)
        candidates = [*parser.links, *parser.loc_parts]
        urls: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            url = coerce_url(clean_text(candidate))
            if url is None or not self._allowed(url, domain) or url in seen:
                continue
            seen.add(url)
            urls.append(url)
        return urls

    def _rank_urls(self, urls: list[str], query: str, limit: int) -> list[str]:
        tokens = [token.casefold() for token in _WORD_RE.findall(query) if len(token) > 2]
        matching = [url for url in urls if any(token in url.casefold() for token in tokens)]
        remaining = [url for url in urls if url not in matching]
        return (matching + remaining)[:limit]

    async def _domain_urls(self, task: SearchTask, domain: str) -> list[str]:
        filters = {str(key).lower(): str(value) for key, value in task.filters.items()}
        path = filters.get("path", "").strip()
        if path and not path.startswith("/"):
            path = f"/{path}"
        endpoint = path or "/sitemap.xml"
        response = await self._request(
            task,
            "GET",
            f"https://{domain}{endpoint}",
            error_message="Official domain request",
            expected_status=(200, 404),
            headers={"Accept": "application/xml,text/html", "User-Agent": TOOL_USER_AGENT},
        )
        if response.status_code == 404 and not path:
            response = await self._request(
                task,
                "GET",
                f"https://{domain}/",
                error_message="Official domain homepage request",
                headers={"Accept": "text/html", "User-Agent": TOOL_USER_AGENT},
            )
        return self._urls_from_response(response, domain) or [f"https://{domain}/"]

    async def search(self, task: SearchTask) -> list[SearchHit]:
        if not task.query.strip():
            raise ToolError(
                "Query must not be empty.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        if not self._domains:
            raise ToolError(
                "No official domains are configured.",
                category=ErrorCategory.UNAVAILABLE,
                tool_name=self.name,
            )
        limit = max_results_from_filters(task.filters, default=5, maximum=10)
        hits: list[SearchHit] = []
        per_domain = max(1, (limit + len(self._domains) - 1) // len(self._domains))
        for domain in self._domains:
            urls = await self._domain_urls(task, domain)
            for index, url in enumerate(self._rank_urls(urls, task.query, per_domain)):
                hit = build_hit(
                    url=url,
                    title=urlsplit(url).path.strip("/").replace("/", " / ") or domain,
                    snippet=f"Official source on {domain}.",
                    publisher=domain,
                    source_type=SourceType.OFFICIAL,
                    tool_name=self.name,
                    score=max(0.0, 1.0 - index * 0.1),
                )
                if hit is not None:
                    hits.append(hit)
        return hits[:limit]

    async def health_check(self) -> bool:
        if not self._domains:
            return False
        try:
            await self._request(
                None,
                "GET",
                f"https://{self._domains[0]}/",
                error_message="Official domain health request",
                headers={"Accept": "text/html", "User-Agent": TOOL_USER_AGENT},
            )
        except ToolError:
            return False
        return True
