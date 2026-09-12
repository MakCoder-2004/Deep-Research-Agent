"""Deterministic search routing and bounded asynchronous tool execution."""

from __future__ import annotations

import asyncio
import inspect
import itertools
import logging
import math
import re
import time
from collections.abc import Awaitable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    ValidationError,
    field_validator,
    model_validator,
)

from research_agent.errors import ClarificationRequiredError, ErrorCategory
from research_agent.models import AttemptOutcome
from research_agent.models.reports import canonicalize_url
from research_agent.models.research import (
    ResearchPlan,
    SearchHit,
    SearchTask,
    SourceCandidate,
    source_candidates_from_ranked_hits,
)
from research_agent.models.urls import normalize_source_url
from research_agent.observability.redaction import redact_mapping, redact_text
from research_agent.ranking import canonicalize_hits, deduplicate_hits, rank_sources
from research_agent.tools._common import max_results_from_filters, normalize_doi, timeout_for_task
from research_agent.tools.base import ResearchTool, ToolError

if TYPE_CHECKING:
    import aiosqlite

    from research_agent.config import Settings
    from research_agent.persistence.repositories import ToolRunRepository

__all__ = [
    "AttemptRecorder",
    "AttemptOutcome",
    "SOURCE_PRIORITIES",
    "ToolAttempt",
    "ToolExecutionResult",
    "ToolRouter",
    "ToolRunRecorder",
    "normalize_hit",
    "normalize_hits",
    "route_plan",
]

logger = logging.getLogger(__name__)


# Keep this table aligned with PLAN section 8.  A plan's explicit selection is
# authoritative; these values are only used for manually-created plans that
# omit ``tools_selected``.
SOURCE_PRIORITIES: dict[str, tuple[str, ...]] = {
    "general": ("tavily", "brave_search", "exa", "searxng", "ddgs"),
    "academic": ("semantic_scholar", "crossref", "arxiv", "openalex", "tavily"),
    "medical": (
        "pubmed",
        "europe_pmc",
        "official_domains",
        "semantic_scholar",
        "crossref",
        "openalex",
        "tavily",
    ),
    "news": ("gdelt", "brave_search", "tavily"),
    "technical": ("official_domains", "github", "stack_exchange", "tavily", "brave_search"),
    "mena_local": ("brave_search", "tavily", "gdelt", "official_domains"),
    "legal": ("official_domains", "tavily", "brave_search"),
    "financial": ("official_domains", "tavily", "brave_search"),
}

_DOMAIN_CATEGORIES: dict[str, tuple[str, ...]] = {
    "general": ("web", "web", "web", "web", "web"),
    "academic": ("academic", "academic", "academic", "academic", "web"),
    "medical": ("medical", "medical", "official", "academic", "academic", "academic", "web"),
    "news": ("news", "news", "news"),
    "technical": ("official", "technical", "technical", "web", "web"),
    "mena_local": ("web", "web", "news", "official"),
    "legal": ("official", "reputable", "news"),
    "financial": ("official", "reputable", "news"),
}
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


class ToolAttempt(BaseModel):
    """Sanitized accounting record for one attempted tool request."""

    model_config = ConfigDict(extra="forbid")

    job_id: str = ""
    task_id: str = ""
    tool_name: str = Field(min_length=1)
    success: bool
    outcome: AttemptOutcome | None = None
    duration_ms: int = Field(default=0, ge=0)
    result_count: int = Field(default=0, ge=0)
    error_category: ErrorCategory | None = None
    error_message: str | None = None
    quota_metadata: dict[str, object] = Field(default_factory=dict)
    http_status: int | None = Field(default=None, ge=100, le=599)
    retry_after: FiniteFloat | None = Field(default=None, ge=0)
    cancelled: bool = False
    task_index: int = Field(default=0, ge=0)

    @field_validator("error_message")
    @classmethod
    def _sanitize_message(cls, value: str | None) -> str | None:
        if value is None:
            return None
        safe = redact_text(value).strip()[:500]
        return safe or None

    @model_validator(mode="after")
    def _validate_outcome(self) -> ToolAttempt:
        if self.outcome is None:
            if self.cancelled or self.error_category is ErrorCategory.CANCELLED:
                self.outcome = AttemptOutcome.CANCELLED
                self.cancelled = True
                self.error_category = ErrorCategory.CANCELLED
            elif self.success:
                self.outcome = AttemptOutcome.SUCCESS
            elif self.error_category is ErrorCategory.TIMEOUT:
                self.outcome = AttemptOutcome.TIMEOUT
            else:
                self.outcome = AttemptOutcome.FAILURE

        if self.outcome is AttemptOutcome.SUCCESS:
            if not self.success or self.cancelled or self.error_category is not None:
                raise ValueError("successful attempts must have success=True and cancelled=False")
        elif self.outcome is AttemptOutcome.CANCELLED:
            if self.success or not self.cancelled:
                raise ValueError("cancelled attempts must have success=False and cancelled=True")
            if self.error_category not in (None, ErrorCategory.CANCELLED):
                raise ValueError("cancelled attempts must use the cancelled error category")
            self.error_category = ErrorCategory.CANCELLED
        else:
            if self.success or self.cancelled:
                raise ValueError("failed attempts must have success=False and cancelled=False")
            if self.outcome is AttemptOutcome.TIMEOUT and self.error_category not in (
                None,
                ErrorCategory.TIMEOUT,
            ):
                raise ValueError("timeout attempts must use the timeout error category")
            if self.outcome is AttemptOutcome.TIMEOUT:
                self.error_category = ErrorCategory.TIMEOUT
            elif self.error_category in (ErrorCategory.TIMEOUT, ErrorCategory.CANCELLED):
                raise ValueError("failure attempts must use a failure error category")
        return self


