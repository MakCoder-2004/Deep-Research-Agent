"""Deterministic retrieval deduplication and source ranking."""

from research_agent.ranking.deduplication import (
    canonicalize_hits,
    deduplicate_hits,
    deduplicate_search_hits,
    extract_doi,
    normalize_title,
)
from research_agent.ranking.scoring import (
    ScoreBreakdown,
    rank_search_hits,
    rank_sources,
    score_hit,
    score_hit_breakdown,
    score_sources,
)

__all__ = [
    "ScoreBreakdown",
    "canonicalize_hits",
    "deduplicate_hits",
    "deduplicate_search_hits",
    "extract_doi",
    "normalize_title",
    "rank_search_hits",
    "rank_sources",
    "score_hit",
    "score_hit_breakdown",
    "score_sources",
]
