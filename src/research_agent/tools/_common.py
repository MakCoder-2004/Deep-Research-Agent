"""Shared HTTP, deadline, and normalization helpers for search adapters."""

from __future__ import annotations

import asyncio
import html
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from pydantic import ValidationError

from research_agent.errors import ErrorCategory
from research_agent.models import SourceType
from research_agent.models.research import SearchHit, SearchTask
from research_agent.tools.base import ToolError

__all__ = [
    "AsyncHttpTool",
    "TOOL_USER_AGENT",
    "build_hit",
    "category_for_status",
    "clean_text",
    "coerce_url",
    "domain_of",
    "doi_url",
    "http_status_category",
    "map_transport_error",
    "max_results_from_filters",
    "normalize_doi",
    "parse_datetime",
    "parse_retry_after",
    "quota_metadata_from_headers",
    "strip_html",
    "timeout_for_task",
    "truncate",
]

TOOL_USER_AGENT = "DeepResearchAgent/0.1 (research; +https://github.com/deep-research-agent)"

_TAG_RE = re.compile(r"<[^>]+>")
_GDELT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(?:T)?(\d{2})(\d{2})(\d{2})(?:Z)?$")
_DOI_RE = re.compile(r"(?i)(?:https?://(?:dx\.)?doi\.org/|doi:\s*)?(10\.\d{4,9}/[^\s\"<>]+)")


def category_for_status(status: int, body: str) -> ErrorCategory:
    """Map an HTTP status code and safe response marker to an error category."""
    lowered = body.lower()
    if "content_filter" in lowered or "content-filter" in lowered or "moderation" in lowered:
        return ErrorCategory.CONTENT_FILTERED
    rate_markers = ("rate limit", "rate_limit", "quota exceeded", "usage limit")
    if status in (402, 429, 432, 433) or any(marker in lowered for marker in rate_markers):
        return ErrorCategory.RATE_LIMITED
    if status in (401, 403):
        return ErrorCategory.AUTH
    if status == 408:
        return ErrorCategory.TIMEOUT
    if status in (400, 404, 422):
        return ErrorCategory.INVALID_REQUEST
    if status == 503:
        return ErrorCategory.UNAVAILABLE
    if 500 <= status <= 599:
        return ErrorCategory.TRANSIENT
    if 400 <= status <= 499:
        return ErrorCategory.INVALID_REQUEST
    return ErrorCategory.UNKNOWN


def map_transport_error(exc: Exception) -> ErrorCategory:
    """Map an httpx transport failure to the shared error taxonomy."""
    if isinstance(exc, httpx.TimeoutException):
        return ErrorCategory.TIMEOUT
    if isinstance(exc, httpx.ConnectError):
        return ErrorCategory.UNAVAILABLE
    if isinstance(exc, httpx.NetworkError):
        return ErrorCategory.TRANSIENT
    if isinstance(exc, httpx.HTTPError):
        return ErrorCategory.TRANSIENT
    return ErrorCategory.UNKNOWN


def parse_retry_after(headers: httpx.Headers | dict[str, str]) -> float | None:
    """Read a numeric or HTTP-date Retry-After header without exposing values."""
    value = _header_value(headers, "retry-after")
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except (AttributeError, TypeError, ValueError):
        try:
            date = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
        seconds = (date - datetime.now(UTC)).total_seconds()
    return max(0.0, seconds)


def _header_value(headers: httpx.Headers | dict[str, str], name: str) -> str | None:
    """Read a header case-insensitively from either supported header shape."""
    value = headers.get(name)
    if value is not None:
        return value
    lowered = name.lower()
    for key, candidate in headers.items():
        if str(key).lower() == lowered:
            return candidate
    return None