@dataclass
class ToolExecutionResult:
    """Normalized output, ranked candidates, and accounting for tool requests.

    ``hits`` remains the normalized task-order output for compatibility.  The
    ranked retrieval boundary is exposed through ``ranked_hits`` and
    ``source_candidates``; candidates are created only after canonicalization,
    deduplication, and ranking.
    """

    hits: list[SearchHit] = field(default_factory=list)
    attempts: list[ToolAttempt] = field(default_factory=list)
    task_hits: dict[str, list[SearchHit]] = field(default_factory=dict)
    ranked_hits: list[SearchHit] = field(default_factory=list)
    source_candidates: list[SourceCandidate] = field(default_factory=list)

    @property
    def successful_tools(self) -> list[str]:
        """Return successful tool names in first-execution order."""
        seen: set[str] = set()
        names: list[str] = []
        for attempt in self.attempts:
            if attempt.success and attempt.tool_name not in seen:
                seen.add(attempt.tool_name)
                names.append(attempt.tool_name)
        return names

    @property
    def failed_tools(self) -> list[str]:
        """Return failed tool names in first-execution order."""
        seen: set[str] = set()
        names: list[str] = []
        for attempt in self.attempts:
            if not attempt.success and attempt.tool_name not in seen:
                seen.add(attempt.tool_name)
                names.append(attempt.tool_name)
        return names


class AttemptRecorder(Protocol):
    """Minimal injectable persistence contract used by :class:`ToolRouter`."""

    async def record(self, attempt: ToolAttempt) -> None: ...


class ToolRunRecorder:
    """Adapt the existing SQLite repository to the router recorder contract."""

    def __init__(
        self, conn: aiosqlite.Connection, repository: ToolRunRepository | None = None
    ) -> None:
        if repository is None:
            from research_agent.persistence.repositories import ToolRunRepository as Repository

            repository = Repository()
        self._conn = conn
        self._repository = repository

    async def record(self, attempt: ToolAttempt) -> None:
        await self._repository.record(
            self._conn,
            job_id=attempt.job_id,
            task_id=attempt.task_id,
            tool_name=attempt.tool_name,
            success=attempt.success,
            outcome=attempt.outcome,
            duration_ms=attempt.duration_ms,
            result_count=attempt.result_count,
            error_category=attempt.error_category,
            error_message=attempt.error_message,
            quota_metadata=attempt.quota_metadata,
            http_status=attempt.http_status,
            retry_after=attempt.retry_after,
            cancelled=attempt.cancelled,
            task_index=attempt.task_index,
        )


def _source_url_for_plan(plan: ResearchPlan) -> str | None:
    if plan.source_url is not None:
        return str(plan.source_url)
    match = _URL_RE.search(plan.query)
    if match is None:
        return None
    value = normalize_source_url(match.group(0))
    return value if isinstance(value, str) and value else None


def _selected_tools(plan: ResearchPlan) -> list[str]:
    if _source_url_for_plan(plan) is not None:
        return []
    domain = plan.domain.value
    names = plan.tools_selected or list(SOURCE_PRIORITIES.get(domain, SOURCE_PRIORITIES["general"]))
    selected: list[str] = []
    seen: set[str] = set()
    for raw_name in names:
        name = str(raw_name).strip().lower()
        if name and name not in seen:
            selected.append(name)
            seen.add(name)
    return selected


