"""Quota-free tests for retrieval execution, normalization, and ranking."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import cast

import pytest
from pydantic import ValidationError

from research_agent.config import Settings
from research_agent.errors import ErrorCategory
from research_agent.models import AttemptOutcome, Domain, Language, SourceType
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


def _task(
    name: str,
    query: str = "climate policy",
    *,
    task_id: str | None = None,
    **filters: str,
) -> SearchTask:
    return SearchTask(
        task_id=task_id or f"task-{name}-{len(filters)}",
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
    def __init__(self, *, delay: float = 0.0) -> None:
        self.attempts: list[ToolAttempt] = []
        self.delay = delay

    async def record(self, attempt: ToolAttempt) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
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
async def test_execution_result_runs_ranked_candidate_pipeline_without_mutation() -> None:
    first = _hit(
        "https://example.org/article?utm_source=first",
        "Climate policy evidence",
        tool="first",
        snippet="climate policy evidence from the first adapter",
    )
    duplicate = _hit(
        "https://example.org/article?utm_medium=second",
        "Climate policy evidence!",
        tool="second",
        source_type=SourceType.OFFICIAL,
        publisher="Official source",
        snippet="climate policy evidence from the second adapter",
    )
    independent = _hit(
        "https://independent.example/report",
        "Independent climate report",
        tool="second",
        snippet="independent climate policy evidence",
    )
    raw_hits = [first, duplicate, independent]
    before = [hit.model_dump(mode="json") for hit in raw_hits]
    first_tool = FakeTool("first", [raw_hits[0]])
    second_tool = FakeTool("second", [raw_hits[1], raw_hits[2]])

    result = await ToolRouter([first_tool, second_tool]).execute(
        [_task("first"), _task("second")],
        plan=ResearchPlan(
            query="climate policy",
            tools_selected=["first", "second"],
            source_budget=4,
            task_budget=2,
        ),
    )

    assert len(result.hits) == 3
    assert len(result.ranked_hits) == 2
    assert [candidate.source_id for candidate in result.source_candidates] == [1, 2]
    assert result.ranked_hits[0].source_type is SourceType.OFFICIAL
    assert str(result.source_candidates[0].canonical_url) == "https://example.org/article"
    assert [str(candidate.canonical_url) for candidate in result.source_candidates] == [
        str(hit.url) for hit in result.ranked_hits
    ]
    merged = next(
        hit for hit in result.ranked_hits if str(hit.url) == "https://example.org/article"
    )
    assert merged.tool_name == "second"
    assert set(merged.tool_names) == {"first", "second"}
    assert [hit.model_dump(mode="json") for hit in raw_hits] == before


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
    recorder = FakeRecorder(delay=0.01)
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


@pytest.mark.asyncio
async def test_execution_hard_caps_tasks_queries_and_results_from_plan() -> None:
    class UnboundedTool(FakeTool):
        async def search(self, task: SearchTask) -> list[SearchHit]:
            self.calls = getattr(self, "calls", [])
            self.calls.append(task)
            return [
                _hit(f"https://results.example/{index}", f"Result {index}") for index in range(20)
            ]

    tool = UnboundedTool("bounded", [])
    plan = ResearchPlan(
        query="bounded query",
        source_budget=3,
        query_budget=1,
        variant_budget=1,
        task_budget=20,
    )
    tasks = [
        SearchTask(
            task_id=f"bounded-{index}",
            tool_name="bounded",
            query="bounded query",
            language=Language.ENGLISH,
            filters={"max_results": "10"},
        )
        for index in range(20)
    ]

    result = await ToolRouter([tool]).execute(tasks, plan=plan, job_id="bounded-job")

    assert len(tool.calls) == 3
    assert len(result.hits) == 3
    assert sum(attempt.result_count for attempt in result.attempts) == 3
    assert [attempt.task_index for attempt in result.attempts] == [0, 1, 2]


@pytest.mark.asyncio
async def test_execution_uses_settings_caps_for_untrusted_direct_tasks() -> None:
    settings = Settings(
        _env_file=None,
        SOURCES_PER_JOB=2,
        SOURCE_BUDGET=2,
        SEARCH_SUBQUERIES_PER_JOB=1,
        SEARCH_VARIANTS_PER_JOB=1,
        SEARCH_TASKS_PER_JOB=5,
        TASK_BUDGET=5,
    )
    tool = FakeTool(
        "settings-bounded",
        [_hit("https://settings.example/a", "A"), _hit("https://settings.example/b", "B")],
    )
    tasks = [
        SearchTask(
            task_id=f"settings-{index}",
            tool_name="settings-bounded",
            query="same settings query",
            language=Language.ENGLISH,
        )
        for index in range(20)
    ]

    result = await ToolRouter([tool], settings=settings).execute(tasks)

    assert len(result.attempts) == 2
    assert len(tool.result) == 2
    assert len(result.hits) == 2


@pytest.mark.asyncio
async def test_settings_provider_limit_is_shared_across_tools() -> None:
    settings = Settings(_env_file=None, SEARCH_PROVIDER_CONCURRENCY=1, SEARCH_TOOL_CONCURRENCY=2)
    first = FakeTool(
        "settings-first",
        [_hit("https://settings.example/one", "One")],
        delay=0.03,
        provider="shared",
    )
    second = FakeTool(
        "settings-second",
        [_hit("https://settings.example/two", "Two")],
        delay=0.03,
        provider="shared",
    )
    started = time.monotonic()

    await ToolRouter([first, second], settings=settings).execute(
        [_task("settings-first"), _task("settings-second")]
    )

    assert time.monotonic() - started >= 0.05
    assert first.max_active == second.max_active == 1


@pytest.mark.asyncio
async def test_cancellation_stops_workers_and_records_all_cancelled_attempts() -> None:
    class BlockingTool:
        name = "blocking"
        provider = "blocking-provider"

        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = 0

        async def search(self, task: SearchTask) -> list[SearchHit]:
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise

        async def health_check(self) -> bool:
            return True

    tool = BlockingTool()
    recorder = FakeRecorder(delay=0.01)
    execution = asyncio.create_task(
        ToolRouter([tool], recorder=recorder).execute(
            [
                _task("blocking", query="one", task_id="blocking-one"),
                _task("blocking", query="two", task_id="blocking-two"),
            ],
            job_id="cancel-job",
        )
    )
    await asyncio.wait_for(tool.started.wait(), timeout=1)
    execution.cancel()

    with pytest.raises(asyncio.CancelledError):
        await execution
    assert tool.cancelled == 1
    assert len(recorder.attempts) == 2
    assert all(attempt.outcome is AttemptOutcome.CANCELLED for attempt in recorder.attempts)
    assert all(attempt.cancelled for attempt in recorder.attempts)


@pytest.mark.asyncio
async def test_malformed_top_level_payload_is_a_failed_attempt() -> None:
    malformed = FakeTool("malformed", {"results": []})

    result = await ToolRouter([malformed]).execute([_task("malformed")])

    assert result.hits == []
    assert len(result.attempts) == 1
    assert result.attempts[0].success is False
    assert result.attempts[0].outcome is AttemptOutcome.FAILURE
    assert result.attempts[0].error_category is ErrorCategory.INVALID_REQUEST


@pytest.mark.asyncio
async def test_tool_error_attempt_keeps_http_retry_metadata() -> None:
    limited = FakeTool(
        "limited",
        [],
        failure=ToolError(
            "rate limited",
            category=ErrorCategory.RATE_LIMITED,
            tool_name="limited",
            http_status=429,
            retry_after=2.5,
        ),
    )

    result = await ToolRouter([limited]).execute([_task("limited")])

    attempt = result.attempts[0]
    assert attempt.outcome is AttemptOutcome.FAILURE
    assert attempt.http_status == 429
    assert attempt.retry_after == 2.5


@pytest.mark.asyncio
async def test_recorder_failure_is_not_silently_discarded() -> None:
    class FailingRecorder:
        def __init__(self) -> None:
            self.calls = 0

        async def record(self, attempt: ToolAttempt) -> None:
            self.calls += 1
            raise RuntimeError("recorder unavailable")

    recorder = FailingRecorder()
    tasks = [
        _task("record-fail", query="one", task_id="record-fail-one"),
        _task("record-fail", query="two", task_id="record-fail-two"),
    ]

    with pytest.raises(RuntimeError, match="recorder unavailable"):
        await ToolRouter([FakeTool("record-fail", [])], recorder=recorder).execute(tasks)
    assert recorder.calls == 2


def test_tool_attempt_validates_outcomes_and_nonnegative_measurements() -> None:
    with pytest.raises(ValidationError):
        ToolAttempt(tool_name="tool", success=True, duration_ms=-1)
    with pytest.raises(ValidationError):
        ToolAttempt(tool_name="tool", success=False, error_category="not-a-category")
    with pytest.raises(ValidationError):
        ToolAttempt(
            tool_name="tool",
            success=False,
            outcome=AttemptOutcome.CANCELLED,
            cancelled=False,
        )
    timeout = ToolAttempt(tool_name="tool", success=False, error_category=ErrorCategory.TIMEOUT)
    assert timeout.outcome is AttemptOutcome.TIMEOUT


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
    assert hits[0].tool_name == "fallback-tool"


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
