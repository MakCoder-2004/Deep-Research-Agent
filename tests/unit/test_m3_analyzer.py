"""Deterministic analyzer verification for Milestone 3."""

from __future__ import annotations

import pytest

from research_agent.agents.analyzer import (
    analyze_query,
    build_query_variants,
    detect_language,
    is_ambiguous,
    select_depth,
)
from research_agent.models import Depth, Domain, Language, RiskLevel


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("What is the climate policy?", Language.ENGLISH),
        ("ما هي سياسة المناخ؟", Language.ARABIC),
        ("ما هي climate policy؟", Language.MIXED),
    ],
)
def test_language_detection_is_script_based(query: str, expected: Language) -> None:
    assert detect_language(query) is expected


def test_analyzer_populates_structured_english_mena_fields() -> None:
    plan = analyze_query("Compare the latest climate policy in Egypt and Saudi Arabia")

    assert plan.language is Language.ENGLISH
    assert plan.domain is Domain.MENA_LOCAL
    assert plan.locality == "mena"
    assert plan.jurisdictions == ["Egypt", "Saudi Arabia"]
    assert plan.requires_freshness is True
    assert plan.risk_level is RiskLevel.NORMAL
    assert plan.depth is Depth.DEEP
    assert plan.source_budget == 12
    assert plan.time_budget_seconds == 300
    assert plan.needs_clarification is False
    assert plan.clarification_question is None
    assert plan.subquestions
    assert plan.query_variants
    assert plan.tools_selected
    assert plan.source_categories


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
        (
            "What is the recommended dosage for diabetes treatment?",
            Domain.MEDICAL,
            RiskLevel.HIGH_STAKES,
            False,
            Depth.DEEP,
        ),
        (
            "What causes this? How does it differ? What evidence supports it?",
            Domain.GENERAL,
            RiskLevel.NORMAL,
            False,
            Depth.DEEP,
        ),
    ],
)
def test_depth_rules_are_deterministic(
    query: str,
    domain: Domain,
    risk: RiskLevel,
    freshness: bool,
    expected: Depth,
) -> None:
    assert select_depth(query, domain, risk, freshness) is expected
    assert select_depth(query, domain, risk, freshness) is expected


def test_bilingual_expansion_adds_the_opposite_script_when_useful() -> None:
    english = analyze_query("What are the latest economic updates in Egypt?")
    arabic = analyze_query("ما هي آخر أخبار الاقتصاد في مصر؟")

    assert any(
        any("\u0600" <= char <= "\u06ff" for char in item) for item in english.query_variants
    )
    assert any(any("A" <= char <= "z" for char in item) for item in arabic.query_variants)
    assert build_query_variants(
        "climate policy",
        Language.ENGLISH,
        ["climate policy"],
        Domain.GENERAL,
        "global",
    ) == ["climate policy"]


@pytest.mark.parametrize("query", ["help", "ما رأيك", "?"])
def test_materially_vague_queries_request_clarification(query: str) -> None:
    plan = analyze_query(query)

    assert is_ambiguous(query)
    assert plan.needs_clarification is True
    assert plan.clarification_question
    assert "clarify" in plan.clarification_question.lower()
    assert "توضيح" in plan.clarification_question


def test_urls_are_not_blocked_by_clarification_gate() -> None:
    plan = analyze_query("Read https://example.org/research")

    assert is_ambiguous(plan.query) is False
    assert plan.needs_clarification is False
    assert str(plan.source_url) == "https://example.org/research"
    assert plan.tools_selected == []
