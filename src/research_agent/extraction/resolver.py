"""Asynchronous system DNS resolution with an injectable test seam."""

from __future__ import annotations

import asyncio
import socket

from research_agent.extraction.contracts import DNSResult


class SystemDNSResolver:
    """Resolve stream addresses off the event loop's blocking call path."""

    async def resolve(self, hostname: str, port: int) -> DNSResult:
        loop = asyncio.get_running_loop()
        results = await loop.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
        return tuple(result[4][0] for result in results)
