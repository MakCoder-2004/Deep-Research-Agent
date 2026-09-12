"""Safe asynchronous HTTP fetching with DNS pinning and bounded buffering."""

from __future__ import annotations

import asyncio
import ipaddress
import time
from collections.abc import Mapping, Sequence
from urllib.parse import urljoin

import httpx

from research_agent.errors import ErrorCategory, ExtractionError
from research_agent.extraction.contracts import (
    AddressValidator,
    DNSResolver,
    DNSResolverCallable,
    ExtractionConfig,
    FetchedPage,
)
from research_agent.extraction.resolver import SystemDNSResolver
from research_agent.extraction.robots import RobotsPolicy
from research_agent.extraction.security import (
    SafeURL,
    normalize_url,
    validate_resolved_addresses,
)
from research_agent.extraction.transport import PinnedAsyncHTTPTransport, pinned_route
from research_agent.tools._common import http_status_category

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_CREDENTIAL_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "api-key",
        "x-api-key",
        "x-auth-token",
        "x-jina-api-key",
    }
)


class SafeFetcher:
    """One reusable async HTTP client behind the extraction safety boundary."""

    def __init__(
        self,
        config: ExtractionConfig | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        test_transport: httpx.MockTransport | None = None,
        resolver: DNSResolver | DNSResolverCallable | None = None,
        address_validator: AddressValidator | None = None,
    ) -> None:
        if client is not None:
            raise ValueError(
                "AsyncClient injection is not supported; use test_transport for "
                "MockTransport tests."
            )
        if test_transport is not None and not isinstance(test_transport, httpx.MockTransport):
            raise ValueError("Only httpx.MockTransport may be injected through test_transport.")
        if test_transport is None and (resolver is not None or address_validator is not None):
            raise ValueError(
                "Resolver and address-validator injection is test-only; provide test_transport."
            )
        self.config = config or ExtractionConfig()
        self._resolver = resolver or SystemDNSResolver()
        self._address_validator = address_validator or validate_resolved_addresses
        self._client = self._build_client(test_transport)
        self._robots = RobotsPolicy(self.config, self._fetch_robots)
        self._closed = False

    @property
    def client(self) -> httpx.AsyncClient:
        """Expose the shared client for application lifecycle integration."""
        return self._client

    @staticmethod
    def _mime(value: str | None) -> str:
        return (value or "").split(";", 1)[0].strip().lower()

    def _build_client(self, test_transport: httpx.MockTransport | None) -> httpx.AsyncClient:
        timeout = httpx.Timeout(
            connect=self.config.connect_timeout_seconds,
            read=self.config.read_timeout_seconds,
            write=self.config.write_timeout_seconds,
            pool=self.config.pool_timeout_seconds,
        )
        transport = test_transport or PinnedAsyncHTTPTransport()
        return httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": self.config.user_agent, "Accept-Encoding": "identity"},
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def __aenter__(self) -> SafeFetcher:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        if not self._closed:
            await self._client.aclose()
        self._closed = True

    async def _resolve(self, target: SafeURL) -> tuple[str, ...]:
        try:
            ipaddress.ip_address(target.hostname)
        except ValueError:
            try:
                operation: object
                if callable(self._resolver):
                    operation = self._resolver(target.hostname, target.port)
                else:
                    operation = self._resolver.resolve(target.hostname, target.port)
                answers = await asyncio.wait_for(operation, timeout=self.config.dns_timeout_seconds)
            except asyncio.CancelledError:
                raise
            except TimeoutError as exc:
                raise ExtractionError(
                    "Hostname resolution timed out.",
                    category=ErrorCategory.TIMEOUT,
                    url=target.url,
                ) from exc
            except Exception as exc:
                raise ExtractionError(
                    "Hostname resolution failed.", category=ErrorCategory.DNS, url=target.url
                ) from exc
            values = _address_values(answers)
        else:
            values = [target.hostname]
        return self._address_validator(values)

    async def _fetch_robots(self, url: str) -> FetchedPage:
        return await self._fetch(
            url,
            allowed_mime_types=frozenset({"text/plain", "text/html"}),
            check_robots=False,
            max_response_bytes=self.config.robots_max_response_bytes,
            accepted_statuses=frozenset({401, 403, 404, 410}),
        )

    async def fetch(
        self,
        url: object,
        *,
        allowed_mime_types: frozenset[str] | set[str] | None = None,
        headers: Mapping[str, str] | None = None,
        check_robots: bool = True,
    ) -> FetchedPage:
        """Fetch a safe URL and return a bounded, temporary response buffer."""
        return await self._fetch(
            url,
            allowed_mime_types=(
                self.config.allowed_mime_types if allowed_mime_types is None else allowed_mime_types
            ),
            headers=headers,
            check_robots=check_robots,
            max_response_bytes=self.config.max_response_bytes,
            accepted_statuses=frozenset(),
        )

    async def _fetch(
        self,
        url: object,
        *,
        allowed_mime_types: frozenset[str] | set[str],
        headers: Mapping[str, str] | None = None,
        check_robots: bool,
        max_response_bytes: int,
        accepted_statuses: frozenset[int],
    ) -> FetchedPage:
        requested = normalize_url(url)
        current = requested
        redirects = 0
        request_headers = {"User-Agent": self.config.user_agent}
        if headers:
            request_headers.update(headers)
        # Never let HTTPX negotiate a compressed response: the extraction limit
        # is enforced on the bytes that arrive, before any decoder runs.
        request_headers["Accept-Encoding"] = "identity"
        allowed = frozenset(self._mime(value) for value in allowed_mime_types)
        self._client.cookies.clear()
        while True:
            addresses = await self._resolve(current)
            if (
                check_robots
                and self.config.respect_robots_txt
                and not await self._robots.allowed(current)
            ):
                raise ExtractionError(
                    "robots.txt disallows fetching this URL.",
                    category=ErrorCategory.ROBOTS,
                    url=current.url,
                )
            started = time.monotonic()
            try:
                with pinned_route(current.hostname, addresses):
                    async with self._client.stream(
                        "GET", current.url, headers=request_headers, follow_redirects=False
                    ) as response:
                        status = response.status_code
                        if status in REDIRECT_STATUSES:
                            location = response.headers.get("location")
                            if not location:
                                raise ExtractionError(
                                    "Redirect response did not include a Location header.",
                                    category=ErrorCategory.REDIRECT,
                                    url=current.url,
                                    http_status=status,
                                )
                            if redirects >= self.config.max_redirects:
                                raise ExtractionError(
                                    "The URL exceeded the redirect limit.",
                                    category=ErrorCategory.REDIRECT,
                                    url=current.url,
                                    http_status=status,
                                )
                            try:
                                next_url = normalize_url(urljoin(current.url, location))
                            except ExtractionError as exc:
                                if exc.category is ErrorCategory.INVALID_REQUEST:
                                    raise ExtractionError(
                                        "Redirect target is not a permitted URL.",
                                        category=ErrorCategory.REDIRECT,
                                        url=current.url,
                                        http_status=status,
                                    ) from exc
                                raise
                            if next_url.origin != current.origin:
                                request_headers = _strip_credentials(request_headers)
                            current = next_url
                            redirects += 1
                            continue
                        if status not in accepted_statuses and not 200 <= status < 300:
                            category = http_status_category(status, "", response.headers)
                            if category is ErrorCategory.UNKNOWN:
                                category = ErrorCategory.HTTP
                            raise ExtractionError(
                                "The source returned an HTTP error.",
                                category=category,
                                url=current.url,
                                http_status=status,
                            )
                        if status in accepted_statuses:
                            return FetchedPage(
                                requested_url=requested.url,
                                final_url=current.url,
                                status_code=status,
                                content_type=self._mime(response.headers.get("content-type")),
                                content=b"",
                                bytes_read=0,
                                fetch_ms=_elapsed_ms(started),
                            )
                        content_type = self._mime(response.headers.get("content-type"))
                        if content_type not in allowed:
                            raise ExtractionError(
                                "The source MIME type is not allowed for extraction.",
                                category=ErrorCategory.MIME,
                                url=current.url,
                                http_status=status,
                            )
                        _reject_encoded_response(response, current.url)
                        _check_content_length(
                            response.headers.get("content-length"), max_response_bytes
                        )
                        content = await _read_bounded(response, max_response_bytes, current.url)
                        return FetchedPage(
                            requested_url=requested.url,
                            final_url=current.url,
                            status_code=status,
                            content_type=content_type,
                            content=content,
                            bytes_read=len(content),
                            fetch_ms=_elapsed_ms(started),
                        )
            except asyncio.CancelledError:
                raise
            except ExtractionError:
                raise
            except httpx.TimeoutException as exc:
                raise ExtractionError(
                    "The source request timed out.",
                    category=ErrorCategory.TIMEOUT,
                    url=current.url,
                ) from exc
            except httpx.HTTPError as exc:
                raise ExtractionError(
                    "The source request failed.",
                    category=ErrorCategory.UNAVAILABLE,
                    url=current.url,
                ) from exc
            except Exception as exc:
                raise ExtractionError(
                    "The source request failed safely.",
                    category=ErrorCategory.HTTP,
                    url=current.url,
                ) from exc
            finally:
                # AsyncClient extracts Set-Cookie into its jar before returning
                # a response.  This fetcher never persists cookies between
                # requests, including redirects or separate source fetches.
                self._client.cookies.clear()


