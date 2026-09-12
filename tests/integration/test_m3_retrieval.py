"""Mocked English and Arabic retrieval pipeline verification for M3."""

from __future__ import annotations

import httpx
import pytest
import respx

from research_agent.agents.analyzer import analyze_query
from research_agent.models import Language
from research_agent.tools import BraveTool, GdeltTool, TavilyTool
from research_agent.tools.router import ToolRouter


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "language"),
    [
        ("What are the latest climate policy news updates?", Language.ENGLISH),
        ("ما هي آخر أخبار سياسة المناخ؟", Language.ARABIC),
    ],
)
@respx.mock
async def test_analyze_route_real_adapters_and_ranked_candidate_boundary(
    query: str, language: Language
) -> None:
    gdelt_route = respx.get("https://api.gdeltproject.org/api/v2/doc/doc").mock(
        return_value=httpx.Response(
            200,
            json={
                "articles": [
                    {
                        "url": "https://evidence.example/climate?utm_source=gdelt",
                        "title": "Climate policy evidence",
                        "snippet": "Shared evidence from GDELT.",
                        "domain": "evidence.example",
                        "seendate": "20260912090000",
                    },
                    {
                        "url": "https://gdelt.example/report?b=2&a=1",
                        "title": "GDELT climate policy report",
                        "snippet": "Independent current evidence.",
                        "domain": "gdelt.example",
                        "seendate": "20260911090000",
                    },
                ]
            },
        )
    )
    brave_route = respx.get("https://api.search.brave.com/res/v1/news/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://evidence.example/climate?utm_medium=brave",
                        "title": "Climate policy evidence",
                        "description": "Shared evidence from Brave.",
                        "meta_url": {"hostname": "evidence.example"},
                        "page_age": "2026-09-10",
                    },
                    {
                        "url": "https://brave.example/report",
                        "title": "Brave climate policy report",
                        "description": "Independent current evidence.",
                        "meta_url": {"hostname": "brave.example"},
                        "page_age": "2026-09-09",
                    },
                ]
            },
        )
    )
    tavily_route = respx.post("https://api.tavily.com/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://evidence.example/climate?utm_campaign=tavily",
                        "title": "Climate policy evidence",
                        "content": "Shared evidence from Tavily.",
                        "publisher": "evidence.example",
                        "published_date": "2026-09-08",
                    },
                    {
                        "url": "https://tavily.example/report",
                        "title": "Tavily climate policy report",
                        "content": "Independent current evidence.",
                        "publisher": "tavily.example",
                        "published_date": "2026-09-07",
                    },
                ],
            },
        )
    )

    plan = analyze_query(query)
    assert plan.language is language
    assert plan.tools_selected == ["gdelt", "brave_search", "tavily"]
    tools = [
        GdeltTool(),
        BraveTool(api_key="brave-test-key"),
        TavilyTool(api_key="tavily-test-key"),
    ]
    router = ToolRouter(tools, request_timeout_seconds=1.0)
    tasks = router.route_plan(plan)

    execution = await router.execute_plan(plan, job_id=f"m3-{plan.language.value}")
    ranked = execution.ranked_hits

    assert [task.tool_name for task in tasks] == plan.tools_selected
    assert all(task.language is plan.language for task in tasks)
    assert gdelt_route.called and brave_route.called and tavily_route.called
    assert brave_route.calls[0].request.url.params["search_lang"] == (
        "ar" if language is Language.ARABIC else "en"
    )
    assert ranked
    assert len(execution.hits) == 6
    assert len(ranked) == 4
    assert len(ranked) == len({str(hit.url) for hit in ranked})
    assert all(hit.url.scheme in {"http", "https"} for hit in ranked)
    assert all("utm_" not in str(hit.url) for hit in ranked)
    shared = next(hit for hit in ranked if str(hit.url) == "https://evidence.example/climate")
    assert shared.tool_name == "gdelt"
    assert shared.tool_names == ["gdelt", "brave_search", "tavily"]
    assert [candidate.source_id for candidate in execution.source_candidates] == list(
        range(1, len(ranked) + 1)
    )
    assert [str(candidate.canonical_url) for candidate in execution.source_candidates] == [
        str(hit.url) for hit in ranked
    ]
    assert all(candidate.tool_names for candidate in execution.source_candidates)
    shared_candidate = next(
        candidate
        for candidate in execution.source_candidates
        if str(candidate.canonical_url) == str(shared.url)
    )
    assert shared_candidate.tool_names == shared.tool_names
    assert [attempt.tool_name for attempt in execution.attempts] == plan.tools_selected
    assert all(attempt.success for attempt in execution.attempts)

    repeated = await router.execute_plan(plan, job_id=f"m3-repeat-{plan.language.value}")
    assert [str(hit.url) for hit in repeated.ranked_hits] == [str(hit.url) for hit in ranked]
    assert [candidate.source_id for candidate in repeated.source_candidates] == list(
        range(1, len(ranked) + 1)
    )
    assert gdelt_route.call_count == brave_route.call_count == tavily_route.call_count == 2
