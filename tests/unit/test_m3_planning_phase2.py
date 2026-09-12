"""Focused phase-2 planning, routing, and budget policy tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from research_agent.agents.analyzer import (
    analyze_query,
    analyze_request,
    build_query_variants,
    detect_locality,
    select_depth,
    select_tools,
)
from research_agent.config import Settings
from research_agent.errors import ClarificationRequiredError
from research_agent.models import Depth, Domain, Language, RiskLevel
from research_agent.models.requests import ResearchRequest
from research_agent.models.research import ResearchPlan, SearchTask
from research_agent.tools.router import SOURCE_PRIORITIES, ToolRouter, route_plan


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        (Domain.GENERAL, ("tavily", "brave_search", "exa", "searxng", "ddgs")),
        (Domain.ACADEMIC, ("semantic_scholar", "crossref", "arxiv", "openalex", "tavily")),
        (
            Domain.MEDICAL,
            (
                "pubmed",
                "europe_pmc",
                "official_domains",
                "semantic_scholar",
                "crossref",
                "openalex",
                "tavily",
            ),
        ),
        (Domain.NEWS, ("gdelt", "brave_search", "tavily")),
        (
            Domain.TECHNICAL,
            ("official_domains", "github", "stack_exchange", "tavily", "brave_search"),
        ),
        (Domain.MENA_LOCAL, ("brave_search", "tavily", "gdelt", "official_domains")),
        (Domain.LEGAL, ("official_domains", "tavily", "brave_search")),
        (Domain.FINANCIAL, ("official_domains", "tavily", "brave_search")),
    ],
)
def test_domain_priority_matrices_are_plan_ordered(
    domain: Domain, expected: tuple[str, ...]
) -> None:
    assert SOURCE_PRIORITIES[domain.value] == expected
    tools, _ = select_tools(domain, Depth.DEEP)
    assert tuple(tools) == expected[:6]


@pytest.mark.parametrize(
    ("query", "language"),
    [
        ("Read https://example.org/article).", Language.ENGLISH),
        ("اقرأ https://example.org/article؟", Language.ARABIC),
        ("https://example.org/article.", Language.MIXED),
    ],
)
def test_url_inputs_use_one_normalized_direct_target(query: str, language: Language) -> None:
    plan = analyze_query(query)

    assert str(plan.source_url) == "https://example.org/article"
    assert plan.language is language
    assert plan.tools_selected == []
    assert route_plan(plan, expand_variants=True) == []


def test_url_plus_text_request_does_not_append_the_structured_url() -> None:
    request = ResearchRequest(
        user_id=7,
        query="Explain this https://example.org/article).",
        source_url="https://example.org/article).",
        language=Language.ARABIC,
    )

    plan = analyze_request(request)

    assert plan.query.count("https://example.org/article") == 1
    assert str(plan.source_url) == "https://example.org/article"
    assert plan.language is Language.ENGLISH
    assert plan.requested_language is Language.ARABIC
    assert plan.output_language is Language.ARABIC
    assert route_plan(plan) == []


def test_clarification_is_an_execution_gate() -> None:
    plan = analyze_query("help")

    assert plan.needs_clarification is True
    assert route_plan(plan) == []
    with pytest.raises(ClarificationRequiredError, match="clarify"):
        route_plan(plan, raise_on_clarification=True)


@pytest.mark.asyncio
async def test_execute_plan_rejects_clarification_before_creating_tasks() -> None:
    with pytest.raises(ClarificationRequiredError, match="clarify"):
        await ToolRouter().execute_plan(analyze_query("help"))


@pytest.mark.asyncio
async def test_direct_execution_defensively_skips_clarification_plan() -> None:
    class MustNotRun:
        name = "must-not-run"

        async def search(self, task: SearchTask) -> list[object]:
            raise AssertionError("clarification plan executed a tool")

        async def health_check(self) -> bool:
            return True

    plan = analyze_query("help")
    task = SearchTask(task_id="should-not-run", tool_name="must-not-run", query="help")

    result = await ToolRouter([MustNotRun()]).execute([task], plan=plan)

    assert result.hits == []
    assert result.attempts == []


def test_requested_language_does_not_replace_detected_language() -> None:
    request = ResearchRequest(user_id=7, query="What is the climate policy?", language="ar")

    plan = analyze_request(request)

    assert plan.language is Language.ENGLISH
    assert plan.requested_language is Language.ARABIC
    assert any("\u0600" <= char <= "\u06ff" for item in plan.query_variants for char in item)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Oman", ["Oman"]),
        ("Jordan", ["Jordan"]),
        ("عمان", ["Oman"]),
        ("عمّان", ["Jordan"]),
    ],
)
def test_mena_aliases_have_no_oman_jordan_collision(text: str, expected: list[str]) -> None:
    locality, jurisdictions = detect_locality(text)

    assert locality == "mena"
    assert jurisdictions == expected


def test_regional_queries_use_deep_rules_and_bilingual_filters() -> None:
    plan = analyze_query("What is the economy of Oman?")
    tasks = route_plan(plan, expand_variants=True)

    assert plan.depth is Depth.DEEP
    assert plan.domain is Domain.FINANCIAL
    assert plan.query_budget <= 6
    assert any("\u0600" <= char <= "\u06ff" for item in plan.query_variants for char in item)
    assert all(task.filters["search_languages"] == "ar,en" for task in tasks)
    assert all(task.filters["region"] == "mena" for task in tasks)


@pytest.mark.parametrize(
    ("query", "domain", "risk", "freshness", "expected"),
    [
        ("What is photosynthesis?", Domain.GENERAL, RiskLevel.NORMAL, False, Depth.QUICK),
        ("Compare solar and wind energy", Domain.GENERAL, RiskLevel.NORMAL, False, Depth.STANDARD),
        (
            "What are the latest climate policy updates?",
            Domain.GENERAL,
            RiskLevel.NORMAL,
            True,
            Depth.STANDARD,
        ),
        ("What is the economy of Oman?", Domain.FINANCIAL, RiskLevel.NORMAL, False, Depth.DEEP),
    ],
)
def test_depth_boundaries_follow_plan(
    query: str,
    domain: Domain,
    risk: RiskLevel,
    freshness: bool,
    expected: Depth,
) -> None:
    assert (
        select_depth(query, domain, risk, freshness, "mena" if "Oman" in query else "global")
        is expected
    )
    plan = analyze_query(query)
    source_ranges = {
        Depth.QUICK: (3, 5),
        Depth.STANDARD: (5, 10),
        Depth.DEEP: (8, 15),
    }
    lower, upper = source_ranges[plan.depth]
    assert lower <= plan.source_budget <= upper


def test_variants_are_deduplicated_bilingual_and_capped() -> None:
    variants = build_query_variants(
        "Egypt economy",
        Language.ENGLISH,
        ["Egypt economy", "egypt ECONOMY", "one", "two", "three", "four", "five"],
        Domain.MENA_LOCAL,
        "mena",
    )

    assert len(variants) == 6
    assert len({item.casefold() for item in variants}) == len(variants)
    assert any("\u0600" <= char <= "\u06ff" for item in variants for char in item)
    assert any("A" <= char <= "Z" or "a" <= char <= "z" for item in variants for char in item)


def test_budget_overrides_flow_into_the_plan_and_route() -> None:
    settings = Settings(
        _env_file=None,
        SOURCES_PER_JOB=7,
        SOURCE_BUDGET=7,
        STANDARD_SOURCE_BUDGET=10,
        TOKEN_BUDGET=12_345,
        STANDARD_TOKEN_BUDGET=30_000,
        TIME_BUDGET_SECONDS=91,
        STANDARD_TIME_BUDGET_SECONDS=200,
        JOB_TIMEOUT_SECONDS=100,
        SEARCH_SUBQUERIES_PER_JOB=2,
        SEARCH_VARIANTS_PER_JOB=2,
        SEARCH_TASKS_PER_JOB=3,
        TASK_BUDGET=3,
    )
    plan = analyze_query("Compare solar and wind energy", settings=settings)

    assert plan.source_budget == 7
    assert plan.token_budget == 12_345
    assert plan.time_budget_seconds == 91
    assert plan.query_budget == 2
    assert plan.variant_budget == 2
    assert plan.task_budget == 3
    assert len(plan.subquestions) <= 2
    assert len(plan.query_variants) <= 2
    assert len(route_plan(plan, expand_variants=True)) <= 3


def test_budget_fields_have_bounded_validation() -> None:
    with pytest.raises(ValidationError):
        ResearchPlan(query="valid question", token_budget=999)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, SEARCH_SUBQUERIES_PER_JOB=7)


def test_route_fanout_is_capped_by_source_query_and_task_budgets() -> None:
    plan = ResearchPlan(
        query="climate policy",
        tools_selected=["first", "second", "third"],
        query_variants=["one", "two", "three", "four", "five", "six"],
        source_budget=8,
        query_budget=6,
        variant_budget=6,
        task_budget=5,
    )

    first = route_plan(plan, expand_variants=True)
    second = route_plan(plan, expand_variants=True)

    assert len(first) == 5
    assert [task.task_id for task in first] == [task.task_id for task in second]
    assert [task.query for task in first] == ["one", "one", "one", "two", "two"]
    assert sum(int(task.filters["max_results"]) for task in first) <= plan.source_budget
    assert len({task.task_id for task in first}) == len(first)


@pytest.mark.asyncio
async def test_execute_plan_preserves_the_same_fanout_cap() -> None:
    class EmptyTool:
        def __init__(self, name: str) -> None:
            self.name = name
            self.calls: list[SearchTask] = []

        async def search(self, task: SearchTask) -> list[object]:
            self.calls.append(task)
            return []

        async def health_check(self) -> bool:
            return True

    tools = {name: EmptyTool(name) for name in ("first", "second", "third")}
    plan = ResearchPlan(
        query="climate policy",
        tools_selected=list(tools),
        query_variants=["one", "two", "three", "four"],
        source_budget=4,
        query_budget=4,
        variant_budget=4,
        task_budget=3,
    )

    result = await ToolRouter(tools).execute_plan(plan, expand_variants=True)

    assert len(result.attempts) == 3
    assert sum(len(tool.calls) for tool in tools.values()) == 3
