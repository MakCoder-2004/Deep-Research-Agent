"""Shared enums and constrained values for domain models."""

from __future__ import annotations

from enum import StrEnum


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
