"""Deterministic search routing and bounded asynchronous tool execution."""

from __future__ import annotations

import asyncio
import inspect
import math
import re
import time
from collections.abc import Awaitable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast

from pydantic import ValidationError

from research_agent.errors import ErrorCategory
from research_agent.models.reports import canonicalize_url
from research_agent.models.research import ResearchPlan, SearchHit, SearchTask
from research_agent.observability.redaction import redact_mapping, redact_text
from research_agent.tools._common import timeout_for_task
from research_agent.tools.base import ResearchTool, ToolError

if TYPE_CHECKING:
    import aiosqlite

    from research_agent.persistence.repositories import ToolRunRepository

__all__ = [
    "AttemptRecorder",
    "SOURCE_PRIORITIES",
    "ToolAttempt",
    "ToolExecutionResult",
    "ToolRouter",
    "ToolRunRecorder",
    "normalize_hit",
    "normalize_hits",
    "route_plan",
]


# Keep this table aligned with PLAN section 8.  A plan's explicit selection is
# authoritative; these values are only used for manually-created plans that
# omit ``tools_selected``.
SOURCE_PRIORITIES: dict[str, tuple[str, ...]] = {
    "general": ("tavily", "brave_search", "exa", "searxng", "ddgs"),
    "academic": ("semantic_scholar", "crossref", "arxiv", "openalex", "tavily"),
    "medical": ("pubmed", "europe_pmc", "official_domains", "semantic_scholar", "tavily"),
    "news": ("gdelt", "brave_search", "tavily"),
    "technical": ("official_domains", "github", "stack_exchange", "tavily"),
    "mena_local": ("brave_search", "tavily", "gdelt", "official_domains"),
    "legal": ("official_domains", "tavily", "brave_search"),
    "financial": ("official_domains", "tavily", "brave_search"),
    # Direct extraction belongs to M4, so the current analyzer's URL plan is
    # deliberately represented by the available discovery tools here.
    "url": ("official_domains", "tavily"),
}

_DOMAIN_CATEGORIES: dict[str, tuple[str, ...]] = {
    "general": ("web", "wiki"),
    "academic": ("academic", "paper", "web"),
    "medical": ("medical", "academic", "official"),
    "news": ("news", "web"),
    "technical": ("technical", "repository", "web"),
    "mena_local": ("news", "web", "official"),
    "legal": ("official", "web", "news"),
    "financial": ("official", "web", "news"),
    "url": ("official", "web"),
}
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


@dataclass(frozen=True)
class ToolAttempt:
    """Sanitized accounting record for one attempted tool request."""

    job_id: str
    task_id: str
    tool_name: str
    success: bool
    duration_ms: int = 0
    result_count: int = 0
    error_category: str | None = None
    error_message: str | None = None
    quota_metadata: dict[str, object] = field(default_factory=dict)
    cancelled: bool = False


@dataclass
class ToolExecutionResult:
    """Normalized output and accounting for a group of tool requests."""

    hits: list[SearchHit] = field(default_factory=list)
    attempts: list[ToolAttempt] = field(default_factory=list)
    task_hits: dict[str, list[SearchHit]] = field(default_factory=dict)

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
            duration_ms=attempt.duration_ms,
            result_count=attempt.result_count,
            error_category=attempt.error_category,
            error_message=attempt.error_message,
            quota_metadata=attempt.quota_metadata,
        )


def _selected_tools(plan: ResearchPlan) -> list[str]:
    domain = plan.domain.value
    names = plan.tools_selected or list(SOURCE_PRIORITIES.get(domain, SOURCE_PRIORITIES["general"]))
    if _URL_RE.search(plan.query) is not None:
        names = plan.tools_selected or list(SOURCE_PRIORITIES["url"])
    selected: list[str] = []
    seen: set[str] = set()
    for raw_name in names:
        name = str(raw_name).strip().lower()
        if name and name not in seen:
            selected.append(name)
            seen.add(name)
    return selected


