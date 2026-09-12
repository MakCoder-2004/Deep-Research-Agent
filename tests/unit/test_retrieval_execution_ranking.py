"""Quota-free tests for retrieval execution, normalization, and ranking."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import cast

import pytest

from research_agent.errors import ErrorCategory
from research_agent.models import Domain, Language, SourceType
from research_agent.models.research import ResearchPlan, SearchHit, SearchTask
from research_agent.persistence.database import connect, init_schema
from research_agent.persistence.repositories import JobRepository, ToolRunRepository
from research_agent.ranking import (
    canonicalize_hits,
    deduplicate_hits,
    rank_sources,
    score_hit,
    score_hit_breakdown,
)
from research_agent.tools.base import ToolError
from research_agent.tools.router import ToolAttempt, ToolRouter, normalize_hits, route_plan


def _task(name: str, query: str = "climate policy", **filters: str) -> SearchTask:
    return SearchTask(
        task_id=f"task-{name}-{len(filters)}",
        tool_name=name,
        query=query,
        language=Language.ENGLISH,
        filters=filters,
    )


def _hit(
    url: str,
    title: str,
    *,
    tool: str = "fake",
    source_type: SourceType = SourceType.WEB,
    publisher: str | None = "Example",
    published_at: datetime | None = None,
    snippet: str = "climate policy evidence",
    **extra: object,
) -> SearchHit:
    return SearchHit(
        url=url,
        title=title,
        snippet=snippet,
        publisher=publisher,
        published_at=published_at,
        source_type=source_type,
        tool_name=tool,
        **extra,
    )


class FakeTool:
    def __init__(
        self,
        name: str,
        result: object,
        *,
        delay: float = 0.0,
        provider: str | None = None,
        failure: BaseException | None = None,
    ) -> None:
        self.name = name
        self.result = result
        self.delay = delay
        self.provider = provider or name
        self.failure = failure
        self.active = 0
        self.max_active = 0

    async def search(self, task: SearchTask) -> list[SearchHit]:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.failure is not None:
                raise self.failure
            return cast(list[SearchHit], self.result)
        finally:
            self.active -= 1

    async def health_check(self) -> bool:
        return True


class FakeRecorder:
    def __init__(self) -> None:
        self.attempts: list[ToolAttempt] = []

    async def record(self, attempt: ToolAttempt) -> None:
        self.attempts.append(attempt)


class KeywordRecorder:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    async def record(self, **kwargs: object) -> None:
        self.records.append(kwargs)


def test_route_plan_honors_plan_priority_and_can_expand_variants() -> None:
    plan = ResearchPlan(
        query="climate policy",
        domain=Domain.NEWS,
        language=Language.ENGLISH,
        query_variants=["climate policy", "climate policy Arabic"],
        tools_selected=["gdelt", "brave_search", "tavily"],
        source_categories=["news", "web"],
    )
    tasks = route_plan(plan)
    assert [task.tool_name for task in tasks] == ["gdelt", "brave_search", "tavily"]
    assert [task.filters["source_priority"] for task in tasks] == ["0", "1", "2"]
    expanded = route_plan(plan, expand_variants=True)
    assert [task.query for task in expanded] == [
        "climate policy",
        "climate policy",
        "climate policy",
        "climate policy Arabic",
        "climate policy Arabic",
        "climate policy Arabic",
    ]


def test_route_plan_has_defined_priorities_for_all_domains() -> None:
    for domain in Domain:
        plan = ResearchPlan(query="a sufficiently detailed question", domain=domain)
        assert route_plan(plan)


@pytest.mark.asyncio
async def test_execution_is_concurrent_but_respects_provider_limit() -> None:
    first = FakeTool("first", [_hit("https://one.example/a", "One")], delay=0.03, provider="shared")
    second = FakeTool(
        "second", [_hit("https://two.example/b", "Two")], delay=0.03, provider="shared"
    )
    started = time.monotonic()
    result = await ToolRouter([first, second], provider_limits={"shared": 1}).execute(
        [_task("first"), _task("second")], job_id="job-1"
    )
    elapsed = time.monotonic() - started
    assert elapsed >= 0.05
    assert first.max_active == second.max_active == 1
    assert len(result.hits) == 2
    assert all(attempt.success for attempt in result.attempts)


@pytest.mark.asyncio
async def test_failed_and_unknown_tools_do_not_cancel_independent_requests() -> None:
    good = FakeTool("good", [_hit("https://good.example/a", "Good")], delay=0.01)
    bad = FakeTool(
        "bad",
        [],
        failure=ToolError(
            "api_key=super-secret",  # noqa: S106 - verifies recorder redaction
            category=ErrorCategory.AUTH,
            tool_name="bad",
        ),
    )
    recorder = FakeRecorder()
    result = await ToolRouter([good, bad], recorder=recorder).execute(
        [_task("good"), _task("bad"), _task("missing")], job_id="job-2"
    )
    assert [hit.title for hit in result.hits] == ["Good"]
    assert len(result.attempts) == 3
    assert result.successful_tools == ["good"]
    assert result.failed_tools == ["bad", "missing"]
    assert {attempt.tool_name for attempt in recorder.attempts} == {"good", "bad", "missing"}
    bad_attempt = next(attempt for attempt in result.attempts if attempt.tool_name == "bad")
    assert bad_attempt.error_category == ErrorCategory.AUTH.value
    assert "super-secret" not in (bad_attempt.error_message or "")
    assert "not registered" in next(
        attempt.error_message or "" for attempt in result.attempts if attempt.tool_name == "missing"
    )


@pytest.mark.asyncio
async def test_url_route_and_task_execution_aliases() -> None:
    plan = ResearchPlan(query="Read https://example.org/page", domain=Domain.GENERAL)
    assert plan.source_url is None
    assert route_plan(plan) == []
    tool = FakeTool("mapped", [_hit("https://mapped.example/result", "Mapped")])
    router = ToolRouter({"alias": tool})
    result = await router.execute_tasks([_task("alias")], job_id="job-alias")
    assert result.successful_tools == ["alias"]
    assert router.available_tools == ("alias", "mapped")


def test_deduplication_rejects_invalid_options_and_malformed_mappings() -> None:
    with pytest.raises(ValueError, match="title_similarity"):
        deduplicate_hits([], title_similarity=0)
    assert canonicalize_hits([{"url": "https://example.org", "title": "missing tool"}]) == []


@pytest.mark.asyncio
async def test_sqlite_recorder_persists_detailed_attempt_metadata() -> None:
    conn = await connect(":memory:")
    try:
        await init_schema(conn)
        await init_schema(conn)
        await JobRepository().create(conn, job_id="job-sqlite", user_id=1, query="q")
        tool = FakeTool("quota-tool", [_hit("https://example.org/q", "Quota")])
        tool.quota_metadata = {"remaining": 4, "api_key": "private-value"}
        result = await ToolRouter([tool], db_connection=conn).execute(
            [_task("quota-tool")], job_id="job-sqlite"
        )
        await conn.commit()
        rows = await ToolRunRepository().list_by_job(conn, "job-sqlite")
        assert result.attempts[0].success
        assert rows[0]["task_id"] == result.attempts[0].task_id
        assert rows[0]["result_count"] == 1
        assert '"remaining": 4' in rows[0]["quota_metadata"]
        assert "private-value" not in rows[0]["quota_metadata"]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_cancelled_tool_is_recorded_without_being_retried() -> None:
    cancelled = FakeTool("cancelled", [], failure=asyncio.CancelledError())
    recorder = KeywordRecorder()
    with pytest.raises(asyncio.CancelledError):
        await ToolRouter([cancelled], recorder=recorder).execute(
            [_task("cancelled")], job_id="job-cancelled"
        )
    assert recorder.records
    assert recorder.records[0]["cancelled"] is True
    assert recorder.records[0]["error_category"] == "cancelled"


@pytest.mark.asyncio
async def test_deadline_records_timeout_and_skips_malformed_individual_hits() -> None:
    raw = [
        {"url": "https://example.org/a?utm_source=x", "title": "Valid", "publisher": "Publisher"},
        {"url": "file:///unsafe", "title": "Invalid"},
        {"url": "https://example.org/b", "title": "Also valid", "content": "Details"},
    ]
    quick = FakeTool("quick", raw)
    result = await ToolRouter([quick], request_timeout_seconds=0.01).execute(
        [_task("quick")], job_id="job-3"
    )
    assert [hit.title for hit in result.hits] == ["Valid", "Also valid"]
    assert str(result.hits[0].url) == "https://example.org/a"
    assert result.hits[1].snippet == "Details"

    slow = FakeTool("slow", [], delay=0.03)
    timed = await ToolRouter([slow], request_timeout_seconds=0.005).execute(
        [_task("slow")], job_id="job-4"
    )
    assert timed.hits == []
    assert timed.attempts[0].error_category == ErrorCategory.TIMEOUT.value
    assert timed.attempts[0].result_count == 0


def test_normalization_preserves_metadata_and_skips_invalid_url() -> None:
    accessed = datetime(2026, 1, 2, tzinfo=UTC)
    hits = normalize_hits(
        [
            {
                "url": "https://example.org/item?gclid=123&b=2&a=1#fragment",
                "title": "Published item",
                "content": "Snippet",
                "publisher": "Original Publisher",
                "published_date": "2025-02-03T00:00:00Z",
                "accessed_at": accessed,
                "source_type": "paper",
                "tool_name": "source-adapter",
            },
            {"url": "not a url", "title": "skip"},
        ],
        "fallback-tool",
    )
    assert len(hits) == 1
    assert str(hits[0].url) == "https://example.org/item?a=1&b=2"
    assert hits[0].publisher == "Original Publisher"
    assert hits[0].published_at is not None
    assert hits[0].accessed_at == accessed
    assert hits[0].source_type is SourceType.PAPER
    assert hits[0].tool_name == "source-adapter"


def test_deduplication_merges_identity_keys_and_provenance() -> None:
    old = datetime(2024, 1, 1, tzinfo=UTC)
    newer = datetime(2025, 1, 1, tzinfo=UTC)
    hits = [
        _hit(
            "https://one.example/article?utm_medium=x",
            "Climate Policy Evidence",
            tool="web-one",
            published_at=old,
            snippet="same evidence",
            content_hash="same-content",
        ),
        _hit(
            "https://two.example/article",
            "Climate Policy Evidence!",
            tool="web-two",
            source_type=SourceType.OFFICIAL,
            publisher="Official agency",
            published_at=newer,
            snippet="same evidence",
            content_hash="same-content",
        ),
        _hit(
            "https://doi.org/10.1000/abc",
            "Unrelated paper",
            tool="paper",
            source_type=SourceType.PAPER,
        ),
        _hit(
            "https://papers.example/record",
            "DOI record",
            tool="crossref",
            source_type=SourceType.ACADEMIC,
            snippet="10.1000/abc",
        ),
    ]
    canonical = canonicalize_hits(hits)
    assert str(canonical[0].url) == "https://one.example/article"
    result = deduplicate_hits(hits)
    assert len(result) == 2
    merged = result[0]
    assert merged.source_type is SourceType.OFFICIAL
    assert merged.published_at == newer
    assert set(merged.tool_names) == {"web-one", "web-two"}


def test_scoring_prefers_primary_relevant_and_independent_sources() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    plan = ResearchPlan(
        query="climate policy",
        domain=Domain.MENA_LOCAL,
        language=Language.ARABIC,
        locality="mena",
        jurisdictions=["Egypt"],
        requires_freshness=True,
    )
    official = _hit(
        "https://ministry.gov.eg/climate-policy",
        "سياسة المناخ climate policy",
        source_type=SourceType.OFFICIAL,
        publisher="Egyptian ministry",
        published_at=datetime(2025, 12, 1, tzinfo=UTC),
    )
    generic = _hit(
        "https://blog.example.com/post",
        "Other topic",
        publisher="Blog",
        published_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    assert score_hit(official, plan, now=now) > score_hit(generic, plan, now=now)
    ranked = rank_sources([generic, official], plan, now=now)
    assert ranked[0].url == official.url
    assert ranked[0].score > 0
    breakdown = score_hit_breakdown(generic, plan, now=now, accessible=False)
    assert breakdown.inaccessible_content_penalty == 1.5
    assert breakdown.primary_source_bonus == 0
    assert score_hit(_hit("https://plain.example", "No title", publisher=None), "") >= 0
