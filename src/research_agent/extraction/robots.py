"""Small asynchronous robots.txt policy cache."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from urllib.robotparser import RobotFileParser

from research_agent.errors import ErrorCategory, ExtractionError
from research_agent.extraction.contracts import ExtractionConfig, FetchedPage
from research_agent.extraction.security import SafeURL

FetchRobots = Callable[[str], Awaitable[FetchedPage]]


class RobotsPolicy:
    """Fetch and cache only parsed robots policy, never the robots body."""

    def __init__(self, config: ExtractionConfig, fetch_robots: FetchRobots) -> None:
        self._config = config
        self._fetch_robots = fetch_robots
        self._cache: dict[tuple[str, str, int], bool] = {}

    async def allowed(self, target: SafeURL) -> bool:
        key = target.origin
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        host_for_url = f"[{target.hostname}]" if ":" in target.hostname else target.hostname
        robots_url = f"{target.scheme}://{host_for_url}"
        if target.port != (80 if target.scheme == "http" else 443):
            robots_url += f":{target.port}"
        robots_url += "/robots.txt"
        try:
            page = await self._fetch_robots(robots_url)
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
            allowed = False
        elif page.status_code in (404, 410):
            allowed = True
        elif 200 <= page.status_code < 300:
            parser = RobotFileParser()
            parser.parse(page.content.decode("utf-8", errors="replace").splitlines())
            allowed = parser.can_fetch(self._config.user_agent, target.url)
        else:
            allowed = False
        self._cache[key] = allowed
        return allowed
