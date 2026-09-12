"""Planning, retrieval, evidence, and usage models."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    HttpUrl,
    field_validator,
    model_validator,
)

from research_agent.models import (
    ClaimType,
    CriticOutcome,
    Depth,
    Domain,
    Language,
    RiskLevel,
    SourceType,
    require_tz_aware,
)
from research_agent.models.reports import canonicalize_url
from research_agent.models.urls import normalize_source_url


class ResearchPlan(BaseModel):
    """Structured research plan produced by the analyzer (never prose)."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    language: Language = Language.MIXED
    requested_language: Language | None = Field(
        default=None,
        validation_alias=AliasChoices("requested_language", "output_language"),
    )
    domain: Domain = Domain.GENERAL
    locality: str = Field(default="global")
    jurisdictions: list[str] = Field(default_factory=list)
    requires_freshness: bool = False
    risk_level: RiskLevel = RiskLevel.NORMAL
    depth: Depth = Depth.STANDARD
    subquestions: list[str] = Field(default_factory=list)
    query_variants: list[str] = Field(default_factory=list)
    tools_selected: list[str] = Field(default_factory=list)
    source_categories: list[str] = Field(default_factory=list)
    source_budget: int = Field(default=8, ge=1, le=15)
    token_budget: int = Field(default=20_000, ge=1_000, le=100_000)
    time_budget_seconds: int = Field(default=300, ge=30, le=600)
    query_budget: int = Field(default=6, ge=1, le=6)
    variant_budget: int = Field(default=6, ge=1, le=6)
    task_budget: int = Field(default=12, ge=1, le=50)
    source_url: HttpUrl | None = None
    needs_clarification: bool = False
    clarification_question: str | None = None

    @field_validator("source_url", mode="before")
    @classmethod
    def _normalize_source_url(cls, value: object) -> object:
        return normalize_source_url(value)

    @model_validator(mode="after")
    def _normalize_policy_lists(self) -> ResearchPlan:
        """Keep hand-created plans within the same deterministic limits."""

        def unique(values: Iterable[str], limit: int) -> list[str]:
            output: list[str] = []
            seen: set[str] = set()
            for raw in values:
                value = " ".join(str(raw).strip().split())
                key = value.casefold()
                if value and key not in seen:
                    output.append(value)
                    seen.add(key)
                if len(output) >= limit:
                    break
            return output

        self.subquestions = unique(self.subquestions, self.query_budget)
        self.query_variants = unique(self.query_variants, self.variant_budget)
        self.tools_selected = unique(self.tools_selected, self.task_budget)
        if self.requested_language is Language.MIXED:
            self.requested_language = None
        return self

    @property
    def output_language(self) -> Language:
        """Return the requested output language, or detected language by default."""
        return self.requested_language or self.language


class SearchTask(BaseModel):
    """A single tool execution request derived from the plan."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    query: str = Field(min_length=1)
    language: Language = Language.MIXED
    filters: dict[str, str] = Field(default_factory=dict)
    source_url: HttpUrl | None = None

    @field_validator("source_url", mode="before")
    @classmethod
    def _normalize_source_url(cls, value: object) -> object:
        return normalize_source_url(value)


class SearchHit(BaseModel):
    """Normalized single search result."""

    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    title: str = Field(min_length=1)
    snippet: str = ""
    publisher: str | None = None
    published_at: datetime | None = None
    accessed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source_type: SourceType = SourceType.WEB
    tool_name: str = Field(min_length=1)
    score: float = 0.0
    doi: str | None = None
    content_hash: str | None = None
    tool_names: list[str] = Field(default_factory=list)
    aliases: list[HttpUrl] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_tz_aware(self) -> SearchHit:
        require_tz_aware(self.accessed_at, "accessed_at")
        require_tz_aware(self.published_at, "published_at")
        return self

    @model_validator(mode="after")
    def _include_primary_tool(self) -> SearchHit:
        """Keep the original adapter name in merged-provenance metadata."""
        names: list[str] = []
        for name in [self.tool_name, *self.tool_names]:
            if name and name not in names:
                names.append(name)
        self.tool_names = names
        return self


class SourceCandidate(BaseModel):
    """Stable, metadata-complete handoff from retrieval to extraction.

    ``canonical_url`` is the URL used for identity and future fetching.  The
    ``url`` input/property keeps callers that use the existing ``SearchHit``
    shape source-compatible without making the serialized contract ambiguous.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )

    source_id: int = Field(ge=1)
    canonical_url: HttpUrl = Field(validation_alias=AliasChoices("canonical_url", "url"))
    original_url: HttpUrl | None = None
    aliases: list[HttpUrl] = Field(default_factory=list)
    title: str = Field(min_length=1)
    snippet: str = ""
    publisher: str | None = None
    published_at: datetime | None = None
    accessed_at: datetime
    source_type: SourceType = SourceType.WEB
    doi: str | None = None
    content_hash: str | None = None
    score: FiniteFloat = 0.0
    tool_name: str = Field(min_length=1)
    tool_names: list[str] = Field(default_factory=list)

    @property
    def url(self) -> HttpUrl:
        """Expose the canonical URL under the existing ``SearchHit`` name."""
        return self.canonical_url

    @property
    def id(self) -> int:
        """Expose the source ID under the report model's conventional name."""
        return self.source_id

    @model_validator(mode="after")
    def _validate_contract(self) -> SourceCandidate:
        if canonicalize_url(self.canonical_url) != str(self.canonical_url):
            raise ValueError("canonical_url must already be canonicalized.")
        if self.original_url is None:
            self.original_url = self.canonical_url
        alias_values = [str(alias) for alias in self.aliases]
        if len(alias_values) != len(set(alias_values)):
            raise ValueError("aliases must not contain duplicates.")
        if self.doi is not None and not self.doi.strip():
            raise ValueError("doi must not be blank when provided.")
        if self.content_hash is not None and not self.content_hash.strip():
            raise ValueError("content_hash must not be blank when provided.")
        require_tz_aware(self.accessed_at, "accessed_at")
        require_tz_aware(self.published_at, "published_at")

        tool_names: list[str] = []
        for name in [self.tool_name, *self.tool_names]:
            normalized = name.strip()
            if not normalized:
                raise ValueError("tool names must not be blank.")
            if normalized not in tool_names:
                tool_names.append(normalized)
        self.tool_name = tool_names[0]
        self.tool_names = tool_names
        return self

    @classmethod
    def from_search_hit(cls, hit: SearchHit, *, source_id: int) -> SourceCandidate:
        """Convert one ranked-compatible hit without discarding its metadata."""
        canonical = canonicalize_url(hit.url)
        aliases: list[str] = []
        for alias in hit.aliases:
            value = str(alias)
            if value not in aliases:
                aliases.append(value)
        return cls(
            source_id=source_id,
            canonical_url=HttpUrl(canonical),
            original_url=hit.url,
            aliases=[HttpUrl(alias) for alias in aliases],
            title=hit.title,
            snippet=hit.snippet,
            publisher=hit.publisher,
            published_at=hit.published_at,
            accessed_at=hit.accessed_at,
            source_type=hit.source_type,
            doi=hit.doi,
            content_hash=hit.content_hash,
            score=hit.score,
            tool_name=hit.tool_name,
            tool_names=hit.tool_names,
        )

    @classmethod
    def from_ranked_hits(
        cls,
        hits: Iterable[SearchHit],
        *,
        first_source_id: int = 1,
    ) -> list[SourceCandidate]:
        """Assign deterministic one-based IDs in the supplied ranking order."""
        if first_source_id < 1:
            raise ValueError("first_source_id must be positive.")
        return [
            cls.from_search_hit(hit, source_id=first_source_id + index)
            for index, hit in enumerate(hits)
        ]


