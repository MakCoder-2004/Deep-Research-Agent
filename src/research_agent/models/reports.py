"""User-visible report models with citation validation."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from research_agent.models import SourceType, require_tz_aware
from research_agent.models.requests import reject_forbidden_report_fields

TRACKING_PARAM_NAMES = frozenset(
    {
        "fbclid",
        "fb_action_ids",
        "fb_action_types",
        "gclid",
        "gclsrc",
        "gbraid",
        "wbraid",
        "dclid",
        "yclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "mkt_tok",
        "sc_campaign",
        "spm",
        "ref",
        "ref_src",
        "pf_rd_p",
        "pf_rd_r",
        "wickedid",
        "_hsenc",
        "_hsmi",
        "hsctatracking",
        "igshid",
        "_ga",
        "_gl",
        "vero_id",
        "zanpid",
        "scid",
        "srsltid",
    }
)

_TRACKING_PREFIXES = ("utm_", "pk_", "piwik_", "matomo", "vero_", "spm_")


def canonicalize_url(url: str | HttpUrl) -> str:
    """Canonicalize a URL for deduplication and uniqueness checks.

    Drops userinfo (dedup by host), lowercases scheme/host, strips default
    ports, IDNA-encodes unicode hosts, normalizes dot-segments, sorts query
    pairs, strips tracking params and fragments. Raises ValueError for
    non-http(s) or malformed inputs so M3 dedup never silently merges.
    """
    raw = str(url).strip()
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme for canonicalization: {scheme!r}.")
    if not parts.hostname:
        raise ValueError("URL must have a host for canonicalization.")
    try:
        host = parts.hostname.lower().encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        host = parts.hostname.lower()
    # Preserve IPv6 brackets that hostname strips.
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"Invalid URL port in {raw!r}.") from exc
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    netloc = host if port is None else f"{host}:{port}"
    import posixpath
    import re as _re

    path = parts.path or "/"
    # Decode percent-encoded dots before dot-segment resolution so that
    # "/a/%2e%2e/b" normalizes like "/a/../b" and cannot bypass dedup.
    # Only dots are decoded (not %2F etc.) to avoid merging distinct paths.
    path = _re.sub(r"%2e", ".", path, flags=_re.IGNORECASE)
    path = posixpath.normpath(path)
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAM_NAMES
        and not any(key.lower().startswith(p) for p in _TRACKING_PREFIXES)
    ]
    query_pairs.sort()
    query = urlencode(query_pairs, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


class Finding(BaseModel):
    """A single report finding with citations into sources."""

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1)
    citation_ids: list[int] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _reject_forbidden_fields(cls, data: Any) -> Any:
        return reject_forbidden_report_fields(data)

    @model_validator(mode="after")
    def _reject_duplicate_citations(self) -> Finding:
        if len(set(self.citation_ids)) != len(self.citation_ids):
            raise ValueError("Citation ids must not contain duplicates.")
        return self


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

    @model_validator(mode="before")
    @classmethod
    def _reject_forbidden_fields(cls, data: Any) -> Any:
        return reject_forbidden_report_fields(data)

    @model_validator(mode="after")
    def _require_tz_aware(self) -> Source:
        require_tz_aware(self.accessed_at, "accessed_at")
        require_tz_aware(self.published_at, "published_at")
        return self


class ResearchReport(BaseModel):
    """Validated user-visible report. Never contains prompts or chain-of-thought."""

    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=1)
    key_findings: list[Finding] = Field(min_length=1)
    summary: str = Field(min_length=1)
    sources: list[Source] = Field(min_length=1)
    tools_used: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _reject_forbidden_fields(cls, data: Any) -> Any:
        return reject_forbidden_report_fields(data)

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
        # Trailing uncited sources are allowed as background material; an
        # uncited source followed by a cited one is rejected below.
        first_uncited = next(
            (index for index, source_id in enumerate(source_ids) if source_id not in seen_set),
            len(source_ids),
        )
        if any(source_id in seen_set for source_id in source_ids[first_uncited:]):
            raise ValueError("Sources must be ordered by first citation appearance.")
        return self
