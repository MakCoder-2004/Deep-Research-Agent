"""GitHub repository, release, and issue search adapter (M3.19)."""

from __future__ import annotations

import httpx
from pydantic import SecretStr

from research_agent.errors import ErrorCategory
from research_agent.models import SourceType
from research_agent.models.research import SearchHit, SearchTask
from research_agent.tools._common import (
    TOOL_USER_AGENT,
    AsyncHttpTool,
    build_hit,
    clean_text,
    max_results_from_filters,
    parse_datetime,
    truncate,
)
from research_agent.tools.base import ToolError

__all__ = ["GithubTool"]

_DEFAULT_BASE_URL = "https://api.github.com"


class GithubTool(AsyncHttpTool):
    """Repository, release, and issue discovery via the GitHub API."""

    def __init__(
        self,
        *,
        token: str | SecretStr = "",
        timeout_seconds: float = 15.0,
        base_url: str = _DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        raw = token.get_secret_value() if isinstance(token, SecretStr) else token
        super().__init__(timeout_seconds=timeout_seconds, client=client)
        self._token = SecretStr(raw)
        self._base_url = base_url.rstrip("/")

    @property
    def name(self) -> str:
        return "github"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": TOOL_USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token.get_secret_value():
            headers["Authorization"] = f"Bearer {self._token.get_secret_value()}"
        return headers

    async def _get_json(self, task: SearchTask, url: str, params: dict[str, str]) -> object:
        response = await self._request(
            task,
            "GET",
            url,
            error_message="GitHub request",
            headers=self._headers(),
            params=params,
        )
        return self._json(response, message="GitHub")

    def _repo_hits(self, items: list[object], limit: int) -> list[SearchHit]:
        hits: list[SearchHit] = []
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue
            owner = item.get("owner")
            login = clean_text(owner.get("login")) if isinstance(owner, dict) else ""
            try:
                score = min(1.0, max(0.0, float(item.get("stargazers_count", 0)) / 10000))
            except (TypeError, ValueError):
                score = 0.0
            description = truncate(clean_text(item.get("description")))
            language = clean_text(item.get("language"))
            snippet = f"{description} [{language}]".strip() if language else description
            hit = build_hit(
                url=item.get("html_url"),
                title=item.get("full_name"),
                snippet=snippet,
                publisher=login or None,
                published_at=parse_datetime(item.get("created_at") or item.get("updated_at")),
                source_type=SourceType.REPOSITORY,
                tool_name=self.name,
                score=score,
            )
            if hit is not None:
                hits.append(hit)
        return hits

    def _issue_hits(self, items: list[object], limit: int) -> list[SearchHit]:
        hits: list[SearchHit] = []
        for index, item in enumerate(items[:limit]):
            if not isinstance(item, dict):
                continue
            user = item.get("user")
            login = clean_text(user.get("login")) if isinstance(user, dict) else ""
            hit = build_hit(
                url=item.get("html_url"),
                title=item.get("title"),
                snippet=item.get("body", ""),
                publisher=login or None,
                published_at=parse_datetime(item.get("created_at")),
                source_type=SourceType.TECHNICAL,
                tool_name=self.name,
                score=max(0.0, 1.0 - index * 0.1),
            )
            if hit is not None:
                hits.append(hit)
        return hits

    def _release_hits(self, releases: list[object], repo: str, login: str) -> list[SearchHit]:
        hits: list[SearchHit] = []
        for item in releases:
            if not isinstance(item, dict):
                continue
            tag = clean_text(item.get("tag_name"))
            release_name = clean_text(item.get("name"))
            title = f"{repo} {tag} {release_name}".strip()
            hit = build_hit(
                url=item.get("html_url"),
                title=title,
                snippet=item.get("body", ""),
                publisher=login or None,
                published_at=parse_datetime(item.get("published_at") or item.get("created_at")),
                source_type=SourceType.REPOSITORY,
                tool_name=self.name,
                score=0.8,
            )
            if hit is not None:
                hits.append(hit)
        return hits

    async def search(self, task: SearchTask) -> list[SearchHit]:
        if not task.query.strip():
            raise ToolError(
                "Query must not be empty.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        limit = max_results_from_filters(task.filters, default=5, maximum=10)
        kind = str(task.filters.get("kind", "repositories")).strip().lower()
        kind = {"repo": "repositories", "issue": "issues", "release": "releases"}.get(kind, kind)
        if kind not in ("repositories", "issues", "releases"):
            kind = "repositories"
        if kind == "issues":
            data = await self._get_json(
                task,
                f"{self._base_url}/search/issues",
                {"q": task.query, "per_page": str(limit), "sort": "updated", "order": "desc"},
            )
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise ToolError(
                    "GitHub returned an unexpected payload.",
                    category=ErrorCategory.INVALID_REQUEST,
                    tool_name=self.name,
                )
            return self._issue_hits(items, limit)
        data = await self._get_json(
            task,
            f"{self._base_url}/search/repositories",
            {
                "q": task.query,
                "per_page": str(min(limit, 10) if kind == "repositories" else 3),
                "sort": "stars",
                "order": "desc",
            },
        )
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ToolError(
                "GitHub returned an unexpected payload.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            )
        if kind == "repositories":
            return self._repo_hits(items, limit)
        hits: list[SearchHit] = []
        per_repo = max(1, limit // max(1, min(len(items), 3)))
        for repo in items[:3]:
            if not isinstance(repo, dict):
                continue
            full_name = clean_text(repo.get("full_name"))
            if not full_name or "/" not in full_name:
                continue
            owner = repo.get("owner")
            login = clean_text(owner.get("login")) if isinstance(owner, dict) else ""
            release_data = await self._get_json(
                task,
                f"{self._base_url}/repos/{full_name}/releases",
                {"per_page": str(per_repo)},
            )
            if isinstance(release_data, list):
                hits.extend(self._release_hits(release_data, full_name, login))
            if len(hits) >= limit:
                break
        return hits[:limit]

    async def health_check(self) -> bool:
        try:
            await self._request(
                None,
                "GET",
                f"{self._base_url}/rate_limit",
                error_message="GitHub health request",
                headers=self._headers(),
            )
        except ToolError:
            return False
        return True
