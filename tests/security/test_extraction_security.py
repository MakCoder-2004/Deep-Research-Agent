"""SSRF and redirect security tests for the safe extraction boundary (M4.21-M4.23)."""

from __future__ import annotations

import httpx
import pytest

from research_agent.errors import ErrorCategory, ExtractionError
from research_agent.extraction import ExtractionConfig, SafeFetcher, validate_url
from research_agent.extraction.transport import PinnedNetworkBackend, pinned_route


class FakeResolver:
    def __init__(self, answers: dict[str, list[object]]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    async def resolve(self, hostname: str, port: int) -> list[object]:
        self.calls.append(hostname)
        return self.answers[hostname]


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "http://user:password@example.com/file",
        "http://localhost/file",
        "http://127.0.0.1/file",
        "http://10.0.0.1/file",
        "http://[::1]/file",
        "http://[fc00::1]/file",
        "http://169.254.1.1/file",
        "http://[fe80::1]/file",
        "http://224.0.0.1/file",
        "http://[ff02::1]/file",
        "http://0.0.0.0/file",
        "http://[::]/file",
        "http://192.0.2.1/file",
        "http://100.100.100.200/file",
        "http://169.254.169.254/latest/meta-data",
    ],
)
def test_url_policy_rejects_prohibited_targets(url: str) -> None:
    with pytest.raises(ExtractionError) as raised:
        validate_url(url)
    assert raised.value.category in {
        ErrorCategory.INVALID_REQUEST,
        ErrorCategory.SSRF,
    }


def test_url_policy_normalizes_http_urls_without_credentials() -> None:
    assert (
        validate_url("HTTPS://Example.com:443/research#ignored") == "https://example.com/research"
    )


@pytest.mark.asyncio
async def test_every_dns_answer_is_validated_before_request() -> None:
    resolver = FakeResolver({"mixed.example": ["93.184.216.34", "127.0.0.1"]})
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<p>ok</p>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = SafeFetcher(
        ExtractionConfig(respect_robots_txt=False), client=client, resolver=resolver
    )
    try:
        with pytest.raises(ExtractionError) as raised:
            await fetcher.fetch("https://mixed.example/page")
    finally:
        await client.aclose()
    assert raised.value.category is ErrorCategory.SSRF
    assert not requests


@pytest.mark.asyncio
async def test_redirect_revalidates_destination_and_blocks_dns_rebinding() -> None:
    resolver = FakeResolver(
        {
            "start.example": ["93.184.216.34"],
            "rebound.example": ["127.0.0.1"],
        }
    )
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "https://rebound.example/private"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = SafeFetcher(
        ExtractionConfig(respect_robots_txt=False), client=client, resolver=resolver
    )
    try:
        with pytest.raises(ExtractionError) as raised:
            await fetcher.fetch("https://start.example/page")
    finally:
        await client.aclose()
    assert raised.value.category is ErrorCategory.SSRF
    assert requested == ["https://start.example/page"]
    assert resolver.calls == ["start.example", "rebound.example"]


@pytest.mark.asyncio
async def test_redirect_limit_is_hard_and_manual() -> None:
    resolver = FakeResolver({"example.com": ["93.184.216.34"]})
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(302, headers={"location": "https://example.com/again"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = SafeFetcher(
        ExtractionConfig(max_redirects=1, respect_robots_txt=False),
        client=client,
        resolver=resolver,
    )
    try:
        with pytest.raises(ExtractionError) as raised:
            await fetcher.fetch("https://example.com/start")
    finally:
        await client.aclose()
    assert raised.value.category is ErrorCategory.REDIRECT
    assert requests == 2


@pytest.mark.asyncio
async def test_transport_connects_to_the_validated_ip_not_a_rebound_hostname() -> None:
    calls: list[str] = []

    class Backend:
        async def connect_tcp(self, host: str, port: int, **kwargs: object) -> object:
            calls.append(host)
            return object()

    backend = PinnedNetworkBackend()
    backend._backend = Backend()  # type: ignore[assignment]
    with pinned_route("rebound.example", ("93.184.216.34",)):
        await backend.connect_tcp("rebound.example", 443)
    assert calls == ["93.184.216.34"]
