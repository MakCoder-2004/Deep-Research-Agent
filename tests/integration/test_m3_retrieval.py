"""Mocked English and Arabic retrieval pipeline verification for M3."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from research_agent.agents.analyzer import analyze_query
from research_agent.models.research import SearchTask
from research_agent.ranking import deduplicate_hits, rank_sources
from research_agent.tools.router import ToolRouter


class _FakeSearchTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[SearchTask] = []

    async def search(self, task: SearchTask) -> list[dict[str, str]]:
        self.calls.append(task)
        safe_name = self.name.replace("_", "-")
        return [
            {
                "url": "https://evidence.example/climate?utm_source=mock",
                "title": f"{task.query} shared evidence",
                "content": "Shared evidence from a mocked adapter.",
                "publisher": "Mock publisher",
                "source_type": "official",
            },
            {
                "url": f"https://{safe_name}.example/source?b=2&a=1",
                "title": f"{task.query} {self.name} source",
                "content": "Independent mocked evidence.",
                "publisher": f"{self.name} publisher",
                "source_type": "web",
            },
        ]

    async def health_check(self) -> bool:
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "What are the latest climate policy updates?",
        "ما هي آخر أخبار سياسة المناخ؟",
    ],
)
async def test_mocked_analyze_route_execute_normalize_dedup_rank(query: str) -> None:
    plan = analyze_query(query)
    tools = {name: _FakeSearchTool(name) for name in plan.tools_selected}
    router = ToolRouter(tools, request_timeout_seconds=1.0)

    execution = await router.execute_plan(plan, job_id=f"m3-{plan.language.value}")
    deduplicated = deduplicate_hits(execution.hits)
    ranked = rank_sources(
        deduplicated,
        plan,
        now=datetime(2026, 3, 1, tzinfo=UTC),
    )

    assert ranked
    assert len(ranked) == len({str(hit.url) for hit in ranked})
    assert all(hit.url.scheme in {"http", "https"} for hit in ranked)
    assert all("utm_" not in str(hit.url) for hit in ranked)
    assert set(tools) == set(plan.tools_selected)
    assert all(tool.calls for tool in tools.values())
    assert {task.tool_name for tool in tools.values() for task in tool.calls} == set(tools)
    assert all(task.language is plan.language for tool in tools.values() for task in tool.calls)

    repeated = rank_sources(
        deduplicate_hits(execution.hits),
        plan,
        now=datetime(2026, 3, 1, tzinfo=UTC),
    )
    assert [str(hit.url) for hit in repeated] == [str(hit.url) for hit in ranked]
