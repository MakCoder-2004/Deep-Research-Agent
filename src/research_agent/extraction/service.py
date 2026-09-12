"""Public safe extraction service used by the later research graph."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import httpx

from research_agent.errors import ErrorCategory, ExtractionError
from research_agent.extraction.contracts import (
    AddressValidator,
    DNSResolver,
    DNSResolverCallable,
    ExtractionConfig,
    FetchedPage,
    ReaderFallback,
)
from research_agent.extraction.fallback import JinaReaderFallback
from research_agent.extraction.fetcher import SafeFetcher
from research_agent.extraction.html import HTMLExtractor
from research_agent.extraction.security import normalize_url
from research_agent.models.research import SourceCandidate, SourceDocument


class SafeExtractor:
    """Fetch and extract selected sources without persistent page storage."""

    def __init__(
        self,
        config: ExtractionConfig | None = None,
        *,
        settings: object | None = None,
        client: httpx.AsyncClient | None = None,
        test_transport: httpx.MockTransport | None = None,
        resolver: DNSResolver | DNSResolverCallable | None = None,
        address_validator: AddressValidator | None = None,
        reader_fallback: ReaderFallback | None = None,
        html_extractor: HTMLExtractor | None = None,
    ) -> None:
        if config is not None and settings is not None:
            raise ValueError("Pass config or settings, not both.")
        if settings is not None:
            self.config = ExtractionConfig.from_settings(settings)
        else:
            self.config = config or ExtractionConfig()
        self.fetcher = SafeFetcher(
            self.config,
            client=client,
            test_transport=test_transport,
            resolver=resolver,
            address_validator=address_validator,
        )
        self.html_extractor = html_extractor or HTMLExtractor(self.config)
        self.reader_fallback = reader_fallback
        if self.reader_fallback is None and self.config.jina_reader_enabled:
            self.reader_fallback = JinaReaderFallback(self.fetcher, self.config)

    @property
    def client(self) -> httpx.AsyncClient:
        return self.fetcher.client

    async def __aenter__(self) -> SafeExtractor:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self.fetcher.close()

    async def extract(
        self,
        source: object,
        *,
        source_id: int | None = None,
    ) -> SourceDocument:
        """Extract one URL or M3 ``SourceCandidate`` into a bounded document."""
        url, resolved_source_id, source_title, publisher, published_at = _source_values(
            source, source_id
        )
        target = normalize_url(url)
        page = await self.fetcher.fetch(target.url)
        try:
            return self.html_extractor.extract(
                page,
                source_id=resolved_source_id,
                source_title=source_title,
                source_publisher=publisher,
                source_published_at=published_at,
            )
        except asyncio.CancelledError:
            raise
        except ExtractionError as exc:
            if (
                exc.category is not ErrorCategory.EXTRACTION
                or not self.config.jina_reader_enabled
                or self.reader_fallback is None
            ):
                raise
            return await self._fallback_document(
                target.url,
                resolved_source_id,
                source_title,
                publisher,
                published_at,
            )

    async def extract_source(
        self, source: object, *, source_id: int | None = None
    ) -> SourceDocument:
        """Compatibility alias for graph nodes that use an explicit verb."""
        return await self.extract(source, source_id=source_id)

    async def _fallback_document(
        self,
        source_url: str,
        source_id: int,
        source_title: str,
        publisher: str | None,
        published_at: datetime | None,
    ) -> SourceDocument:
        assert self.reader_fallback is not None
        try:
            page = await self.reader_fallback.read(source_url)
        except asyncio.CancelledError:
            raise
        except ExtractionError:
            raise
        except Exception as exc:
            raise ExtractionError(
                "The reader fallback failed.",
                category=ErrorCategory.UNAVAILABLE,
                url=source_url,
            ) from exc
        if not isinstance(page, FetchedPage) or not page.content.strip():
            raise ExtractionError(
                "The reader fallback returned no response content.",
                category=ErrorCategory.EXTRACTION,
                url=source_url,
            )
        document = self.html_extractor.extract(
            page,
            source_id=source_id,
            source_title=source_title,
            source_publisher=publisher,
            source_published_at=published_at,
            source_url=source_url,
        )
        return document.model_copy(update={"extraction_tool": "jina_reader", "fallback_used": True})


def _source_values(
    source: object,
    source_id: int | None,
) -> tuple[str, int, str, str | None, datetime | None]:
    if isinstance(source, SourceCandidate):
        return (
            str(source.canonical_url),
            source_id if source_id is not None else source.source_id,
            source.title,
            source.publisher,
            source.published_at,
        )
    url_value = getattr(source, "url", source)
    if not isinstance(url_value, (str, httpx.URL)):
        url_value = str(url_value)
    candidate_id: object = (
        source_id
        if source_id is not None
        else getattr(source, "source_id", getattr(source, "id", 1))
    )
    try:
        numeric_id = int(str(candidate_id))
    except (TypeError, ValueError) as exc:
        raise ExtractionError(
            "Source ID must be a positive integer.", category=ErrorCategory.INVALID_REQUEST
        ) from exc
    if numeric_id < 1:
        raise ExtractionError(
            "Source ID must be a positive integer.", category=ErrorCategory.INVALID_REQUEST
        )
    return (
        str(url_value),
        numeric_id,
        _title_value(getattr(source, "title", "")),
        _optional_str(getattr(source, "publisher", None)),
        getattr(source, "published_at", None),
    )


def _title_value(value: object) -> str:
    return value if isinstance(value, str) else ""


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None
