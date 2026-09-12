"""Focused close-out coverage for M3.23-M3.35 retrieval behavior."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from research_agent.errors import ErrorCategory
from research_agent.models import Domain, Language, SourceType
from research_agent.models.research import ResearchPlan, SearchHit, SearchTask
from research_agent.ranking import rank_sources, score_hit_breakdown
from research_agent.tools.router import ToolAttempt, ToolRouter, route_plan


def _task(tool_name: str, *, task_id: str | None = None) -> SearchTask:
    return SearchTask(
        task_id=task_id or f"task-{tool_name}",
        tool_name=tool_name,
        query="climate policy",
        language=Language.ENGLISH,
    )


class _Recorder:
    def __init__(self) -> None:
        self.attempts: list[ToolAttempt] = []

    async def record(self, attempt: ToolAttempt) -> None:
        self.attempts.append(attempt)


class _SlowTool:
    name = "slow"

    async def search(self, task: SearchTask) -> list[SearchHit]:
        await asyncio.sleep(1)
        return []

    async def health_check(self) -> bool:
        return True


@pytest.mark.parametrize(
    ("domain", "first_tool"),
    [
        (Domain.GENERAL, "tavily"),
        (Domain.ACADEMIC, "semantic_scholar"),
        (Domain.MEDICAL, "pubmed"),
        (Domain.NEWS, "gdelt"),
        (Domain.TECHNICAL, "official_domains"),
        (Domain.MENA_LOCAL, "brave_search"),
        (Domain.LEGAL, "official_domains"),
        (Domain.FINANCIAL, "official_domains"),
    ],
)
def test_domain_routing_uses_plan_defined_priorities(domain: Domain, first_tool: str) -> None:
    plan = ResearchPlan(query="a sufficiently detailed research question", domain=domain)

    tasks = route_plan(plan)

    assert tasks
    assert tasks[0].tool_name == first_tool
    assert [task.filters["source_priority"] for task in tasks] == [
        str(index) for index in range(len(tasks))
    ]


@pytest.mark.asyncio
async def test_global_deadline_records_timeout_attempt() -> None:
    recorder = _Recorder()
    result = await ToolRouter(
        [_SlowTool()],
        request_timeout_seconds=None,
        recorder=recorder,
    ).execute([_task("slow")], job_id="deadline-job", timeout_seconds=0.01)

    assert result.hits == []
    assert len(result.attempts) == 1
    assert result.attempts[0].success is False
    assert result.attempts[0].error_category == ErrorCategory.TIMEOUT.value
    assert result.attempts[0].cancelled is False
    assert recorder.attempts == result.attempts


def _hit(
    url: str,
    title: str,
    *,
    source_type: SourceType = SourceType.WEB,
    tool_name: str = "fake",
    published_at: datetime | None = None,
) -> SearchHit:
    return SearchHit(
        url=url,
        title=title,
        snippet="climate policy evidence",
        publisher="Example publisher",
        published_at=published_at,
        source_type=source_type,
        tool_name=tool_name,
    )


def test_source_scoring_keeps_primary_bonus_independent_of_tool_and_rewards_corroboration() -> None:
    official = _hit(
        "https://ministry.gov.eg/climate-policy",
        "Climate policy",
        source_type=SourceType.OFFICIAL,
        tool_name="tavily",
    )
    plan = ResearchPlan(
        query="climate policy",
        domain=Domain.MENA_LOCAL,
        language=Language.ENGLISH,
        locality="mena",
        jurisdictions=["Egypt"],
    )

    via_tavily = score_hit_breakdown(official, plan, corroboration_count=0)
    via_brave = score_hit_breakdown(
        official.model_copy(update={"tool_name": "brave_search"}),
        plan,
        corroboration_count=0,
    )
    corroborated = score_hit_breakdown(official, plan, corroboration_count=1)

    assert via_tavily.primary_source_bonus == 1.1
    assert via_brave.primary_source_bonus == via_tavily.primary_source_bonus
    assert corroborated.corroboration_potential > via_tavily.corroboration_potential


def test_rank_sources_is_stable_and_does_not_mutate_input() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    hits = [
        _hit("https://example.org/second", "Climate policy", tool_name="second"),
        _hit("https://example.org/first", "Climate policy", tool_name="first"),
    ]
    before = [hit.model_dump(mode="json") for hit in hits]

    ranked = rank_sources(hits, "climate policy", now=now)

    assert [hit.model_dump(mode="json") for hit in hits] == before
    assert [str(hit.url) for hit in ranked] == [
        "https://example.org/second",
        "https://example.org/first",
    ]
