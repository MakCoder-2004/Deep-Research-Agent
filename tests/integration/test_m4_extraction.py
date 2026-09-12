"""End-to-end mocked extraction contract test (M4 gate)."""

from __future__ import annotations

import httpx
import pytest

from research_agent.extraction import ExtractionConfig, SafeExtractor


class Resolver:
    async def resolve(self, hostname: str, port: int) -> list[str]:
        return ["93.184.216.34"]


@pytest.mark.asyncio
async def test_allowed_source_becomes_clean_bounded_document() -> None:
    html = b"<html><body><main><h1>Report</h1><p>Evidence from the source.</p></main></body></html>"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=html)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    extractor = SafeExtractor(
        ExtractionConfig(max_source_chars=80, respect_robots_txt=False),
        client=client,
        resolver=Resolver(),
    )
    try:
        document = await extractor.extract("https://example.com/report", source_id=1)
    finally:
        await client.aclose()
    assert document.source_id == 1
    assert document.body_text == "Report\nEvidence from the source."
    assert len(document.body_text) <= 80
    assert document.content_type == "text/html"
    assert document.status_code == 200
