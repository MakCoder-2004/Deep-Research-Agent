"""Small asynchronous robots.txt policy cache."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.robotparser import RobotFileParser

from research_agent.errors import ErrorCategory, ExtractionError
from research_agent.extraction.contracts import ExtractionConfig, FetchedPage
from research_agent.extraction.security import SafeURL

FetchRobots = Callable[[str, str], Awaitable[FetchedPage]]


@dataclass(frozen=True, slots=True)
class _CachedPolicy:
    parser: RobotFileParser | None
    allow_all: bool
    fetched_at: float

    def allows(self, user_agent: str, target_url: str) -> bool:
        if self.allow_all:
            return True
        return self.parser is not None and self.parser.can_fetch(user_agent, target_url)


class RobotsPolicy:
    """Fetch and cache only parsed robots policy, never the robots body."""

    def __init__(self, config: ExtractionConfig, fetch_robots: FetchRobots) -> None:
        self._config = config
        self._fetch_robots = fetch_robots
        self._cache: dict[tuple[str, str, int], _CachedPolicy] = {}

    async def allowed(self, target: SafeURL, user_agent: str | None = None) -> bool:
        effective_user_agent = user_agent or self._config.user_agent
        key = target.origin
        cached = self._cache.get(key)
        now = time.monotonic()
        ttl = self._config.robots_cache_ttl_seconds
        if cached is not None and (ttl is None or now - cached.fetched_at < ttl):
            return cached.allows(effective_user_agent, target.url)
        host_for_url = f"[{target.hostname}]" if ":" in target.hostname else target.hostname
        robots_url = f"{target.scheme}://{host_for_url}"
        if target.port != (80 if target.scheme == "http" else 443):
            robots_url += f":{target.port}"
        robots_url += "/robots.txt"
        try:
            page = await self._fetch_robots(robots_url, effective_user_agent)
        except ExtractionError as exc:
            # A missing robots file is handled by the HTTP status branch.  A
            # policy fetch failure otherwise fails closed rather than silently
            # bypassing an operator's robots rule.
            raise ExtractionError(
                "robots.txt could not be checked safely.",
                category=ErrorCategory.ROBOTS,
                url=target.url,
            ) from exc
        if page.status_code in (401, 403):
            policy = _CachedPolicy(parser=None, allow_all=False, fetched_at=now)
        elif page.status_code in (404, 410):
            policy = _CachedPolicy(parser=None, allow_all=True, fetched_at=now)
        elif 200 <= page.status_code < 300:
            parser = RobotFileParser()
            parser.parse(page.content.decode("utf-8", errors="replace").splitlines())
            policy = _CachedPolicy(parser=parser, allow_all=False, fetched_at=now)
        else:
            policy = _CachedPolicy(parser=None, allow_all=False, fetched_at=now)
        if ttl is not None:
            self._cache[key] = policy
        return policy.allows(effective_user_agent, target.url)