def _safe_metadata_value(value: object) -> int | float | str | None:
    """Keep provider quota values bounded and free of arbitrary response data."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return value if math.isfinite(float(value)) else None
        except OverflowError:
            return None
    text = str(value).strip()
    if not text or len(text) > 100 or any(ord(char) < 32 for char in text):
        return None
    try:
        number = float(text)
    except (OverflowError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def quota_metadata_from_headers(headers: httpx.Headers | dict[str, str]) -> dict[str, object]:
    """Extract standard rate-limit headers without retaining credentials or body data."""
    metadata: dict[str, object] = {}
    aliases = {
        "x-ratelimit-limit": "limit",
        "x-rate-limit-limit": "limit",
        "ratelimit-limit": "limit",
        "x-ratelimit-remaining": "remaining",
        "x-rate-limit-remaining": "remaining",
        "ratelimit-remaining": "remaining",
        "x-ratelimit-reset": "reset",
        "x-rate-limit-reset": "reset",
        "ratelimit-reset": "reset",
    }
    for header, key in aliases.items():
        value = _header_value(headers, header)
        if value is None or key in metadata:
            continue
        safe = _safe_metadata_value(value)
        if safe is not None:
            metadata[key] = safe
    retry_after = parse_retry_after(headers)
    if retry_after is not None:
        metadata["retry_after"] = retry_after
    return metadata


def normalize_doi(value: object) -> str | None:
    """Return a canonical bare DOI, or ``None`` for values that are not DOIs."""
    if not isinstance(value, str):
        return None
    text = unquote(value.strip()).strip("<>\"'")
    parsed_url = urlparse(text)
    if (
        parsed_url.scheme
        and parsed_url.hostname
        and parsed_url.hostname.lower()
        in {
            "doi.org",
            "dx.doi.org",
            "www.doi.org",
        }
    ):
        text = parsed_url.path.lstrip("/")
    match = _DOI_RE.search(text)
    if match is None:
        return None
    doi = match.group(1).rstrip(".,;:!?)]}").strip().lower()
    return doi or None


def doi_url(value: object) -> str | None:
    """Return a resolver URL for a normalized DOI, or ``None`` if invalid."""
    doi = normalize_doi(value)
    return f"https://doi.org/{doi}" if doi else None


def http_status_category(
    status: int,
    body: str,
    headers: httpx.Headers | dict[str, str] | None = None,
) -> ErrorCategory:
    """Map status codes, including quota exhaustion reported in headers."""
    if headers is not None:
        remaining = _header_value(headers, "x-ratelimit-remaining") or _header_value(
            headers, "ratelimit-remaining"
        )
        if remaining is not None and remaining.strip() == "0":
            return ErrorCategory.RATE_LIMITED
    return category_for_status(status, body)


def _response_text(response: httpx.Response) -> str:
    """Return a bounded response body for classification, never for logging."""
    try:
        return response.text[:4000]
    except (UnicodeError, httpx.ResponseNotRead):
        return ""


class AsyncHttpTool:
    """Small base for async search tools with one consistent failure boundary."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")
        self._timeout_seconds = timeout_seconds
        self._client = client
        self._owned = client is None
        self.quota_metadata: dict[str, object] = {}

    @property
    def name(self) -> str:
        raise NotImplementedError

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            timeout = httpx.Timeout(
                connect=5.0,
                read=self._timeout_seconds,
                write=self._timeout_seconds,
                pool=5.0,
            )
            self._client = httpx.AsyncClient(timeout=timeout)
        return self._client

    async def aclose(self) -> None:
        if self._owned and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        task: SearchTask | None,
        method: str,
        url: str,
        *,
        error_message: str,
        expected_status: int | tuple[int, ...] = 200,
        **kwargs: Any,
    ) -> httpx.Response:
        """Perform one bounded request and normalize transport/status failures."""
        timeout = timeout_for_task(task, self._timeout_seconds)
        try:
            async with asyncio.timeout(timeout):
                response = await self._get_client().request(method, url, **kwargs)
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            raise ToolError(
                f"{error_message} timed out.",
                category=ErrorCategory.TIMEOUT,
                tool_name=self.name,
            ) from exc
        except Exception as exc:
            raise ToolError(
                f"{error_message} failed.",
                category=map_transport_error(exc),
                tool_name=self.name,
            ) from exc

        self.quota_metadata = quota_metadata_from_headers(response.headers)
        statuses = (expected_status,) if isinstance(expected_status, int) else expected_status
        if response.status_code not in statuses:
            body = _response_text(response)
            raise ToolError(
                f"{error_message} failed.",
                category=http_status_category(response.status_code, body, response.headers),
                tool_name=self.name,
                http_status=response.status_code,
                retry_after=parse_retry_after(response.headers),
            )
        return response

    def _json(self, response: httpx.Response, *, message: str) -> object:
        try:
            data = response.json()
        except (TypeError, UnicodeError, ValueError) as exc:
            raise ToolError(
                f"{message} returned invalid JSON.",
                category=ErrorCategory.INVALID_REQUEST,
                tool_name=self.name,
            ) from exc
        if isinstance(data, Mapping):
            for raw_key, target in (
                ("quota_max", "limit"),
                ("quota_remaining", "remaining"),
                ("credits", "credits"),
                ("backoff", "backoff"),
            ):
                safe = _safe_metadata_value(data.get(raw_key))
                if safe is not None:
                    self.quota_metadata[target] = safe
            usage = data.get("usage")
            if isinstance(usage, Mapping):
                safe = _safe_metadata_value(usage.get("credits"))
                if safe is not None:
                    self.quota_metadata["credits"] = safe
        return data


