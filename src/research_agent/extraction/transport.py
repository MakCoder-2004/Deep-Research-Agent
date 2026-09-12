"""DNS-pinned transport support for the shared httpx client."""

from __future__ import annotations

import ssl
import typing
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

import httpcore
import httpx

__all__ = ["PinnedAsyncHTTPTransport", "PinnedNetworkBackend", "pinned_route"]


@dataclass(frozen=True, slots=True)
class _PinnedRoute:
    hostname: str
    addresses: tuple[str, ...]


_CURRENT_ROUTE: ContextVar[_PinnedRoute | None] = ContextVar("safe_extraction_route", default=None)


@contextmanager
def pinned_route(hostname: str, addresses: tuple[str, ...]) -> Iterator[None]:
    """Pin one request task to addresses validated immediately before sending."""
    token: Token[_PinnedRoute | None] = _CURRENT_ROUTE.set(
        _PinnedRoute(hostname.casefold(), addresses)
    )
    try:
        yield
    finally:
        _CURRENT_ROUTE.reset(token)


class PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connect to validated IPs while retaining hostname-based HTTP/TLS identity.

    httpcore still receives the original hostname, so the ``Host`` header and
    TLS SNI/certificate verification remain correct.  Only the TCP destination
    is replaced.  The context-local route prevents a mutable shared map from
    introducing a cross-request race.
    """

    def __init__(self) -> None:
        self._backend = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore contract
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        route = _CURRENT_ROUTE.get()
        if route is None or route.hostname != host.casefold():
            raise httpcore.ConnectError("No validated DNS route is active for this connection.")
        last_error: Exception | None = None
        for address in route.addresses:
            try:
                return await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError("No validated DNS addresses are available.")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore contract
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("Unix sockets are not allowed by safe extraction.")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _AsyncResponseStream(httpx.AsyncByteStream):
    """Adapt an httpcore response stream to the httpx transport contract."""

    def __init__(self, stream: typing.AsyncIterable[bytes]) -> None:
        self._stream = stream

    async def __aiter__(self) -> typing.AsyncIterator[bytes]:
        try:
            async for chunk in self._stream:
                yield chunk
        except _HTTPCORE_ERRORS as exc:
            raise _map_httpcore_exception(exc) from exc

    async def aclose(self) -> None:
        close = getattr(self._stream, "aclose", None)
        if callable(close):
            await close()


class PinnedAsyncHTTPTransport(httpx.AsyncBaseTransport):
    """HTTPX transport backed by a DNS-pinned httpcore connection pool.

    HTTPX 0.28 does not expose ``network_backend`` on ``AsyncHTTPTransport``.
    Constructing the pool directly is the supported httpcore API and avoids
    falling back to proxy or environment-based DNS behavior.
    """

    def __init__(self) -> None:
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            max_connections=20,
            max_keepalive_connections=10,
            keepalive_expiry=5.0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=PinnedNetworkBackend(),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        httpcore_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=typing.cast(typing.AsyncIterable[bytes], request.stream),
            extensions=request.extensions,
        )
        try:
            response = await self._pool.handle_async_request(httpcore_request)
        except _HTTPCORE_ERRORS as exc:
            raise _map_httpcore_exception(exc) from exc
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_AsyncResponseStream(typing.cast(typing.AsyncIterable[bytes], response.stream)),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


_HTTPCORE_ERRORS = (
    httpcore.TimeoutException,
    httpcore.NetworkError,
    httpcore.ProxyError,
    httpcore.UnsupportedProtocol,
    httpcore.ProtocolError,
)


def _map_httpcore_exception(exc: Exception) -> httpx.HTTPError:
    """Keep transport failures on httpx's public exception taxonomy."""
    mappings: tuple[tuple[type[Exception], type[httpx.HTTPError]], ...] = (
        (httpcore.ConnectTimeout, httpx.ConnectTimeout),
        (httpcore.ReadTimeout, httpx.ReadTimeout),
        (httpcore.WriteTimeout, httpx.WriteTimeout),
        (httpcore.PoolTimeout, httpx.PoolTimeout),
        (httpcore.TimeoutException, httpx.TimeoutException),
        (httpcore.ConnectError, httpx.ConnectError),
        (httpcore.ReadError, httpx.ReadError),
        (httpcore.WriteError, httpx.WriteError),
        (httpcore.ProxyError, httpx.ProxyError),
        (httpcore.UnsupportedProtocol, httpx.UnsupportedProtocol),
        (httpcore.LocalProtocolError, httpx.LocalProtocolError),
        (httpcore.RemoteProtocolError, httpx.RemoteProtocolError),
        (httpcore.ProtocolError, httpx.ProtocolError),
        (httpcore.NetworkError, httpx.NetworkError),
    )
    for source, target in mappings:
        if isinstance(exc, source):
            return target(str(exc))
    return httpx.HTTPError(str(exc))