def route_plan(
    plan: ResearchPlan,
    *,
    expand_variants: bool = False,
    raise_on_clarification: bool = False,
) -> list[SearchTask]:
    """Turn a validated plan into ordered, deterministic search tasks.

    One request per selected tool is the default.  Callers that explicitly
    want variants can opt into bounded expansion. URL plans are handed to the
    extraction milestone through ``ResearchPlan.source_url`` and never become
    search requests.
    """

    if plan.needs_clarification:
        if raise_on_clarification:
            raise ClarificationRequiredError(
                plan.clarification_question or "Clarification is required before searching."
            )
        return []
    if _source_url_for_plan(plan) is not None:
        return []
    tools = _selected_tools(plan)
    if not tools:
        return []
    domain = plan.domain.value
    categories = plan.source_categories or list(_DOMAIN_CATEGORIES.get(domain, ("web",)))
    tools = tools[: min(plan.task_budget, plan.source_budget)]
    if not tools:
        return []

    variants = [plan.query]
    if expand_variants:
        variants = []
        seen_variants: set[str] = set()
        for raw in [*plan.query_variants, plan.query]:
            value = str(raw).strip()
            key = value.casefold()
            if value and key not in seen_variants:
                variants.append(value)
                seen_variants.add(key)
                if len(variants) >= min(plan.query_budget, plan.variant_budget):
                    break
    task_limit = min(plan.task_budget, plan.source_budget)
    task_count = min(task_limit, len(variants) * len(tools))
    base_results, extra_results = divmod(plan.source_budget, task_count)
    freshness = "pw" if plan.requires_freshness else ""
    tasks: list[SearchTask] = []
    task_number = 0
    for variant_index, query in enumerate(variants):
        for priority, tool_name in enumerate(tools):
            if len(tasks) >= task_limit:
                break
            task_number += 1
            filters = {
                "source_category": categories[min(priority, len(categories) - 1)],
                "source_priority": str(priority),
                "max_results": str(min(10, base_results + (task_number <= extra_results))),
            }
            if freshness:
                filters["freshness"] = freshness
            if plan.locality:
                filters["locality"] = plan.locality
            if plan.jurisdictions:
                filters["jurisdictions"] = ",".join(plan.jurisdictions)
            if plan.domain.value == "news" and tool_name == "brave_search":
                filters["channel"] = "news"
            if plan.locality == "mena":
                filters["region"] = "mena"
                filters["search_languages"] = "ar,en"
                filters["search_lang"] = "ar" if re.search(r"[\u0600-\u06ff]", query) else "en"
                if plan.domain.value == "mena_local" and tool_name == "official_domains":
                    filters["official_scope"] = "local"
            if plan.domain.value == "medical" and tool_name == "official_domains":
                filters["official_scope"] = "who,government"
            if plan.domain.value in {"legal", "financial"}:
                filters["source_scope"] = "official,reputable,news"
            if expand_variants:
                filters["query_variant"] = str(variant_index)
            tasks.append(
                SearchTask(
                    task_id=f"search-{task_number}-{tool_name}",
                    tool_name=tool_name,
                    query=query,
                    language=plan.language,
                    filters=filters,
                )
            )
        if len(tasks) >= task_limit:
            break
    return tasks


def _text_alias(data: dict[str, object], field_name: str, aliases: tuple[str, ...]) -> None:
    if field_name not in data:
        for alias in aliases:
            if alias in data:
                data[field_name] = data[alias]
                break
    for alias in aliases:
        if alias != field_name:
            data.pop(alias, None)
    return


def normalize_hit(raw: object, tool_name: str) -> SearchHit | None:
    """Safely coerce one adapter result, skipping malformed results.

    Canonicalization happens before Pydantic validation so an invalid URL never
    reaches the normalized result set. Existing metadata is copied rather than
    replaced, except provenance, which is assigned by the executing router.
    """

    if isinstance(raw, SearchHit):
        data = cast(dict[str, object], raw.model_dump(mode="python"))
    elif isinstance(raw, Mapping):
        data = {str(key): value for key, value in raw.items()}
    else:
        return None
    raw_url = data.get("url")
    try:
        data["url"] = canonicalize_url(cast(str, raw_url))
    except (TypeError, ValueError):
        return None

    _text_alias(data, "snippet", ("content", "description", "summary", "text"))
    _text_alias(data, "published_at", ("published_date", "publication_date", "date"))
    _text_alias(data, "accessed_at", ("access_date", "retrieved_at"))
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    executing_tool = tool_name.strip().lower()
    if not executing_tool:
        return None
    # Adapter payloads are untrusted data.  Provenance comes from the router,
    # never from a payload-provided tool_name/tool_names value.
    data["tool_name"] = executing_tool
    data["tool_names"] = [executing_tool]

    raw_aliases = data.get("aliases", [])
    aliases: list[str] = []
    if isinstance(raw_aliases, (list, tuple, set)):
        for alias in raw_aliases:
            try:
                canonical = canonicalize_url(cast(str, alias))
            except (TypeError, ValueError):
                continue
            if canonical != data["url"] and canonical not in aliases:
                aliases.append(canonical)
    data["aliases"] = aliases
    if data.get("doi") is not None:
        data["doi"] = normalize_doi(data["doi"])
    value = data.get("content_hash")
    if value is not None and not isinstance(value, str):
        data["content_hash"] = str(value)
    try:
        return SearchHit.model_validate(data)
    except (ValidationError, TypeError, ValueError):
        return None


def normalize_hits(
    raw: object,
    tool_name: str,
    *,
    max_results: int = 10,
) -> list[SearchHit]:
    """Normalize a bounded adapter response, rejecting malformed top-level data."""

    if max_results < 1:
        raise ValueError("max_results must be positive.")

    if isinstance(raw, SearchHit):
        items: Iterable[object] = (raw,)
    elif isinstance(raw, (list, tuple)):
        items = itertools.islice(raw, max_results)
    else:
        raise ToolError(
            "Tool returned an unexpected payload.",
            category=ErrorCategory.INVALID_REQUEST,
            tool_name=tool_name,
        )
    return [
        normalized for item in items if (normalized := normalize_hit(item, tool_name)) is not None
    ]


def _sanitize_error(exc: BaseException) -> str:
    message = redact_text(str(exc)).strip()
    return (message or exc.__class__.__name__)[:500]