def route_plan(plan: ResearchPlan, *, expand_variants: bool = False) -> list[SearchTask]:
    """Turn a validated plan into ordered, deterministic search tasks.

    One request per selected tool is the default.  Callers that explicitly
    want every bilingual/subquestion variant can opt into the Cartesian
    expansion without changing the plan's source priority order.
    """

    tools = _selected_tools(plan)
    if not tools:
        return []
    domain = "url" if _URL_RE.search(plan.query) is not None else plan.domain.value
    categories = plan.source_categories or list(_DOMAIN_CATEGORIES.get(domain, ("web",)))
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
    per_tool = max(1, math.ceil(plan.source_budget / len(tools)))
    freshness = "pw" if plan.requires_freshness else ""
    tasks: list[SearchTask] = []
    task_number = 0
    for variant_index, query in enumerate(variants):
        for priority, tool_name in enumerate(tools):
            task_number += 1
            filters = {
                "source_category": categories[min(priority, len(categories) - 1)],
                "source_priority": str(priority),
                "max_results": str(min(10, per_tool)),
            }
            if freshness:
                filters["freshness"] = freshness
            if plan.locality:
                filters["locality"] = plan.locality
            if plan.jurisdictions:
                filters["jurisdictions"] = ",".join(plan.jurisdictions)
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
    reaches the normalized result set.  Existing metadata is copied rather
    than replaced; only missing aliases such as ``content`` are filled in.
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
    existing_tool = data.get("tool_name")
    if not isinstance(existing_tool, str) or not existing_tool.strip():
        data["tool_name"] = tool_name
    else:
        data["tool_name"] = existing_tool.strip()

    raw_tool_names = data.get("tool_names", [])
    tool_names: list[str] = []
    if isinstance(raw_tool_names, (list, tuple, set)):
        tool_names = [str(name).strip() for name in raw_tool_names if str(name).strip()]
    if str(data["tool_name"]).strip() not in tool_names:
        tool_names.insert(0, str(data["tool_name"]).strip())
    data["tool_names"] = tool_names

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
    for key in ("doi", "content_hash"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            data[key] = str(value)
    try:
        return SearchHit.model_validate(data)
    except (ValidationError, TypeError, ValueError):
        return None


def normalize_hits(raw: object, tool_name: str) -> list[SearchHit]:
    """Normalize all valid items in one adapter response."""

    if isinstance(raw, SearchHit):
        items: Iterable[object] = (raw,)
    elif isinstance(raw, (list, tuple)):
        items = raw
    else:
        return []
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
    return value or ErrorCategory.UNKNOWN.value


def _quota_metadata(tool: object) -> dict[str, object]:
    for attribute in ("quota_metadata", "last_quota_metadata"):
        value = getattr(tool, attribute, None)
        if isinstance(value, Mapping):
            redacted = redact_mapping({str(key): item for key, item in value.items()})
            return {str(key): item for key, item in redacted.items()}
    return {}


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
    return cast(
        Awaitable[object] | object,
        target(
            job_id=attempt.job_id,
            task_id=attempt.task_id,
            tool_name=attempt.tool_name,
            success=attempt.success,
            duration_ms=attempt.duration_ms,
            result_count=attempt.result_count,
            error_category=attempt.error_category,
            error_message=attempt.error_message,
            quota_metadata=attempt.quota_metadata,
            cancelled=attempt.cancelled,
        ),
    )


class ToolRouter:
    """Route plans and execute independent search tasks with bounded fan-out."""

    def __init__(
        self,
        tools: Mapping[str, ResearchTool] | Iterable[ResearchTool] = (),
        *,
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
        self._tool_limits = self._validate_limits(tool_limits, "tool")
        self._provider_limits = self._validate_limits(provider_limits, "provider")
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
            if not name or limit < 1:
                raise ValueError(f"{label} concurrency limits must be positive.")
            result[name] = limit
        return result

    @property
    def available_tools(self) -> tuple[str, ...]:
        """Return registered tool names in deterministic order."""
        return tuple(sorted(self._tools))

    def route_plan(self, plan: ResearchPlan, *, expand_variants: bool = False) -> list[SearchTask]:
        return route_plan(plan, expand_variants=expand_variants)

    def _provider_for(self, name: str, tool: ResearchTool) -> str:
        return self._tool_providers.get(
            name,
            str(getattr(tool, "provider", name)).strip().lower() or name,
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

    async def _limited_search(self, task: SearchTask, tool: ResearchTool) -> object:
        name = task.tool_name.strip().lower()
        provider = self._provider_for(name, tool)
        semaphores = [
            self._semaphore(self._tool_semaphores, name, self._tool_limits, 1),
            self._semaphore(self._provider_semaphores, provider, self._provider_limits, None),
        ]
        acquired: list[asyncio.Semaphore] = []
        try:
            for semaphore in semaphores:
                if semaphore is not None:
                    await semaphore.acquire()
                    acquired.append(semaphore)
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

    async def _record_attempt(self, attempt: ToolAttempt) -> None:
        if self._recorder is None:
            return
        try:
            result = _recorder_call(self._recorder, attempt)
            if inspect.isawaitable(result):
                await cast(Awaitable[object], result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - persistence is optional
            # Do not allow metrics storage to turn a usable partial search into
            # a failed job, and do not log an unredacted provider exception.
            safe_message = redact_text(str(exc))[:200]
            import logging

            logging.getLogger(__name__).warning("tool attempt recording failed: %s", safe_message)

    async def _execute_one(
        self,
        task: SearchTask,
        index: int,
        job_id: str,
        global_deadline: float | None,
        attempts: list[ToolAttempt | None],
        task_hits: list[list[SearchHit]],
        deadline_expired: list[bool],
    ) -> None:
        started = time.monotonic()
        success = False
        cancelled = False
        error_category: str | None = None
        error_message: str | None = None
        hits: list[SearchHit] = []
        tool = self._tools.get(task.tool_name.strip().lower())
        try:
            if tool is None:
                raise ToolError(
                    f"Tool {task.tool_name!r} is not registered.",
                    category=ErrorCategory.UNAVAILABLE,
                    tool_name=task.tool_name,
                )
            timeout = self._task_timeout(task, global_deadline)
            if timeout is not None and timeout <= 0:
                raise TimeoutError("Tool request deadline exceeded.")
            if timeout is None or math.isinf(timeout):
                raw = await self._limited_search(task, tool)
            else:
                async with asyncio.timeout(timeout):
                    raw = await self._limited_search(task, tool)
            hits = normalize_hits(raw, task.tool_name)
            success = True
        except asyncio.CancelledError:
            if deadline_expired[0]:
                error_category = ErrorCategory.TIMEOUT.value
                error_message = "Tool request deadline exceeded."
            else:
                cancelled = True
                error_category = "cancelled"
                error_message = "Tool request cancelled."
            raise
        except TimeoutError:
            error_category = ErrorCategory.TIMEOUT.value
            error_message = "Tool request deadline exceeded."
        except ToolError as exc:
            error_category = _error_category(exc)
            error_message = _sanitize_error(exc)
        except Exception as exc:  # noqa: BLE001 - one bad tool must not cancel peers
            error_category = _error_category(exc)
            error_message = _sanitize_error(exc)
        finally:
            task_hits[index] = hits
            attempt = ToolAttempt(
                job_id=job_id,
                task_id=task.task_id,
                tool_name=task.tool_name,
                success=success,
                duration_ms=max(0, round((time.monotonic() - started) * 1000)),
                result_count=len(hits),
                error_category=error_category,
                error_message=error_message,
                quota_metadata=_quota_metadata(tool) if tool is not None else {},
                cancelled=cancelled,
            )
            attempts[index] = attempt
            try:
                await asyncio.shield(self._record_attempt(attempt))
            except asyncio.CancelledError:
                # Preserve the tool cancellation semantics after best-effort
                # persistence.  The attempt was already placed in ``attempts``.
                if not cancelled:
                    raise
            except Exception as exc:  # noqa: BLE001 - preserve tool cancellation semantics
                # _record_attempt already handles ordinary recorder failures;
                # this guard also protects unusual awaitable implementations.
                if not cancelled:
                    import logging

                    logging.getLogger(__name__).debug(
                        "tool attempt finalization failed: %s", redact_text(str(exc))[:200]
                    )

    async def execute(
        self,
        tasks: Sequence[SearchTask],
        *,
        job_id: str = "",
        deadline: datetime | float | None = None,
        timeout_seconds: float | None = None,
    ) -> ToolExecutionResult:
        """Execute tasks concurrently while isolating individual failures."""

        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive or None.")
        global_seconds = self._deadline_seconds(deadline)
        if timeout_seconds is not None:
            global_seconds = (
                timeout_seconds if global_seconds is None else min(global_seconds, timeout_seconds)
            )
        global_deadline = time.monotonic() + global_seconds if global_seconds is not None else None
        task_list = list(tasks)
        attempts: list[ToolAttempt | None] = [None] * len(task_list)
        task_hits: list[list[SearchHit]] = [[] for _ in task_list]
        deadline_expired = [False]
        workers = [
            asyncio.create_task(
                self._execute_one(
                    task,
                    index,
                    job_id,
                    global_deadline,
                    attempts,
                    task_hits,
                    deadline_expired,
                )
            )
            for index, task in enumerate(task_list)
        ]
        gathered: list[object] = []
        if workers:
            try:
                if global_seconds is None or math.isinf(global_seconds):
                    gathered = list(await asyncio.gather(*workers, return_exceptions=True))
                else:
                    _, pending = await asyncio.wait(workers, timeout=max(0.0, global_seconds))
                    if pending:
                        deadline_expired[0] = True
                        for worker in pending:
                            worker.cancel()
                    gathered = list(await asyncio.gather(*workers, return_exceptions=True))
            except asyncio.CancelledError:
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                raise
        if not deadline_expired[0]:
            for outcome in gathered:
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
        accepted_hits: list[SearchHit] = []
        result_by_task: dict[str, list[SearchHit]] = {}
        for task, hits in zip(task_list, task_hits, strict=True):
            result_by_task[task.task_id] = list(hits)
            accepted_hits.extend(hits)
        recorded_attempts = [attempt for attempt in attempts if attempt is not None]
        return ToolExecutionResult(
            hits=accepted_hits,
            attempts=recorded_attempts,
            task_hits=result_by_task,
        )

    async def execute_tasks(
        self,
        tasks: Sequence[SearchTask],
        *,
        job_id: str = "",
        deadline: datetime | float | None = None,
        timeout_seconds: float | None = None,
    ) -> ToolExecutionResult:
        """Alias for :meth:`execute` for callers using the task-oriented name."""

        return await self.execute(
            tasks,
            job_id=job_id,
            deadline=deadline,
            timeout_seconds=timeout_seconds,
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

        return await self.execute(
            self.route_plan(plan, expand_variants=expand_variants),
            job_id=job_id,
            deadline=deadline,
            timeout_seconds=(
                plan.time_budget_seconds
                if timeout_seconds is None and deadline is None
                else timeout_seconds
            ),
        )
