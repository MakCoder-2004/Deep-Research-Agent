"""Request, job, and session models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from research_agent.models import Depth, Domain, JobState, Language, RiskLevel

FORBIDDEN_REPORT_FIELDS = frozenset({"prompt", "hidden_prompt", "chain_of_thought", "cot"})


def reject_forbidden_report_fields(data: Any) -> Any:
    """Reject hidden-prompt / chain-of-thought keys in user-visible report data."""
    if isinstance(data, dict):
        forbidden = sorted(FORBIDDEN_REPORT_FIELDS.intersection(data))
        if forbidden:
            raise ValueError(
                f"Forbidden report fields must not appear in delivery data: {forbidden}."
            )
    return data


class ResearchRequest(BaseModel):
    """A validated incoming research request."""

    model_config = ConfigDict(extra="forbid")

    user_id: int = Field(ge=1)
    query: str = Field(min_length=1, max_length=4000)
    language: Language = Language.MIXED
    source_url: HttpUrl | None = None


class ResearchJob(BaseModel):
    """Persisted lifecycle record for a research job."""

    model_config = ConfigDict(extra="forbid")

    job_id: UUID = Field(default_factory=uuid4)
    user_id: int = Field(ge=1)
    query: str = Field(min_length=1, max_length=4000)
    language: Language = Language.MIXED
    domain: Domain = Domain.GENERAL
    depth: Depth = Depth.STANDARD
    risk_level: RiskLevel = RiskLevel.NORMAL
    state: JobState = JobState.QUEUED
    repair_count: int = Field(default=0, ge=0, le=1)
    trace_id: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SessionContext(BaseModel):
    """Temporary conversation state (24h / last 6 interactions)."""

    model_config = ConfigDict(extra="forbid")

    user_id: int = Field(ge=1)
    language: Language = Language.ENGLISH
    interactions: list[str] = Field(default_factory=list, max_length=6)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