def source_candidates_from_ranked_hits(
    hits: Iterable[SearchHit],
    *,
    first_source_id: int = 1,
) -> list[SourceCandidate]:
    """Convert ranked ``SearchHit`` records into the M4 source contract."""
    return SourceCandidate.from_ranked_hits(hits, first_source_id=first_source_id)


class SourceDocument(BaseModel):
    """Bounded clean document extracted from a URL."""

    model_config = ConfigDict(extra="forbid")

    source_id: int = Field(ge=1)
    url: HttpUrl
    title: str = ""
    author: str | None = None
    publisher: str | None = None
    published_at: datetime | None = None
    headings: list[str] = Field(default_factory=list)
    body_text: str = ""
    links: list[HttpUrl] = Field(default_factory=list)
    quotations: list[str] = Field(default_factory=list)
    fetch_ms: int = Field(default=0, ge=0)
    extraction_tool: str = "beautifulsoup"
    requested_url: HttpUrl | None = None
    fetch_requested_url: HttpUrl | None = None
    fetch_final_url: HttpUrl | None = None
    status_code: int | None = Field(default=None, ge=100, le=599)
    content_type: str | None = None
    bytes_read: int = Field(default=0, ge=0)
    fallback_used: bool = False

    @model_validator(mode="after")
    def _require_tz_aware(self) -> SourceDocument:
        require_tz_aware(self.published_at, "published_at")
        return self


class EvidenceClaim(BaseModel):
    """A query-relevant claim linked to source IDs and quotations."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    source_ids: list[int] = Field(min_length=1)
    quotations: list[str] = Field(default_factory=list)
    claim_type: ClaimType = ClaimType.FACT


class EvidenceLedger(BaseModel):
    """Concise evidence set passed to the critic."""

    model_config = ConfigDict(extra="forbid")

    claims: list[EvidenceClaim] = Field(default_factory=list)
    agreements: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class CritiqueResult(BaseModel):
    """Structured critic verdict."""

    model_config = ConfigDict(extra="forbid")

    outcome: CriticOutcome
    deficiencies: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    citation_issues: list[str] = Field(default_factory=list)


class ProviderUsage(BaseModel):
    """LLM request accounting for budgets and traces."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    cache_hit: bool = False


class TraceContext(BaseModel):
    """Sanitized metadata attached to a LangSmith root trace."""

    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    report_id: str | None = None
    user_hash: str = Field(min_length=1)
    environment: str = Field(min_length=1)
    language: Language = Language.MIXED
    domain: Domain = Domain.GENERAL
    depth: Depth = Depth.STANDARD
    risk_level: RiskLevel = RiskLevel.NORMAL
    tools_selected: list[str] = Field(default_factory=list)
    provider: str = ""
    model: str = ""
    critic_result: CriticOutcome | None = None
