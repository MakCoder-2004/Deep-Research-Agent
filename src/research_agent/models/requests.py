"""Request, job, and session models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

from research_agent.models import Depth, Domain, JobState, Language, RiskLevel, require_tz_aware
from research_agent.models.urls import normalize_source_url

FORBIDDEN_REPORT_FIELDS = frozenset(
    {
        "prompt",
        "system_prompt",
        "hidden_prompt",
        "chain_of_thought",
        "cot",
        "reasoning",
        "thought",
        "internal_reasoning",
    }
)


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
    requested_language: Language | None = Field(
        default=None,
        validation_alias=AliasChoices("requested_language", "output_language"),
    )

    @field_validator("source_url", mode="before")
    @classmethod
    def _normalize_source_url(cls, value: object) -> object:
        return normalize_source_url(value)

    @model_validator(mode="after")
    def _normalize_requested_language(self) -> ResearchRequest:
        if self.requested_language is Language.MIXED:
            self.requested_language = None
        return self

    @property
    def output_language(self) -> Language | None:
        """Expose the requested output language under a descriptive name."""
        if self.requested_language is not None:
            return self.requested_language
        return None if self.language is Language.MIXED else self.language


class ResearchJob(BaseModel):
    """Persisted lifecycle record for a research job."""

    model_config = ConfigDict(extra="forbid")

    job_id: UUID = Field(default_factory=uuid4)
    user_id: int = Field(ge=1)
    query: str = Field(min_length=1, max_length=4000)
    language: Language = Language.MIXED
    source_url: HttpUrl | None = None
    domain: Domain = Domain.GENERAL
    depth: Depth = Depth.STANDARD
    risk_level: RiskLevel = RiskLevel.NORMAL
    state: JobState = JobState.QUEUED
    repair_count: int = Field(default=0, ge=0, le=1)
    trace_id: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("source_url", mode="before")
    @classmethod
    def _normalize_source_url(cls, value: object) -> object:
        return normalize_source_url(value)

    @model_validator(mode="after")
    def _require_tz_aware(self) -> ResearchJob:
        require_tz_aware(self.created_at, "created_at")
        require_tz_aware(self.updated_at, "updated_at")
        return self


class SessionContext(BaseModel):
    """Temporary conversation state (24h / last 6 interactions)."""

    model_config = ConfigDict(extra="forbid")

    user_id: int = Field(ge=1)
    language: Language = Language.ENGLISH
    interactions: list[str] = Field(default_factory=list, max_length=6)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _require_tz_aware(self) -> SessionContext:
        require_tz_aware(self.updated_at, "updated_at")
        return self
