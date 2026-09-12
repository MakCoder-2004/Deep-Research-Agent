"""Optional Jina Reader integration behind the same safe fetcher."""

from __future__ import annotations

from research_agent.errors import ErrorCategory, ExtractionError
from research_agent.extraction.contracts import ExtractionConfig, FetchedPage
from research_agent.extraction.fetcher import SafeFetcher
from research_agent.extraction.security import normalize_url, validate_url


class JinaReaderFallback:
    """Read a previously validated source through a configured Jina endpoint."""

    def __init__(self, fetcher: SafeFetcher, config: ExtractionConfig) -> None:
        self._fetcher = fetcher
        self._config = config

    async def read(self, url: str) -> FetchedPage:
        source = normalize_url(url)
        base = self._config.jina_reader_base_url.strip()
        try:
            base_url = validate_url(base)
            endpoint = validate_url(base_url.rstrip("/") + "/" + source.url)
        except ExtractionError as exc:
            raise ExtractionError(
                "The configured reader endpoint is not safe.",
                category=ErrorCategory.INVALID_REQUEST,
                url=source.url,
            ) from exc
        headers: dict[str, str] = {}
        api_key = self._config.jina_reader_api_key.get_secret_value()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        page = await self._fetcher.fetch(
            endpoint,
            allowed_mime_types=frozenset({"text/plain", "text/markdown", "text/html"}),
            headers=headers,
        )
        if not page.content.decode("utf-8", errors="replace").strip():
            raise ExtractionError(
                "The reader returned no extractable text.",
                category=ErrorCategory.EXTRACTION,
                url=source.url,
            )
        return page
