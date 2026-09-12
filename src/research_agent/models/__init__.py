"""Shared enums and constrained values for domain models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum


def require_tz_aware(value: datetime | None, field_name: str) -> datetime | None:
    """Return the datetime, rejecting naive values that lack tzinfo.

    SQLite parsing helpers attach UTC explicitly, so a naive datetime here
    always indicates a caller bug rather than a storage artifact.
    """
    if value is not None and value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware.")
    return value


class Language(StrEnum):
    ENGLISH = "en"
    ARABIC = "ar"
    MIXED = "mixed"


class Domain(StrEnum):
    GENERAL = "general"
    ACADEMIC = "academic"
    NEWS = "news"
    TECHNICAL = "technical"
    MENA_LOCAL = "mena_local"
    MEDICAL = "medical"
    LEGAL = "legal"
    FINANCIAL = "financial"


class Depth(StrEnum):
    QUICK = "quick"
    STANDARD = "standard"
    DEEP = "deep"


class RiskLevel(StrEnum):
    NORMAL = "normal"
    HIGH_STAKES = "high_stakes"


class JobState(StrEnum):
    QUEUED = "queued"
    ACTIVE = "active"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class AttemptOutcome(StrEnum):
    """Terminal outcome for one search-tool attempt."""

    SUCCESS = "success"
    FAILURE = "failure"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class SourceType(StrEnum):
    WEB = "web"
    NEWS = "news"
    ACADEMIC = "academic"
    OFFICIAL = "official"
    TECHNICAL = "technical"
    WIKI = "wiki"
    PAPER = "paper"
    REPOSITORY = "repository"
    MEDICAL = "medical"
    OTHER = "other"


class CriticOutcome(StrEnum):
    PASS = "PASS"  # noqa: S105 - critic verdict, not a password
    REPAIR_REQUIRED = "REPAIR_REQUIRED"
    PARTIAL = "PARTIAL"
    REFUSE = "REFUSE"


class ClaimType(StrEnum):
    FACT = "fact"
    ESTIMATE = "estimate"
    OPINION = "opinion"
    ALLEGATION = "allegation"