def _address_values(answers: Sequence[object]) -> list[object]:
    """Accept plain fake answers and ``getaddrinfo``-shaped answers."""
    values: list[object] = []
    for answer in answers:
        if isinstance(answer, str):
            values.append(answer)
            continue
        if isinstance(answer, (tuple, list)):
            if len(answer) >= 5 and isinstance(answer[4], (tuple, list)):
                values.append(answer[4][0])
            elif answer and isinstance(answer[0], str):
                values.append(answer[0])
                continue
        values.append(answer)
    return values


def _check_content_length(value: str | None, maximum: int) -> None:
    if value is None:
        return
    try:
        content_length = int(value)
    except (TypeError, ValueError) as exc:
        raise ExtractionError(
            "The response size header is invalid.", category=ErrorCategory.SIZE
        ) from exc
    if content_length < 0 or content_length > maximum:
        raise ExtractionError(
            "The source response exceeds the size limit.", category=ErrorCategory.SIZE
        )


def _strip_credentials(headers: Mapping[str, str]) -> dict[str, str]:
    """Keep ordinary request headers while removing origin-bound credentials."""
    return {
        name: value for name, value in headers.items() if name.casefold() not in _CREDENTIAL_HEADERS
    }


def _reject_encoded_response(response: httpx.Response, url: str) -> None:
    """Reject compressed bodies before HTTPX can inflate them in memory."""
    encodings = response.headers.get("content-encoding", "").strip().casefold()
    if encodings and encodings != "identity":
        raise ExtractionError(
            "Compressed source responses are not allowed by the size boundary.",
            category=ErrorCategory.SIZE,
            url=url,
        )


async def _read_bounded(response: httpx.Response, maximum: int, url: str) -> bytes:
    chunks: list[bytes] = []
    total = 0
    try:
        if response.is_stream_consumed:
            content = response.content
            if len(content) > maximum:
                raise ExtractionError(
                    "The source response exceeds the size limit.",
                    category=ErrorCategory.SIZE,
                    url=url,
                )
            return content
        async for chunk in response.aiter_raw():
            total += len(chunk)
            if total > maximum:
                raise ExtractionError(
                    "The source response exceeds the size limit.",
                    category=ErrorCategory.SIZE,
                    url=url,
                )
            chunks.append(chunk)
    except asyncio.CancelledError:
        raise
    except ExtractionError:
        raise
    except httpx.TimeoutException as exc:
        raise ExtractionError(
            "The source body timed out.", category=ErrorCategory.TIMEOUT, url=url
        ) from exc
    except httpx.HTTPError as exc:
        raise ExtractionError(
            "The source body could not be read.",
            category=ErrorCategory.UNAVAILABLE,
            url=url,
        ) from exc
    return b"".join(chunks)


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))
