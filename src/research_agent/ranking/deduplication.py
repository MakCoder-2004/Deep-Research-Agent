"""Deterministic URL, DOI, title, and content deduplication."""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from research_agent.models import SourceType
from research_agent.models.reports import canonicalize_url
from research_agent.models.research import SearchHit

__all__ = [
    "canonicalize_hits",
    "deduplicate_hits",
    "deduplicate_search_hits",
    "extract_doi",
    "normalize_title",
]

_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:a-z0-9]+", re.IGNORECASE)
_TRAILING_DOI_PUNCTUATION = ".,;:)]}>" + "'"


def normalize_title(title: str) -> str:
    """Normalize a title for stable, Unicode-aware similarity checks."""

    normalized = unicodedata.normalize("NFKC", title).casefold()
    return " ".join(re.findall(r"[\w\u0600-\u06ff]+", normalized, re.UNICODE))


def _doi_value(value: str) -> str | None:
    text = value.strip().casefold()
    text = re.sub(r"^(?:https?://)?(?:dx\.)?doi\.org/", "", text)
    text = re.sub(r"^doi:\s*", "", text)
    match = _DOI_RE.search(text)
    if match is None:
        return None
    return match.group(0).rstrip(_TRAILING_DOI_PUNCTUATION)


def extract_doi(hit: SearchHit) -> str | None:
    """Extract a normalized DOI from explicit metadata or visible hit text."""

    if hit.doi:
        doi = _doi_value(hit.doi)
        if doi:
            return doi
    for value in (str(hit.url), hit.title, hit.snippet):
        doi = _doi_value(value)
        if doi:
            return doi
    return None


def _content_hash(hit: SearchHit) -> str | None:
    if hit.content_hash and hit.content_hash.strip():
        return hit.content_hash.strip().casefold()
    return None


def _title_match(left: str, right: str, threshold: float) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if len(left_tokens) < 3 or len(right_tokens) < 3:
        return False
    overlap = len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))
    return overlap >= 0.8 and difflib.SequenceMatcher(None, left, right).ratio() >= threshold


def _coerce_hit(raw: object) -> SearchHit | None:
    if isinstance(raw, SearchHit):
        data = raw.model_dump(mode="python")
    elif isinstance(raw, Mapping):
        data = dict(raw)
    else:
        return None
    try:
        raw_url = data.get("url")
        canonical = canonicalize_url(str(raw_url))
        data["url"] = canonical
        aliases = data.get("aliases", [])
        canonical_aliases: list[str] = []
        if isinstance(aliases, (list, tuple, set)):
            for alias in aliases:
                try:
                    alias_url = canonicalize_url(str(alias))
                except (TypeError, ValueError):
                    continue
                if alias_url != canonical and alias_url not in canonical_aliases:
                    canonical_aliases.append(alias_url)
        data["aliases"] = canonical_aliases
        if not isinstance(data.get("tool_name"), str) or not str(data["tool_name"]).strip():
            return None
        return SearchHit.model_validate(data)
    except (ValidationError, TypeError, ValueError):
        return None


def canonicalize_hits(hits: Iterable[SearchHit | Mapping[str, Any]]) -> list[SearchHit]:
    """Canonicalize valid hits and skip only malformed individual hits."""

    result: list[SearchHit] = []
    for raw in hits:
        hit = _coerce_hit(raw)
        if hit is not None:
            result.append(hit)
    return result


def _source_quality(hit: SearchHit) -> int:
    return {
        SourceType.OFFICIAL: 6,
        SourceType.MEDICAL: 5,
        SourceType.PAPER: 5,
        SourceType.ACADEMIC: 5,
        SourceType.REPOSITORY: 4,
        SourceType.TECHNICAL: 4,
        SourceType.NEWS: 3,
        SourceType.WEB: 2,
        SourceType.WIKI: 1,
        SourceType.OTHER: 1,
    }.get(hit.source_type, 1)


def _best_index(group: list[int], hits: list[SearchHit]) -> int:
    return max(
        group,
        key=lambda index: (
            _source_quality(hits[index]),
            float(hits[index].score),
            bool(hits[index].publisher),
            bool(hits[index].published_at),
            len(hits[index].snippet),
            -index,
        ),
    )


def _merge_group(group: list[int], hits: list[SearchHit]) -> SearchHit:
    winner = hits[_best_index(group, hits)]
    data = winner.model_dump(mode="python")
    tools: list[str] = []
    aliases: list[str] = []
    publishers = [hits[index].publisher for index in group if hits[index].publisher]
    dates: list[datetime] = []
    for index in group:
        published_at = hits[index].published_at
        if published_at is not None:
            dates.append(published_at)
    access_dates = [hits[index].accessed_at for index in group]
    for index in group:
        hit = hits[index]
        for name in [hit.tool_name, *hit.tool_names]:
            if name and name not in tools:
                tools.append(name)
        for alias in [str(item) for item in hit.aliases] + [str(hit.url)]:
            if alias != str(winner.url) and alias not in aliases:
                aliases.append(alias)
    data["tool_names"] = tools
    data["aliases"] = aliases
    data["score"] = max(float(hits[index].score) for index in group)
    if not data.get("publisher") and publishers:
        data["publisher"] = publishers[0]
    if data.get("published_at") is None and dates:
        data["published_at"] = max(dates)
    if access_dates:
        data["accessed_at"] = max(access_dates, key=lambda value: value.timestamp())
    if not data.get("doi"):
        data["doi"] = next(
            (doi for index in group if (doi := extract_doi(hits[index])) is not None),
            None,
        )
    if not data.get("content_hash"):
        data["content_hash"] = next(
            (hits[index].content_hash for index in group if hits[index].content_hash), None
        )
    return SearchHit.model_validate(data)


def deduplicate_hits(
    hits: Iterable[SearchHit | Mapping[str, Any]],
    *,
    title_similarity: float = 0.88,
) -> list[SearchHit]:
    """Merge duplicate hits while retaining the best metadata and provenance.

    Exact canonical URLs, DOI values, and supplied content hashes are strong
    identities.  Title similarity is a conservative fallback and is only
    considered for titles with at least three meaningful tokens.
    """

    if not 0.0 < title_similarity <= 1.0:
        raise ValueError("title_similarity must be in (0, 1].")
    normalized = canonicalize_hits(hits)
    if not normalized:
        return []
    parent = list(range(len(normalized)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    identities: dict[tuple[str, str], int] = {}
    for index, hit in enumerate(normalized):
        keys = [
            ("url", str(hit.url)),
            *[("alias", str(alias)) for alias in hit.aliases],
        ]
        doi = extract_doi(hit)
        if doi:
            keys.append(("doi", doi))
        content_hash = _content_hash(hit)
        if content_hash:
            keys.append(("content", content_hash))
        for key in keys:
            previous = identities.get(key)
            if previous is not None:
                union(index, previous)
            else:
                identities[key] = index

    # Search result sets are small (bounded by the plan source budget), making
    # a pairwise title check both transparent and deterministic.
    title_keys = [normalize_title(hit.title) for hit in normalized]
    for left in range(len(normalized)):
        for right in range(left):
            if _title_match(title_keys[left], title_keys[right], title_similarity):
                union(left, right)

    groups: dict[int, list[int]] = {}
    for index in range(len(normalized)):
        groups.setdefault(find(index), []).append(index)
    ordered_groups = sorted(groups.values(), key=lambda group: group[0])
    return [_merge_group(group, normalized) for group in ordered_groups]


deduplicate_search_hits = deduplicate_hits