def _error_category(exc: BaseException) -> str:
    category = getattr(exc, "category", ErrorCategory.UNKNOWN)
    if isinstance(category, ErrorCategory):
        return category.value
    value = str(category).strip().lower()
    try:
        return ErrorCategory(value).value
    except ValueError:
        return ErrorCategory.UNKNOWN.value


def _http_metadata(exc: BaseException) -> tuple[int | None, float | None]:
    status = getattr(exc, "http_status", None)
    retry_after = getattr(exc, "retry_after", None)
    try:
        status_value = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_value = None
    if status_value is not None and not 100 <= status_value <= 599:
        status_value = None
    try:
        retry_value = float(retry_after) if retry_after is not None else None
    except (TypeError, ValueError):
        retry_value = None
    if retry_value is not None and (not math.isfinite(retry_value) or retry_value < 0):
        retry_value = None
    return status_value, retry_value


def _quota_metadata(tool: object) -> dict[str, object]:
    for attribute in ("quota_metadata", "last_quota_metadata"):
        value = getattr(tool, attribute, None)
        if isinstance(value, Mapping):
            redacted = redact_mapping({str(key): item for key, item in value.items()})
            return {str(key): item for key, item in redacted.items()}
    return {}


@dataclass(frozen=True)
class _ExecutionCaps:
    """Effective execution caps after applying plan and runtime settings."""

    tasks: int
    queries: int
    variants: int
    results: int
    candidates: int


def _bounded_min(default: int, *values: object) -> int:
    limits = [default]
    for value in values:
        if not isinstance(value, (int, float, str)):
            continue
        try:
            integer = int(value)
        except (TypeError, ValueError):
            continue
        if integer > 0:
            limits.append(integer)
    return min(limits)


def _recorder_call(recorder: object, attempt: ToolAttempt) -> Awaitable[object] | object:
    target: Any = getattr(recorder, "record", recorder)
    if not callable(target):
        raise TypeError("attempt recorder must be callable or expose record()")
    try:
        parameters = tuple(inspect.signature(target).parameters.values())
    except (TypeError, ValueError):
        parameters = ()
    keyword_style = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or parameter.name in {"job_id", "task_id", "tool_name"}
        for parameter in parameters
    )
    if not keyword_style:
        return cast(Awaitable[object] | object, target(attempt))
    values: dict[str, object] = {
        "job_id": attempt.job_id,
        "task_id": attempt.task_id,
        "tool_name": attempt.tool_name,
        "success": attempt.success,
        "outcome": attempt.outcome.value if attempt.outcome is not None else None,
        "duration_ms": attempt.duration_ms,
        "result_count": attempt.result_count,
        "error_category": (
            attempt.error_category.value if attempt.error_category is not None else None
        ),
        "error_message": attempt.error_message,
        "quota_metadata": attempt.quota_metadata,
        "http_status": attempt.http_status,
        "retry_after": attempt.retry_after,
        "cancelled": attempt.cancelled,
        "task_index": attempt.task_index,
    }
    if not any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        accepted = {parameter.name for parameter in parameters}
        values = {name: value for name, value in values.items() if name in accepted}
    return cast(Awaitable[object] | object, target(**values))


