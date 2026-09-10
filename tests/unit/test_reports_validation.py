"""Unit tests for Pydantic report and citation rules (M1.37)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from research_agent.models.reports import Finding, ResearchReport, Source, canonicalize_url


def _source(source_id: int, url: str, title: str = "Title") -> Source:
    return Source(
        id=source_id,
        title=title,
        url=url,  # type: ignore[arg-type]
        accessed_at=datetime.now(UTC),
    )


def _valid_report() -> ResearchReport:
    return ResearchReport(
        topic="Topic",
        key_findings=[Finding(statement="Claim [1] [2]", citation_ids=[1, 2])],
        summary="Summary [1] [2]",
        sources=[
            _source(1, "https://example.com/a"),
            _source(2, "https://example.com/b"),
        ],
        tools_used=["tavily"],
    )


def test_valid_report_passes() -> None:
    assert _valid_report().topic == "Topic"


def test_finding_without_citation_rejected() -> None:
    with pytest.raises(ValidationError):
        Finding(statement="No citation", citation_ids=[])


def test_unknown_citation_id_rejected() -> None:
    with pytest.raises(ValidationError, match="existing source"):
        ResearchReport(
            topic="T",
            key_findings=[Finding(statement="S", citation_ids=[99])],
            summary="Sum",
            sources=[_source(1, "https://example.com/a")],
            tools_used=["tavily"],
        )


def test_duplicate_canonical_urls_rejected() -> None:
    with pytest.raises(ValidationError, match="unique after canonicalization"):
        ResearchReport(
            topic="T",
            key_findings=[Finding(statement="S", citation_ids=[1])],
            summary="Sum",
            sources=[
                _source(1, "https://example.com/a?utm_source=x"),
                _source(2, "https://EXAMPLE.com/a/?utm_medium=y"),
            ],
            tools_used=["tavily"],
        )


def test_source_order_must_follow_first_citation() -> None:
    with pytest.raises(ValidationError, match="first citation appearance"):
        ResearchReport(
            topic="T",
            key_findings=[Finding(statement="S", citation_ids=[2, 1])],
            summary="Sum",
            sources=[
                _source(1, "https://example.com/a"),
                _source(2, "https://example.com/b"),
            ],
            tools_used=["tavily"],
        )


def test_trailing_uncited_source_allowed() -> None:
    report = ResearchReport(
        topic="T",
        key_findings=[Finding(statement="S", citation_ids=[1, 2])],
        summary="Sum",
        sources=[
            _source(1, "https://example.com/a"),
            _source(2, "https://example.com/b"),
            _source(3, "https://example.com/background"),
        ],
        tools_used=["tavily"],
    )
    assert [source.id for source in report.sources] == [1, 2, 3]


def test_interspersed_uncited_source_rejected() -> None:
    with pytest.raises(ValidationError, match="first citation appearance"):
        ResearchReport(
            topic="T",
            key_findings=[Finding(statement="S", citation_ids=[1, 2])],
            summary="Sum",
            sources=[
                _source(1, "https://example.com/a"),
                _source(3, "https://example.com/background"),
                _source(2, "https://example.com/b"),
            ],
            tools_used=["tavily"],
        )


def test_duplicate_source_ids_rejected() -> None:
    with pytest.raises(ValidationError, match="unique"):
        ResearchReport(
            topic="T",
            key_findings=[Finding(statement="S", citation_ids=[1])],
            summary="Sum",
            sources=[
                _source(1, "https://example.com/a"),
                _source(1, "https://example.com/b"),
            ],
            tools_used=["tavily"],
        )


def test_hidden_prompt_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        ResearchReport(
            topic="T",
            key_findings=[Finding(statement="S", citation_ids=[1])],
            summary="Sum",
            sources=[_source(1, "https://example.com/a")],
            tools_used=["tavily"],
            prompt="hidden",  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        Finding(statement="S", citation_ids=[1], chain_of_thought="cot")  # type: ignore[call-arg]


def test_forbidden_report_fields_rejected_with_clear_message() -> None:
    with pytest.raises(ValidationError, match="Forbidden report fields"):
        ResearchReport.model_validate(
            {
                "topic": "T",
                "key_findings": [{"statement": "S", "citation_ids": [1]}],
                "summary": "Sum",
                "sources": [
                    {
                        "id": 1,
                        "title": "A",
                        "url": "https://example.com/a",
                        "accessed_at": "2026-01-01T00:00:00+00:00",
                    }
                ],
                "tools_used": ["tavily"],
                "hidden_prompt": "exfiltrate",
            }
        )


def test_canonicalize_url_strips_tracking_and_case() -> None:
    assert (
        canonicalize_url("https://EXAMPLE.com/a/?utm_source=x&b=2") == "https://example.com/a?b=2"
    )
    assert canonicalize_url("https://example.com/a?utm_source=x") == canonicalize_url(
        "https://example.com/a"
    )


def test_arabic_report_preserves_citation_markers() -> None:
    report = ResearchReport(
        topic="موضوع",
        key_findings=[Finding(statement="نتيجة مهمة [1]", citation_ids=[1])],
        summary="ملخص [1]",
        sources=[_source(1, "https://example.com/ar")],
        tools_used=["tavily"],
    )
    assert "[1]" in report.key_findings[0].statement
