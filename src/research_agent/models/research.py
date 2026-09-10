"""Planning, retrieval, evidence, and usage models."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from research_agent.models import (
    ClaimType,
    CriticOutcome,
    Depth,
    Domain,
    Language,
    RiskLevel,
    SourceType,
)


class ResearchPlan(BaseModel):
    """Structured research plan produced by the analyzer (never prose)."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    language: Language = Language.MIXED
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
    time_budget_seconds: int = Field(default=300, ge=30)
    needs_clarification: bool = False
    clarification_question: str | None = None


class SearchTask(BaseModel):
    """A single tool execution request derived from the plan."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    query: str = Field(min_length=1)
    language: Language = Language.MIXED
    filters: dict[str, str] = Field(default_factory=dict)


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