class ToolRouter:
    """Route plans and execute independent search tasks with bounded fan-out."""

    def __init__(
        self,
        tools: Mapping[str, ResearchTool] | Iterable[ResearchTool] = (),
        *,
        settings: Settings | None = None,
        tool_limits: Mapping[str, int] | None = None,
        provider_limits: Mapping[str, int] | None = None,
        tool_providers: Mapping[str, str] | None = None,
        request_timeout_seconds: float | None = 15.0,
        recorder: AttemptRecorder | object | None = None,
        db_connection: aiosqlite.Connection | None = None,
        tool_run_repository: ToolRunRepository | None = None,
    ) -> None:
        if request_timeout_seconds is not None and request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive or None.")
        if recorder is not None and (db_connection is not None or tool_run_repository is not None):
            raise ValueError("Use recorder or db_connection/tool_run_repository, not both.")
        self._tools: dict[str, ResearchTool] = {}
        if isinstance(tools, Mapping):
            entries = tools.items()
            for raw_name, tool in entries:
                name = str(raw_name).strip().lower()
                if name:
                    self._tools[name] = tool
                actual_name = str(getattr(tool, "name", "")).strip().lower()
                if actual_name:
                    self._tools.setdefault(actual_name, tool)
        else:
            for tool in tools:
                name = str(getattr(tool, "name", "")).strip().lower()
                if name:
                    self._tools[name] = tool
        self._settings = settings
        configured_tool_limits = getattr(settings, "search_tool_limits", {})
        configured_provider_limits = getattr(settings, "search_provider_limits", {})
        self._tool_limits = self._validate_limits(
            {**configured_tool_limits, **(tool_limits or {})}, "tool"
        )
        self._provider_limits = self._validate_limits(
            {**configured_provider_limits, **(provider_limits or {})}, "provider"
        )
        self._default_tool_limit = int(getattr(settings, "search_tool_concurrency", 1))
        self._default_provider_limit = int(getattr(settings, "search_provider_concurrency", 1))
        self._tool_providers = {
            str(name).strip().lower(): str(provider).strip().lower()
            for name, provider in (tool_providers or {}).items()
            if str(name).strip() and str(provider).strip()
        }
        self._request_timeout_seconds = request_timeout_seconds
        self._recorder = recorder
        if db_connection is not None:
            self._recorder = ToolRunRecorder(db_connection, tool_run_repository)
        self._tool_semaphores: dict[str, asyncio.Semaphore] = {}
        self._provider_semaphores: dict[str, asyncio.Semaphore] = {}

    @staticmethod
    def _validate_limits(values: Mapping[str, int] | None, label: str) -> dict[str, int]:
        result: dict[str, int] = {}
        for raw_name, raw_limit in (values or {}).items():
            name = str(raw_name).strip().lower()
            limit = int(raw_limit)
            if not name or limit < 1 or limit > 100:
                raise ValueError(f"{label} concurrency limits must be between 1 and 100.")
            result[name] = limit
        return result

    @property
    def available_tools(self) -> tuple[str, ...]:
        """Return registered tool names in deterministic order."""
        return tuple(sorted(self._tools))

    def route_plan(
        self,
        plan: ResearchPlan,
        *,
        expand_variants: bool = False,
        raise_on_clarification: bool = False,
    ) -> list[SearchTask]:
        return route_plan(
            plan,
            expand_variants=expand_variants,
            raise_on_clarification=raise_on_clarification,
        )

    def _provider_for(self, name: str, tool: ResearchTool) -> str:
        configured = self._tool_providers.get(name)
        if configured is not None:
            return configured
        provider = str(getattr(tool, "provider", "")).strip().lower()
        return provider or name

    def _execution_caps(self, plan: ResearchPlan | None) -> _ExecutionCaps:
        settings = self._settings
        plan_source = plan.source_budget if plan is not None else 8
        plan_tasks = plan.task_budget if plan is not None else 12
        plan_queries = plan.query_budget if plan is not None else 6
        plan_variants = plan.variant_budget if plan is not None else 6
        source_budget = _bounded_min(
            plan_source,
            getattr(settings, "source_budget", None),
        )
        candidate_budget = _bounded_min(
            source_budget,
            getattr(settings, "sources_per_job", None),
        )
        task_budget = _bounded_min(
            plan_tasks,
            source_budget,
            candidate_budget,
            getattr(settings, "search_tasks_per_job", None),
            getattr(settings, "task_budget", None),
        )
        return _ExecutionCaps(
            tasks=task_budget,
            queries=_bounded_min(
                plan_queries,
                getattr(settings, "search_subqueries_per_job", None),
                getattr(settings, "query_budget", None),
            ),
            variants=_bounded_min(
                plan_variants,
                getattr(settings, "search_variants_per_job", None),
                getattr(settings, "variant_budget", None),
            ),
            results=source_budget,
            candidates=candidate_budget,
        )

    def _semaphore(
        self,
        store: dict[str, asyncio.Semaphore],
        key: str,
        limits: Mapping[str, int],
        default: int | None,
    ) -> asyncio.Semaphore | None:
        limit = limits.get(key, default)
        if limit is None:
            return None
        if key not in store:
            store[key] = asyncio.Semaphore(limit)
        return store[key]

    async def _limited_search(
        self,
        task: SearchTask,
        tool: ResearchTool,
        *,
        global_deadline: float | None = None,
    ) -> object:
        name = task.tool_name.strip().lower()
        provider = self._provider_for(name, tool)
        semaphores = [
            self._semaphore(
                self._tool_semaphores,
                name,
                self._tool_limits,
                self._default_tool_limit,
            ),
            self._semaphore(
                self._provider_semaphores,
                provider,
                self._provider_limits,
                self._default_provider_limit,
            ),
        ]
        acquired: list[asyncio.Semaphore] = []
        try:
            for semaphore in semaphores:
                if semaphore is not None:
                    await semaphore.acquire()
                    acquired.append(semaphore)
            if global_deadline is not None:
                remaining = global_deadline - time.monotonic()
                remaining = min(remaining, timeout_for_task(task, float("inf")))
                if remaining <= 0:
                    raise TimeoutError("Tool request deadline exceeded.")
                filters = dict(task.filters)
                filters["request_timeout_seconds"] = f"{remaining:.6f}"
                task = task.model_copy(update={"filters": filters})
            return await tool.search(task)
        finally:
            for semaphore in reversed(acquired):
                semaphore.release()

    @staticmethod
    def _deadline_seconds(deadline: datetime | float | None) -> float | None:
        if deadline is None:
            return None
        if isinstance(deadline, datetime):
            instant = deadline if deadline.tzinfo is not None else deadline.replace(tzinfo=UTC)
            return (instant - datetime.now(UTC)).total_seconds()
        try:
            value = float(deadline)
        except (TypeError, ValueError):
            raise ValueError("deadline must be seconds or a timezone-aware datetime.") from None
        if not math.isfinite(value):
            raise ValueError("deadline must be finite.")
        return value

    def _task_timeout(self, task: SearchTask, global_deadline: float | None) -> float | None:
        default = self._request_timeout_seconds or float("inf")
        timeout = timeout_for_task(task, default)
        if global_deadline is not None:
            timeout = min(timeout, global_deadline - time.monotonic())
        return timeout

    @staticmethod
    def _task_with_limits(
        task: SearchTask, *, result_limit: int, timeout: float | None
    ) -> SearchTask:
        filters = dict(task.filters)
        filters["max_results"] = str(result_limit)
        if timeout is not None and math.isfinite(timeout):
            filters["request_timeout_seconds"] = f"{max(0.0, timeout):.6f}"
        return task.model_copy(update={"filters": filters})

    @staticmethod
    def _task_key(value: str) -> str:
        return " ".join(value.strip().split()).casefold()

    def _bounded_tasks(self, tasks: Sequence[SearchTask], caps: _ExecutionCaps) -> list[SearchTask]:
        """Admit a bounded, deterministic subset of an otherwise untrusted plan."""
        selected: list[SearchTask] = []
        seen_task_ids: set[str] = set()
        seen_queries: set[str] = set()
        seen_variants: set[str] = set()
        for task in itertools.islice(tasks, caps.tasks):
            if not isinstance(task, SearchTask):
                raise TypeError("tasks must contain SearchTask instances")
            task_id = task.task_id.strip()
            query_key = self._task_key(task.query)
            if task_id in seen_task_ids or not query_key:
                continue
            if query_key not in seen_queries and len(seen_queries) >= caps.queries:
                continue
            if query_key not in seen_variants and len(seen_variants) >= caps.variants:
                continue
            seen_task_ids.add(task_id)
            seen_queries.add(query_key)
            seen_variants.add(query_key)
            selected.append(task)
        return selected

    @staticmethod
    def _result_quotas(tasks: Sequence[SearchTask], caps: _ExecutionCaps) -> list[int]:
        """Split the candidate budget deterministically before concurrent execution."""
        remaining = min(caps.results, caps.candidates)
        quotas: list[int] = []
        for index, task in enumerate(tasks):
            requests_left = len(tasks) - index
            requested = max_results_from_filters(task.filters, default=5, maximum=10)
            fair_share = max(1, (remaining + requests_left - 1) // requests_left)
            quota = min(requested, fair_share)
            quotas.append(quota)
            remaining -= quota
        return quotas

    async def _record_attempt(self, attempt: ToolAttempt) -> None:
        if self._recorder is None:
            return
        result = _recorder_call(self._recorder, attempt)
        if inspect.isawaitable(result):
            await cast(Awaitable[object], result)

    async def _record_attempt_durably(self, attempt: ToolAttempt, completed: list[bool]) -> None:
        """Finish a recorder write before propagating cancellation to its caller."""
        if self._recorder is None:
            completed[0] = True
            return
        recording = asyncio.create_task(self._record_attempt(attempt))
        try:
            await asyncio.shield(recording)
        except asyncio.CancelledError:
            try:
                await recording
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - preserve outer cancellation
                logger.warning("tool attempt recording failed: %s", redact_text(str(exc))[:200])
            else:
                completed[0] = True
            raise
        completed[0] = True

    async def _record_attempts(
        self,
        attempts: Sequence[ToolAttempt],
        recorded: set[int],
    ) -> Exception | None:
        """Record all attempts in task order and return, rather than hide, failures."""
        first_error: Exception | None = None
        for index, attempt in enumerate(attempts):
            if index in recorded:
                continue
            completed = [False]
            try:
                await self._record_attempt_durably(attempt, completed)
            except asyncio.CancelledError:
                if completed[0]:
                    recorded.add(index)
                raise
            except Exception as exc:  # noqa: BLE001 - continue recording later attempts
                if first_error is None:
                    first_error = exc
                continue
            recorded.add(index)
        return first_error

    async def _execute_one(
        self,
        task: SearchTask,
        index: int,
        job_id: str,
        global_deadline: float | None,
        result_limit: int,
        attempts: list[ToolAttempt | None],
        task_hits: list[list[SearchHit]],
        deadline_expired: list[bool],
    ) -> None:
        started = time.monotonic()
        success = False
        cancelled = False
        outcome: AttemptOutcome | None = None
        error_category: ErrorCategory | None = None
        error_message: str | None = None
        http_status: int | None = None
        retry_after: float | None = None
        hits: list[SearchHit] = []
        tool_name = task.tool_name.strip().lower()
        tool = self._tools.get(tool_name)
        try:
            if tool is None:
                raise ToolError(
                    f"Tool {task.tool_name!r} is not registered.",
                    category=ErrorCategory.UNAVAILABLE,
                    tool_name=tool_name,
                )
            timeout = self._task_timeout(task, global_deadline)
            if timeout is not None and timeout <= 0:
                raise TimeoutError("Tool request deadline exceeded.")
            execution_task = self._task_with_limits(
                task,
                result_limit=result_limit,
                timeout=timeout,
            )
            if timeout is None or math.isinf(timeout):
                raw = await self._limited_search(
                    execution_task, tool, global_deadline=global_deadline
                )
            else:
                async with asyncio.timeout(timeout):
                    raw = await self._limited_search(
                        execution_task, tool, global_deadline=global_deadline
                    )
            hits = normalize_hits(raw, tool_name, max_results=result_limit)
            success = True
            outcome = AttemptOutcome.SUCCESS
        except asyncio.CancelledError:
            if deadline_expired[0]:
                outcome = AttemptOutcome.TIMEOUT
                error_category = ErrorCategory.TIMEOUT
                error_message = "Tool request deadline exceeded."
            else:
                cancelled = True
                outcome = AttemptOutcome.CANCELLED
                error_category = ErrorCategory.CANCELLED
                error_message = "Tool request cancelled."
            raise
        except TimeoutError:
            outcome = AttemptOutcome.TIMEOUT
            error_category = ErrorCategory.TIMEOUT
            error_message = "Tool request deadline exceeded."
        except ToolError as exc:
            error_category = ErrorCategory(_error_category(exc))
            error_message = _sanitize_error(exc)
            http_status, retry_after = _http_metadata(exc)
            outcome = (
                AttemptOutcome.TIMEOUT
                if error_category is ErrorCategory.TIMEOUT
                else AttemptOutcome.FAILURE
            )
        except Exception as exc:  # noqa: BLE001 - one bad tool must not cancel peers
            error_category = ErrorCategory(_error_category(exc))
            error_message = _sanitize_error(exc)
            http_status, retry_after = _http_metadata(exc)
            outcome = (
                AttemptOutcome.TIMEOUT
                if error_category is ErrorCategory.TIMEOUT
                else AttemptOutcome.FAILURE
            )
        finally:
            task_hits[index] = hits
            attempt = ToolAttempt(
                job_id=job_id,
                task_id=task.task_id,
                tool_name=tool_name,
                success=success,
                outcome=outcome,
                duration_ms=max(0, round((time.monotonic() - started) * 1000)),
                result_count=len(hits),
                error_category=error_category,
                error_message=error_message,
                quota_metadata=_quota_metadata(tool) if tool is not None else {},
                cancelled=cancelled,
                http_status=http_status,
                retry_after=retry_after,
                task_index=index,
            )
            attempts[index] = attempt

    async def _wait_workers(
        self,
        workers: Sequence[asyncio.Task[None]],
        *,
        global_deadline: float | None,
        deadline_expired: list[bool],
        execution_cancelled: list[bool],
    ) -> list[object]:
        """Wait for workers, stopping the whole fan-out on cancellation or deadline."""
        pending: set[asyncio.Task[None]] = set(workers)
        while pending:
            timeout = None
            if global_deadline is not None:
                timeout = max(0.0, global_deadline - time.monotonic())
            done, pending = await asyncio.wait(
                pending,
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                deadline_expired[0] = True
                for worker in pending:
                    worker.cancel()
                break
            stopped = False
            for worker in done:
                if worker.cancelled():
                    execution_cancelled[0] = True
                    stopped = True
                    continue
                if worker.exception() is not None:
                    stopped = True
            if stopped:
                for worker in pending:
                    worker.cancel()
                break
        return list(await asyncio.gather(*workers, return_exceptions=True))

    @staticmethod
    async def _cancel_workers(workers: Sequence[asyncio.Task[None]]) -> None:
        for worker in workers:
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    @staticmethod
    def _fill_missing_attempts(
        tasks: Sequence[SearchTask],
        attempts: list[ToolAttempt | None],
        task_hits: list[list[SearchHit]],
        *,
        job_id: str,
        deadline_expired: bool,
        execution_cancelled: bool,
    ) -> None:
        """Account for workers cancelled before their coroutine body started."""
        for index, task in enumerate(tasks):
            if attempts[index] is not None:
                continue
            if deadline_expired:
                current_outcome = AttemptOutcome.TIMEOUT
                category = ErrorCategory.TIMEOUT
                message = "Tool request deadline exceeded."
                current_cancelled = False
            elif execution_cancelled:
                current_outcome = AttemptOutcome.CANCELLED
                category = ErrorCategory.CANCELLED
                message = "Tool request cancelled."
                current_cancelled = True
            else:
                current_outcome = AttemptOutcome.FAILURE
                category = ErrorCategory.UNKNOWN
                message = "Tool worker stopped before execution."
                current_cancelled = False
            attempts[index] = ToolAttempt(
                job_id=job_id,
                task_id=task.task_id,
                tool_name=task.tool_name.strip().lower(),
                success=False,
                outcome=current_outcome,
                duration_ms=0,
                result_count=0,
                error_category=category,
                error_message=message,
                cancelled=current_cancelled,
                task_index=index,
            )
            task_hits[index] = []

    @staticmethod
    def _execution_result(
        tasks: Sequence[SearchTask],
        task_hits: Sequence[list[SearchHit]],
        attempts: Sequence[ToolAttempt | None],
        plan: ResearchPlan | None = None,
    ) -> ToolExecutionResult:
        accepted_hits: list[SearchHit] = []
        result_by_task: dict[str, list[SearchHit]] = {}
        for task, hits in zip(tasks, task_hits, strict=True):
            result_by_task[task.task_id] = list(hits)
            accepted_hits.extend(hits)
        canonical_hits = canonicalize_hits(accepted_hits)
        deduplicated_hits = deduplicate_hits(canonical_hits)
        query: str | ResearchPlan = plan or (tasks[0].query if tasks else "")
        ranked_hits = rank_sources(deduplicated_hits, query)
        return ToolExecutionResult(
            hits=accepted_hits,
            ranked_hits=ranked_hits,
            source_candidates=source_candidates_from_ranked_hits(ranked_hits),
            attempts=[attempt for attempt in attempts if attempt is not None],
            task_hits=result_by_task,
        )

    async def execute(
        self,
        tasks: Sequence[SearchTask],
        *,
        job_id: str = "",
        deadline: datetime | float | None = None,
        timeout_seconds: float | None = None,
        plan: ResearchPlan | None = None,
    ) -> ToolExecutionResult:
        """Execute tasks concurrently while isolating individual failures."""

        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive or None.")
        if plan is not None and (plan.needs_clarification or plan.source_url is not None):
            return ToolExecutionResult()
        global_seconds = self._deadline_seconds(deadline)
        if timeout_seconds is not None:
            global_seconds = (
                timeout_seconds if global_seconds is None else min(global_seconds, timeout_seconds)
            )
        global_deadline = time.monotonic() + global_seconds if global_seconds is not None else None
        caps = self._execution_caps(plan)
        task_list = self._bounded_tasks(tasks, caps)
        attempts: list[ToolAttempt | None] = [None] * len(task_list)
        task_hits: list[list[SearchHit]] = [[] for _ in task_list]
        deadline_expired = [False]
        execution_cancelled = [False]
        quotas = self._result_quotas(task_list, caps)
        workers = [
            asyncio.create_task(
                self._execute_one(
                    task,
                    index,
                    job_id,
                    global_deadline,
                    quotas[index],
                    attempts,
                    task_hits,
                    deadline_expired,
                )
            )
            for index, task in enumerate(task_list)
        ]
        gathered: list[object] = []
        recorded: set[int] = set()
        try:
            if workers:
                gathered = await self._wait_workers(
                    workers,
                    global_deadline=global_deadline,
                    deadline_expired=deadline_expired,
                    execution_cancelled=execution_cancelled,
                )
            self._fill_missing_attempts(
                task_list,
                attempts,
                task_hits,
                job_id=job_id,
                deadline_expired=deadline_expired[0],
                execution_cancelled=execution_cancelled[0],
            )
        except asyncio.CancelledError:
            execution_cancelled[0] = True
            await self._cancel_workers(workers)
            self._fill_missing_attempts(
                task_list,
                attempts,
                task_hits,
                job_id=job_id,
                deadline_expired=False,
                execution_cancelled=True,
            )
            try:
                record_error = await self._record_attempts(
                    [attempt for attempt in attempts if attempt is not None], recorded
                )
            except asyncio.CancelledError as recorder_cancelled:
                logger.warning("tool attempt recording was cancelled: %s", recorder_cancelled)
            else:
                if record_error is not None:
                    logger.warning(
                        "tool attempt recording failed during cancellation: %s",
                        redact_text(str(record_error))[:200],
                    )
            raise

        ordered_attempts = [attempt for attempt in attempts if attempt is not None]
        try:
            record_error = await self._record_attempts(ordered_attempts, recorded)
        except asyncio.CancelledError:
            # Workers have finished; complete any remaining durable writes before
            # allowing the caller's cancellation to leave this method.
            try:
                await self._record_attempts(ordered_attempts, recorded)
            except asyncio.CancelledError:
                pass
            raise
        if record_error is not None:
            raise record_error
        if execution_cancelled[0] and not deadline_expired[0]:
            raise asyncio.CancelledError
        for outcome in gathered:
            if isinstance(outcome, BaseException) and not isinstance(
                outcome, asyncio.CancelledError
            ):
                raise outcome
        return self._execution_result(task_list, task_hits, attempts)

    async def execute_tasks(
        self,
        tasks: Sequence[SearchTask],
        *,
        job_id: str = "",
        deadline: datetime | float | None = None,
        timeout_seconds: float | None = None,
        plan: ResearchPlan | None = None,
    ) -> ToolExecutionResult:
        """Alias for :meth:`execute` for callers using the task-oriented name."""

        return await self.execute(
            tasks,
            job_id=job_id,
            deadline=deadline,
            timeout_seconds=timeout_seconds,
            plan=plan,
        )

    async def execute_plan(
        self,
        plan: ResearchPlan,
        *,
        job_id: str = "",
        deadline: datetime | float | None = None,
        timeout_seconds: float | None = None,
        expand_variants: bool = False,
    ) -> ToolExecutionResult:
        """Route and execute one plan using its configured time budget."""

        if plan.needs_clarification:
            raise ClarificationRequiredError(
                plan.clarification_question or "Clarification is required before searching."
            )
        tasks = self.route_plan(plan, expand_variants=expand_variants)
        return await self.execute(
            tasks,
            job_id=job_id,
            deadline=deadline,
            timeout_seconds=(
                plan.time_budget_seconds
                if timeout_seconds is None and deadline is None
                else timeout_seconds
            ),
            plan=plan,
        )
