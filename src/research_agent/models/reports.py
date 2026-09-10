"""User-visible report models with citation validation."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from research_agent.models import SourceType

TRACKING_PARAM_NAMES = frozenset(
    {
        "fbclid",
        "gclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "_hsenc",
        "_hsmi",
        "igshid",
    }
)


def canonicalize_url(url: str | HttpUrl) -> str:
    """Canonicalize a URL for deduplication and uniqueness checks."""
    parts = urlsplit(str(url))
    scheme = parts.scheme.lower()
    host = parts.hostname.lower() if parts.hostname else ""
    port = parts.port
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    netloc = host if port is None else f"{host}:{port}"
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAM_NAMES and not key.lower().startswith("utm_")
    ]
    query_pairs.sort()
    query = urlencode(query_pairs, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


class Finding(BaseModel):
    """A single report finding with citations into sources."""

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1)
    citation_ids: list[int] = Field(min_length=1)


class Source(BaseModel):
    """A cited source listed in the report."""

    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=1)
    title: str = Field(min_length=1)
    url: HttpUrl
    publisher: str | None = None
    published_at: datetime | None = None
    accessed_at: datetime
    source_type: SourceType = SourceType.WEB


class ResearchReport(BaseModel):
    """Validated user-visible report. Never contains prompts or chain-of-thought."""

    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=1)
    key_findings: list[Finding] = Field(min_length=1)
    summary: str = Field(min_length=1)
    sources: list[Source] = Field(min_length=1)
    tools_used: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_citations_and_sources(self) -> ResearchReport:
        source_ids = [source.id for source in self.sources]
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("Source ids must be unique.")
        canonical_urls = [canonicalize_url(source.url) for source in self.sources]
        if len(set(canonical_urls)) != len(canonical_urls):
            raise ValueError("Source URLs must be unique after canonicalization.")
        known_ids = set(source_ids)
        for finding in self.key_findings:
            if not finding.citation_ids:
                raise ValueError("Every finding must have at least one citation.")
            for citation_id in finding.citation_ids:
                if citation_id not in known_ids:
                    raise ValueError(
                        f"Citation id {citation_id} does not reference an existing source."
                    )
        seen: list[int] = []
        for finding in self.key_findings:
            for citation_id in finding.citation_ids:
                if citation_id not in seen:
                    seen.append(citation_id)
        seen_set = set(seen)
        cited_in_listed_order = [source_id for source_id in source_ids if source_id in seen_set]
        if cited_in_listed_order != seen:
            raise ValueError("Sources must be ordered by first citation appearance.")
        first_uncited = next(
            (index for index, source_id in enumerate(source_ids) if source_id not in seen_set),
            len(source_ids),
        )
        if any(source_id in seen_set for source_id in source_ids[first_uncited:]):
            raise ValueError("Sources must be ordered by first citation appearance.")
        return self