def timeout_for_task(task: SearchTask | None, default: float) -> float:
    """Apply an optional remaining task deadline without changing the protocol."""
    if task is None:
        return default
    filters = {str(key).lower(): value for key, value in task.filters.items()}
    for key in ("deadline_seconds", "request_timeout_seconds", "timeout_seconds", "deadline"):
        raw = filters.get(key)
        if raw is None:
            continue
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            return max(0.0, min(default, value))
    raw_deadline = filters.get("deadline_at")
    if raw_deadline:
        deadline = parse_datetime(raw_deadline)
        if deadline is not None:
            return max(0.0, min(default, (deadline - datetime.now(UTC)).total_seconds()))
    return default


def truncate(text: str, limit: int = 2000) -> str:
    """Bound free-text payloads so snippets stay within a sane size."""
    cleaned = text.strip()
    if limit <= 0 or len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + "..."


def clean_text(value: object) -> str:
    """Coerce an arbitrary provider value to stripped text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value).strip()
    if isinstance(value, list):
        parts = [clean_text(item) for item in value]
        return " ".join(part for part in parts if part).strip()
    return ""


def strip_html(value: str) -> str:
    """Remove tags and entities from provider snippets."""
    return " ".join(html.unescape(_TAG_RE.sub(" ", value)).split())


def coerce_url(value: object) -> str | None:
    """Return a syntactically valid credential-free HTTP(S) URL, else None."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or any(char.isspace() for char in candidate):
        return None
    try:
        parsed = urlparse(candidate)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return None
    if (
        parsed.username is not None
        or parsed.password is not None
        or (port is None and ":" in parsed.netloc and not parsed.netloc.startswith("["))
    ):
        return None
    return candidate


def domain_of(url: str) -> str:
    """Extract a lowercase hostname from a URL."""
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def max_results_from_filters(
    filters: dict[str, str],
    *,
    default: int = 5,
    maximum: int = 10,
) -> int:
    """Read a per-tool result limit from task filters, clamped safely."""
    keys = (
        "max_results",
        "maxresults",
        "max_records",
        "maxrecords",
        "count",
        "limit",
        "per_page",
        "perpage",
        "numresults",
        "num_results",
        "rows",
        "pagesize",
        "page_size",
    )
    lowered = {str(key).lower(): value for key, value in filters.items()}
    for key in keys:
        raw = lowered.get(key)
        if raw is None:
            continue
        try:
            parsed = int(str(raw).strip())
        except (ValueError, TypeError):
            continue
        return max(1, min(maximum, parsed))
    return max(1, min(maximum, default))


def parse_datetime(value: object) -> datetime | None:
    """Parse common provider date forms into timezone-aware UTC values."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, (int, float)):
        stamp = float(value)
        if not math.isfinite(stamp):
            return None
        if stamp > 1e12:
            stamp /= 1000.0
        try:
            return datetime.fromtimestamp(stamp, tz=UTC)
        except (ValueError, OverflowError, OSError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    match = _GDELT_RE.match(text)
    if match:
        try:
            year, month, day, hour, minute, second = (int(part) for part in match.groups())
            return datetime(year, month, day, hour, minute, second, tzinfo=UTC)
        except ValueError:
            return None
    iso = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        parsed = None
    if parsed is not None:
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    try:
        parsed_email = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        parsed_email = None
    if parsed_email is not None:
        return (
            parsed_email.replace(tzinfo=UTC)
            if parsed_email.tzinfo is None
            else parsed_email.astimezone(UTC)
        )
    for fmt in (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y-%m",
        "%Y",
        "%d %b %Y",
        "%b %d, %Y",
        "%Y.%m.%d",
        "%Y %b %d",
        "%Y %B %d",
        "%Y %b",
        "%Y %B",
        "%b %Y",
        "%B %Y",
    ):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def build_hit(
    *,
    url: object,
    title: object,
    snippet: object = "",
    publisher: object = None,
    published_at: datetime | None = None,
    source_type: SourceType = SourceType.WEB,
    tool_name: str,
    score: float = 0.0,
    doi: object = None,
) -> SearchHit | None:
    """Build a valid normalized hit; malformed individual results are skipped."""
    raw_url = coerce_url(url)
    if raw_url is None:
        return None
    try:
        raw_score = float(score)
    except (TypeError, ValueError):
        raw_score = 0.0
    if not math.isfinite(raw_score):
        raw_score = 0.0
    raw_publisher = clean_text(publisher) or domain_of(raw_url) or None
    try:
        return SearchHit(
            url=raw_url,  # type: ignore[arg-type]
            title=(clean_text(title) or raw_url)[:500],
            snippet=truncate(clean_text(snippet)),
            publisher=raw_publisher,
            published_at=published_at,
            source_type=source_type,
            tool_name=tool_name,
            score=raw_score,
            doi=normalize_doi(doi),
        )
    except (ValidationError, TypeError, ValueError):
        return None
