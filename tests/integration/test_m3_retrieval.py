"""Mocked English and Arabic retrieval pipeline verification for M3."""

from __future__ import annotations

import pytest

from research_agent.agents.analyzer import analyze_query
from research_agent.models.research import SearchTask
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
    ranked = execution.ranked_hits

    assert ranked
    assert len(ranked) == len({str(hit.url) for hit in ranked})
    assert all(hit.url.scheme in {"http", "https"} for hit in ranked)
    assert all("utm_" not in str(hit.url) for hit in ranked)
    assert [candidate.source_id for candidate in execution.source_candidates] == list(
        range(1, len(ranked) + 1)
    )
    assert [str(candidate.canonical_url) for candidate in execution.source_candidates] == [
        str(hit.url) for hit in ranked
    ]
    assert all(candidate.tool_names for candidate in execution.source_candidates)
    assert set(tools) == set(plan.tools_selected)
    assert all(tool.calls for tool in tools.values())
    assert {task.tool_name for tool in tools.values() for task in tool.calls} == set(tools)
    assert all(task.language is plan.language for tool in tools.values() for task in tool.calls)

    repeated = await router.execute_plan(plan, job_id=f"m3-repeat-{plan.language.value}")
    assert [str(hit.url) for hit in repeated.ranked_hits] == [str(hit.url) for hit in ranked]
    assert [candidate.source_id for candidate in repeated.source_candidates] == list(
        range(1, len(ranked) + 1)
    )
