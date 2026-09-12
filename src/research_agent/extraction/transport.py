"""DNS-pinned transport support for the shared httpx client."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

import httpcore
from httpcore._backends.auto import AutoBackend

__all__ = ["PinnedNetworkBackend", "pinned_route"]


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
        self._backend = AutoBackend()

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
