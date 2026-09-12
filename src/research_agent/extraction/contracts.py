"""Contracts and bounded in-memory values used by safe extraction."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class ExtractionConfig(BaseModel):
    """Limits and feature flags for one extraction service instance.

    The defaults are deliberately conservative.  ``content`` on
    :class:`FetchedPage` is a short-lived buffer owned by the fetch/extract
    call; the extraction package has no persistence or tracing hook for it.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    connect_timeout_seconds: float = Field(default=5.0, gt=0)
    read_timeout_seconds: float = Field(default=20.0, gt=0)
    write_timeout_seconds: float = Field(default=5.0, gt=0)
    pool_timeout_seconds: float = Field(default=5.0, gt=0)
    dns_timeout_seconds: float = Field(default=5.0, gt=0)
    max_redirects: int = Field(default=3, ge=0, le=20)
    max_response_bytes: int = Field(default=2_000_000, ge=1)
    max_source_chars: int = Field(default=20_000, ge=1, le=20_000)
    max_headings: int = Field(default=100, ge=0, le=1_000)
    heading_max_chars: int = Field(default=500, ge=1, le=2_000)
    title_max_chars: int = Field(default=500, ge=1, le=2_000)
    metadata_max_chars: int = Field(default=300, ge=1, le=2_000)
    max_quotations: int = Field(default=5, ge=0, le=50)
    quotation_max_chars: int = Field(default=280, ge=40, le=2_000)
    max_links: int = Field(default=100, ge=0, le=1_000)
    respect_robots_txt: bool = True
    user_agent: str = Field(
        default="DeepResearchAgent/0.1 (research; +https://github.com/deep-research-agent)",
        min_length=1,
        max_length=300,
    )
    allowed_mime_types: frozenset[str] = frozenset({"text/html", "application/xhtml+xml"})
    robots_max_response_bytes: int = Field(default=512_000, ge=1)
    robots_cache_ttl_seconds: float | None = Field(default=3_600.0, gt=0)
    jina_reader_enabled: bool = False
    jina_reader_base_url: str = "https://r.jina.ai/"
    jina_reader_api_key: SecretStr = SecretStr("")

    @field_validator("user_agent")
    @classmethod
    def _validate_user_agent(cls, value: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("user_agent must not contain control characters.")
        normalized = value.strip()
        if not normalized:
            raise ValueError("user_agent must not be blank.")
        return normalized

    @field_validator("allowed_mime_types", mode="before")
    @classmethod
    def _normalize_mime_types(cls, value: object) -> frozenset[str]:
        if isinstance(value, str):
            values: Sequence[object] = value.split(",")
        elif isinstance(value, (set, frozenset, list, tuple)):
            values = list(value)
        else:
            raise ValueError("allowed_mime_types must be a collection of MIME strings.")
        normalized = frozenset(str(item).split(";", 1)[0].strip().lower() for item in values)
        if not normalized or any("/" not in item or item.startswith("/") for item in normalized):
            raise ValueError("allowed_mime_types must contain valid MIME types.")
        return normalized

    @model_validator(mode="before")
    @classmethod
    def _accept_settings_names(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        values = dict(data)
        # Settings calls the same budget CHARS_PER_SOURCE.  Accepting its name
        # keeps construction from application settings explicit and lossless.
        if "max_source_chars" not in values and "chars_per_source" in values:
            values["max_source_chars"] = values["chars_per_source"]
        return values

    @classmethod
    def from_settings(cls, settings: object) -> ExtractionConfig:
        """Build extraction limits from the application's Pydantic settings."""
        return cls(
            connect_timeout_seconds=getattr(settings, "extraction_connect_timeout_seconds", 5.0),
            read_timeout_seconds=getattr(settings, "extraction_read_timeout_seconds", 20.0),
            write_timeout_seconds=getattr(settings, "extraction_write_timeout_seconds", 5.0),
            pool_timeout_seconds=getattr(settings, "extraction_pool_timeout_seconds", 5.0),
            dns_timeout_seconds=getattr(settings, "extraction_dns_timeout_seconds", 5.0),
            max_redirects=getattr(settings, "max_redirects", 3),
            max_response_bytes=getattr(settings, "max_response_bytes", 2_000_000),
            max_source_chars=getattr(settings, "chars_per_source", 20_000),
            max_headings=getattr(settings, "max_headings", 100),
            heading_max_chars=getattr(settings, "heading_max_chars", 500),
            title_max_chars=getattr(settings, "title_max_chars", 500),
            metadata_max_chars=getattr(settings, "metadata_max_chars", 300),
            respect_robots_txt=getattr(settings, "respect_robots_txt", True),
            robots_cache_ttl_seconds=getattr(settings, "robots_cache_ttl_seconds", 3_600.0),
            jina_reader_enabled=getattr(settings, "jina_reader_enabled", False),
            jina_reader_base_url=getattr(settings, "jina_reader_base_url", "https://r.jina.ai/"),
            jina_reader_api_key=SecretStr(
                _secret_value(getattr(settings, "jina_reader_api_key", ""))
            ),
        )


def _secret_value(value: object) -> str:
    """Read a Pydantic ``SecretStr`` without ever stringifying the secret."""
    getter = getattr(value, "get_secret_value", None)
    if callable(getter):
        result = getter()
        return result if isinstance(result, str) else ""
    return value if isinstance(value, str) else ""


@dataclass(frozen=True, slots=True)
class FetchedPage:
    """Bounded response data held only for the duration of extraction."""

    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    content: bytes
    bytes_read: int
    fetch_ms: int


DNSResult = Sequence[object]
DNSResolverCallable = Callable[[str, int], Awaitable[DNSResult]]
AddressValidator = Callable[[list[object]], tuple[str, ...]]


@runtime_checkable
class DNSResolver(Protocol):
    """Injectable asynchronous hostname resolver."""

    async def resolve(self, hostname: str, port: int) -> DNSResult: ...


@runtime_checkable
class ReaderFallback(Protocol):
    """Narrow contract for an optional, already safety-wrapped reader."""

    async def read(self, url: str) -> FetchedPage: ...
