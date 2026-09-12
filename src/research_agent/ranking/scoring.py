"""LLM-free deterministic source scoring and stable ranking."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from research_agent.models import Language, SourceType
from research_agent.models.research import ResearchPlan, SearchHit

__all__ = [
    "ScoreBreakdown",
    "rank_search_hits",
    "rank_sources",
    "score_hit",
    "score_hit_breakdown",
    "score_sources",
]

_WORD_RE = re.compile(r"[\w\u0600-\u06ff]+", re.UNICODE)
_ARABIC_RE = re.compile(r"[\u0600-\u06ff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_KNOWN_NEWS = frozenset(
    {
        "bbc.com",
        "reuters.com",
        "apnews.com",
        "aljazeera.com",
        "theguardian.com",
        "nytimes.com",
    }
)
_OFFICIAL_WORDS = frozenset(
    {
        "government",
        "gov",
        "ministry",
        "regulator",
        "authority",
        "commission",
        "who",
        "un",
        "ncbi",
        "pubmed",
    }
)
_JURISDICTION_TLDS = {
    "egypt": ("eg", "مصر"),
    "saudi arabia": ("sa", "السعودية"),
    "uae": ("ae", "الإمارات"),
    "qatar": ("qa", "قطر"),
    "kuwait": ("kw", "الكويت"),
    "bahrain": ("bh", "البحرين"),
    "oman": ("om", "عمان"),
    "jordan": ("jo", "الأردن"),
    "lebanon": ("lb", "لبنان"),
    "morocco": ("ma", "المغرب"),
    "tunisia": ("tn", "تونس"),
}


@dataclass(frozen=True)
class ScoreBreakdown:
    """Named deterministic scoring components for auditability."""

    relevance: float
    authority: float
    freshness: float
    primary_source_bonus: float
    language_or_region_match: float
    corroboration_potential: float
    duplication_penalty: float
    inaccessible_content_penalty: float

    @property
    def total(self) -> float:
        return (
            self.relevance
            + self.authority
            + self.freshness
            + self.primary_source_bonus
            + self.language_or_region_match
            + self.corroboration_potential
            - self.duplication_penalty
            - self.inaccessible_content_penalty
        )


def _tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return {token for token in _WORD_RE.findall(normalized) if len(token) > 1}


def _domain(hit: SearchHit) -> str:
    try:
        return (urlsplit(str(hit.url)).hostname or "").casefold().rstrip(".")
    except ValueError:
        return ""


def _authority(hit: SearchHit) -> float:
    domain = _domain(hit)
    source_values = {
        SourceType.OFFICIAL: 2.6,
        SourceType.MEDICAL: 2.35,
        SourceType.PAPER: 2.3,
        SourceType.ACADEMIC: 2.3,
        SourceType.REPOSITORY: 1.9,
        SourceType.TECHNICAL: 1.8,
        SourceType.NEWS: 1.55,
        SourceType.WEB: 1.0,
        SourceType.WIKI: 0.7,
        SourceType.OTHER: 0.5,
    }
    score = source_values.get(hit.source_type, 0.5)
    registrable = ".".join(domain.split(".")[-2:]) if domain else domain
    if domain.endswith(".gov") or ".gov." in domain or domain.endswith(".edu") or ".edu." in domain:
        score += 0.65
    if domain in {"who.int", "un.org", "ncbi.nlm.nih.gov", "pubmed.ncbi.nlm.nih.gov"}:
        score += 0.6
    if registrable in _KNOWN_NEWS:
        score += 0.35
    if domain.endswith("doi.org"):
        score += 0.25
    if _tokens(hit.publisher or "") & _OFFICIAL_WORDS:
        score += 0.3
    return score


def _is_primary(hit: SearchHit) -> bool:
    domain = _domain(hit)
    if hit.source_type is SourceType.OFFICIAL:
        return True
    if hit.source_type is SourceType.REPOSITORY and domain in {"github.com", "gitlab.com"}:
        return True
    if domain.endswith(".gov") or ".gov." in domain:
        return True
    if any(word in domain for word in ("who.int", "un.org", "ncbi.nlm.nih.gov")):
        return True
    return False


def _freshness(hit: SearchHit, *, required: bool, now: datetime) -> float:
    if hit.published_at is None:
        return -0.25 if required else 0.0
    age_days = max(0.0, (now - hit.published_at.astimezone(UTC)).total_seconds() / 86400)
    if required:
        return max(-0.9, 1.35 - min(age_days, 3650.0) / 365.0)
    return max(0.0, 0.4 - min(age_days, 3650.0) / 3650.0)


def _language_region(
    hit: SearchHit,
    language: Language,
    locality: str,
    jurisdictions: Sequence[str],
) -> float:
    text = f"{hit.title} {hit.snippet}"
    has_arabic = _ARABIC_RE.search(text) is not None
    has_latin = _LATIN_RE.search(text) is not None
    score = 0.0
    if language is Language.ARABIC:
        score += 0.55 if has_arabic else -0.2
    elif language is Language.ENGLISH:
        score += 0.35 if has_latin and not has_arabic else 0.0
    elif language is Language.MIXED:
        score += 0.25 if has_arabic and has_latin else 0.0
    if locality == "mena" or jurisdictions:
        lower = text.casefold()
        for jurisdiction in jurisdictions:
            values = _JURISDICTION_TLDS.get(jurisdiction.casefold(), ())
            if any(value.casefold() in lower for value in values[1:]) or any(
                _domain(hit).endswith(f".{value}") for value in values[:1]
            ):
                score += 0.45
                break
        if locality == "mena" and any(
            _domain(hit).endswith(f".{tld}") for tld, _ in _JURISDICTION_TLDS.values()
        ):
            score += 0.25
    return score


def _accessibility(hit: SearchHit) -> float:
    value = f"{str(hit.url)} {hit.snippet}".casefold()
    if any(marker in value for marker in (".onion", "login", "paywall", "access denied", "403")):
        return 1.0
    if str(hit.url).lower().startswith("https://"):
        return 0.0
    return 0.4


def _relevance(hit: SearchHit, query: str) -> float:
    query_tokens = _tokens(query)
    if not query_tokens:
        return max(0.0, min(1.0, float(hit.score)))
    title_tokens = _tokens(hit.title)
    body_tokens = _tokens(hit.snippet)
    title_overlap = len(query_tokens & title_tokens) / len(query_tokens)
    body_overlap = len(query_tokens & body_tokens) / len(query_tokens)
    provider_score = max(0.0, min(1.0, float(hit.score)))
    return 2.6 * title_overlap + 1.0 * body_overlap + 0.25 * provider_score


def score_hit_breakdown(
    hit: SearchHit,
    query: str | ResearchPlan = "",
    *,
    plan: ResearchPlan | None = None,
    now: datetime | None = None,
    corroboration_count: int = 0,
    duplicate_count: int = 0,
    language: Language | None = None,
    locality: str | None = None,
    jurisdictions: Sequence[str] = (),
    accessible: bool | None = None,
) -> ScoreBreakdown:
    """Return scoring components without making a model or network call."""

    if isinstance(query, ResearchPlan):
        plan = query
        query_text = plan.query
    else:
        query_text = query
    if plan is not None:
        query_text = plan.query if not query_text else query_text
        language = language or plan.language
        locality = locality or plan.locality
        jurisdictions = tuple(jurisdictions) or tuple(plan.jurisdictions)
        freshness_required = plan.requires_freshness
    else:
        language = language or Language.MIXED
        locality = locality or "global"
        freshness_required = False
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    current = current.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    if accessible is False:
        inaccessible = 1.5
    elif accessible is True:
        inaccessible = 0.0
    else:
        inaccessible = _accessibility(hit)
    return ScoreBreakdown(
        relevance=_relevance(hit, query_text),
        authority=_authority(hit),
        freshness=_freshness(hit, required=freshness_required, now=current),
        primary_source_bonus=1.1 if _is_primary(hit) else 0.0,
        language_or_region_match=_language_region(hit, language, locality, jurisdictions),
        corroboration_potential=min(1.0, max(0, corroboration_count) * 0.3),
        duplication_penalty=min(1.5, max(0, duplicate_count) * 0.5),
        inaccessible_content_penalty=inaccessible,
    )


def score_hit(
    hit: SearchHit,
    query: str | ResearchPlan = "",
    **kwargs: Any,
) -> float:
    """Return one deterministic scalar source score."""

    return score_hit_breakdown(hit, query, **kwargs).total


def _duplicate_counts(hits: Sequence[SearchHit]) -> list[int]:
    counts: dict[str, int] = {}
    keys: list[str] = []
    for hit in hits:
        key = str(hit.url).casefold()
        keys.append(key)
        counts[key] = counts.get(key, 0) + 1
    return [max(0, counts[key] - 1) for key in keys]


def _corroboration_counts(hits: Sequence[SearchHit], query: str) -> list[int]:
    domains = [_domain(hit) for hit in hits]
    query_tokens = _tokens(query)
    relevant = [query_tokens & (_tokens(hit.title) | _tokens(hit.snippet)) for hit in hits]
    return [
        sum(
            1
            for other, other_terms in enumerate(relevant)
            if other != index
            and domains[other]
            and domains[other] != domains[index]
            and other_terms
        )
        for index in range(len(hits))
    ]


def score_sources(
    hits: Iterable[SearchHit],
    query: str | ResearchPlan = "",
    *,
    plan: ResearchPlan | None = None,
    now: datetime | None = None,
) -> list[SearchHit]:
    """Return hits with scores replaced by deterministic first-pass scores."""

    hit_list = list(hits)
    query_text = (
        plan.query
        if plan is not None
        else (query.query if isinstance(query, ResearchPlan) else query)
    )
    duplicate_counts = _duplicate_counts(hit_list)
    corroboration_counts = _corroboration_counts(hit_list, query_text)
    scored: list[SearchHit] = []
    for index, hit in enumerate(hit_list):
        breakdown = score_hit_breakdown(
            hit,
            query,
            plan=plan,
            now=now,
            duplicate_count=duplicate_counts[index],
            corroboration_count=corroboration_counts[index],
        )
        scored.append(hit.model_copy(update={"score": breakdown.total}))
    return scored


def rank_sources(
    hits: Iterable[SearchHit],
    query: str | ResearchPlan = "",
    *,
    plan: ResearchPlan | None = None,
    now: datetime | None = None,
) -> list[SearchHit]:
    """Score and stably sort sources, retaining input order for ties."""

    scored = score_sources(hits, query, plan=plan, now=now)
    return [
        hit
        for _, hit in sorted(enumerate(scored), key=lambda pair: (-float(pair[1].score), pair[0]))
    ]


rank_search_hits = rank_sources
