"""Focused tests for the M3-to-M4 source-candidate contract."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from research_agent.models import SourceType
from research_agent.models.research import (
    SearchHit,
    SourceCandidate,
    source_candidates_from_ranked_hits,
)


def _hit() -> SearchHit:
    return SearchHit(
        url="https://EXAMPLE.org/article?utm_source=search&part=2",
        aliases=["https://example.org/old-record"],
        title="Climate policy evidence",
        snippet="A complete source snippet.",
        publisher="Example publisher",
        published_at=datetime(2025, 2, 3, 4, 5, tzinfo=UTC),
        accessed_at=datetime(2026, 1, 2, 3, 4, tzinfo=UTC),
        source_type=SourceType.PAPER,
        doi="10.1000/example.doi",
        content_hash="sha256:abc123",
        score=0.875,
        tool_name="tavily",
        tool_names=["brave_search", "tavily"],
    )


def test_conversion_assigns_stable_ids_and_preserves_metadata() -> None:
    hit = _hit()

    candidates = source_candidates_from_ranked_hits([hit, hit], first_source_id=7)

    assert [candidate.source_id for candidate in candidates] == [7, 8]
    candidate = candidates[0]
    assert str(candidate.canonical_url) == "https://example.org/article?part=2"
    assert str(candidate.url) == str(candidate.canonical_url)
    assert str(candidate.original_url) == "https://example.org/article?utm_source=search&part=2"
    assert [str(alias) for alias in candidate.aliases] == ["https://example.org/old-record"]
    assert candidate.title == hit.title
    assert candidate.snippet == hit.snippet
    assert candidate.publisher == hit.publisher
    assert candidate.published_at == hit.published_at
    assert candidate.accessed_at == hit.accessed_at
    assert candidate.source_type is SourceType.PAPER
    assert candidate.doi == hit.doi
    assert candidate.content_hash == hit.content_hash
    assert candidate.score == hit.score
    assert candidate.tool_name == "tavily"
    assert candidate.tool_names == ["tavily", "brave_search"]
    assert candidate.id == candidate.source_id


def test_legacy_url_input_and_json_round_trip_use_canonical_contract() -> None:
    candidate = SourceCandidate(
        source_id=1,
        url="https://example.org/record",
        title="Record",
        accessed_at=datetime(2026, 1, 1, tzinfo=UTC),
        tool_name="crossref",
    )

    restored = SourceCandidate.model_validate_json(candidate.model_dump_json())

    assert restored == candidate
    assert restored.original_url == restored.canonical_url
    assert "canonical_url" in candidate.model_dump(mode="json")
    assert "url" not in candidate.model_dump(mode="json")


@pytest.mark.parametrize(
    "updates",
    [
        {"source_id": 0},
        {"canonical_url": "https://example.org/record?utm_source=tracking"},
        {"canonical_url": "file:///tmp/record"},
        {"accessed_at": datetime(2026, 1, 1)},
        {"published_at": datetime(2026, 1, 1)},
        {"doi": "  "},
        {"content_hash": ""},
        {"unexpected": "field"},
    ],
)
def test_invalid_candidate_values_are_rejected(updates: dict[str, object]) -> None:
    data: dict[str, object] = {
        "source_id": 1,
        "canonical_url": "https://example.org/record",
        "title": "Record",
        "accessed_at": datetime(2026, 1, 1, tzinfo=UTC),
        "tool_name": "tavily",
    }
    data.update(updates)

    with pytest.raises(ValidationError):
        SourceCandidate.model_validate(data)


def test_ranked_conversion_rejects_non_positive_start_id() -> None:
    with pytest.raises(ValueError, match="positive"):
        SourceCandidate.from_ranked_hits([_hit()], first_source_id=0)
