"""Telegram command handlers."""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import aiosqlite
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message
from pydantic import ValidationError

if TYPE_CHECKING:
    from research_agent.services.queue import BoundedJobQueue

from research_agent.models.requests import ResearchRequest
from research_agent.persistence.repositories import (
    SessionRepository,
    ToolRunRepository,
    UserRepository,
)
from research_agent.services.queue import (
    JobRef,
    UserBusyError,
    cancel_user_job,
    enqueue_request,
    get_user_active_job,
    queue_position,
)
from research_agent.services.reports import get_report_bundle, list_recent_reports
from research_agent.services.sessions import (
    ensure_user,
    get_language,
    normalize_language,
    set_language,
)
from research_agent.telegram.renderer import (
    deliver_report,
    render_concise_report,
    report_filename,
    split_message,
)
from research_agent.telegram.texts import (
    TelegramLimits,
    format_history,
    format_status,
    pick_lang,
    render_busy,
    render_cancel_none,
    render_cancelled,
    render_forget_done,
    render_help,
    render_invalid_url,
    render_language_current,
    render_language_invalid,
    render_language_set,
    render_non_text,
    render_private_chat_only,
    render_report_failure,
    render_report_not_found,
    render_report_usage,
    render_research_accepted,
    render_research_usage,
    render_start,
    render_too_long,
    render_unavailable,
    render_whoami,
)
from research_agent.telegram.validators import classify_input, is_accepted_url

router = Router()

LangCode = Literal["en", "ar"]


@dataclass
class RequestCtx:
    """Bundled per-request DB handles plus a normalized language code."""

    conn: aiosqlite.Connection | None = None
    db_path: Path | str | None = None
    lang_code: LangCode = "en"
    job_queue: BoundedJobQueue | None = None


def resolve_lang(raw: str | None) -> LangCode:
    """Normalize a Telegram language code once per handler to en/ar."""
    normalized = normalize_language(raw)
    if normalized == "en":
        return "en"
    if normalized == "ar":
        return "ar"
    # Fall back to prefix matching so unknown Arabic variants (e.g. ar-DZ)
    # still resolve to Arabic instead of incorrectly falling back to English.
    return "ar" if pick_lang(raw) == "ar" else "en"


def build_ctx(
    message: Message,
    conn: aiosqlite.Connection | None = None,
    db_path: Path | str | None = None,
    job_queue: BoundedJobQueue | None = None,
) -> RequestCtx:
    """Build a RequestCtx with language normalized once from the sender."""
    from_user = message.from_user
    raw: str | None = from_user.language_code if from_user is not None else None
    effective_db_path = db_path
    if effective_db_path is None and job_queue is not None:
        effective_db_path = job_queue.db_path
    return RequestCtx(
        conn=conn,
        db_path=effective_db_path,
        lang_code=resolve_lang(raw),
        job_queue=job_queue,
    )


async def resolve_preferred_lang(ctx: RequestCtx, user_id: int) -> LangCode:
    """Resolve a stored user/session preference before Telegram's fallback."""
    async with with_db(ctx.conn, ctx.db_path) as db_conn:
        if db_conn is None:
            return ctx.lang_code
        try:
            user = await UserRepository().get(db_conn, user_id)
            session = await SessionRepository().get(db_conn, user_id)
            if user is None and session is None:
                return ctx.lang_code
            return resolve_lang(await get_language(db_conn, user_id))
        except Exception:  # noqa: BLE001 - language lookup must not block a reply
            return ctx.lang_code


def is_group_chat(message: Message) -> bool:
    """Return whether a Telegram message came from a group or supergroup."""
    chat = getattr(message, "chat", None)
    return getattr(chat, "type", None) in {"group", "supergroup"}


async def reject_group_chat(message: Message, lang_code: LangCode) -> bool:
    """Reject operations that could expose jobs, progress, or reports in groups."""
    if not is_group_chat(message):
        return False
    await message.answer(render_private_chat_only(lang_code))
    return True


# The progress module's public registry stores the editable target, while its
# current implementation does not store the stage. Keep a bounded adapter-side
# view until that public API carries stage data itself.
_MAX_TRACKED_PROGRESS = 256
_PROGRESS_STAGES: OrderedDict[str, str] = OrderedDict()
_PROGRESS_LANGS: OrderedDict[str, LangCode] = OrderedDict()
_TERMINAL_PROGRESS_JOBS: OrderedDict[str, None] = OrderedDict()
_WIRED_QUEUE_IDS: set[int] = set()
_PROGRESS_TRACKING_INSTALLED = False


def _remember_progress(job_id: str, lang_code: LangCode, stage: str = "analyzing") -> None:
    """Track stage and language for the public progress API adapter."""
    _TERMINAL_PROGRESS_JOBS.pop(job_id, None)
    _PROGRESS_STAGES.pop(job_id, None)
    _PROGRESS_STAGES[job_id] = stage
    _PROGRESS_LANGS.pop(job_id, None)
    _PROGRESS_LANGS[job_id] = lang_code
    while len(_PROGRESS_STAGES) > _MAX_TRACKED_PROGRESS:
        old_job, _ = _PROGRESS_STAGES.popitem(last=False)
        _PROGRESS_LANGS.pop(old_job, None)


def _remember_stage(job_id: str, stage: object) -> None:
    """Record a normalized stage emitted by ``update_progress``."""
    value = getattr(stage, "value", stage)
    if not isinstance(value, str):
        return
    _PROGRESS_STAGES.pop(job_id, None)
    _PROGRESS_STAGES[job_id] = value
    while len(_PROGRESS_STAGES) > _MAX_TRACKED_PROGRESS:
        old_job, _ = _PROGRESS_STAGES.popitem(last=False)
        _PROGRESS_LANGS.pop(old_job, None)


def _clear_tracked_progress(job_id: str) -> None:
    """Clear both public progress data and adapter state for one job."""
    from research_agent.telegram.progress import clear_progress

    clear_progress(job_id)
    _PROGRESS_STAGES.pop(job_id, None)
    _PROGRESS_LANGS.pop(job_id, None)
    _TERMINAL_PROGRESS_JOBS.pop(job_id, None)


def clear_tracked_progress() -> None:
    """Clear all adapter-tracked progress entries during queue shutdown."""
    for job_id in tuple(_PROGRESS_STAGES):
        _clear_tracked_progress(job_id)
    _PROGRESS_STAGES.clear()
    _PROGRESS_LANGS.clear()
    _TERMINAL_PROGRESS_JOBS.clear()


def _mark_progress_terminal(job_id: str) -> None:
    """Clear a worker's progress and remember terminal completion briefly."""
    from research_agent.telegram.progress import clear_progress

    clear_progress(job_id)
    _PROGRESS_STAGES.pop(job_id, None)
    _PROGRESS_LANGS.pop(job_id, None)
    _TERMINAL_PROGRESS_JOBS.pop(job_id, None)
    _TERMINAL_PROGRESS_JOBS[job_id] = None
    while len(_TERMINAL_PROGRESS_JOBS) > _MAX_TRACKED_PROGRESS:
        _TERMINAL_PROGRESS_JOBS.popitem(last=False)


def _is_progress_terminal(job_id: str) -> bool:
    """Return whether the adapter observed the worker leave this job."""
    return job_id in _TERMINAL_PROGRESS_JOBS


def _install_progress_tracking() -> None:
    """Track stages and route worker edits through the resolved user language."""
    global _PROGRESS_TRACKING_INSTALLED
    if _PROGRESS_TRACKING_INSTALLED:
        return
    from research_agent.telegram import progress as progress_module

    original = progress_module.update_progress

    async def tracked_update(
        bot: Any,
        chat_id: int,
        message_id: int,
        stage: Any,
        lang: str = "en",
        *,
        job_id: str | None = None,
        position: int | None = None,
    ) -> bool:
        if job_id is not None:
            _remember_stage(job_id, stage)
            resolved = _PROGRESS_LANGS.get(job_id, resolve_lang(lang))
        else:
            resolved = resolve_lang(lang)
        return await original(
            bot,
            chat_id,
            message_id,
            stage,
            resolved,
            job_id=job_id,
            position=position,
        )

    progress_module.update_progress = tracked_update
    _PROGRESS_TRACKING_INSTALLED = True


def wire_queue_worker(queue: BoundedJobQueue) -> None:
    """Install progress cleanup around queue methods when no hook is exposed."""
    _install_progress_tracking()
    queue_id = id(queue)
    if queue_id in _WIRED_QUEUE_IDS:
        return
    original_worker = queue._run_with_semaphore
    original_stop = queue.stop

    async def tracked_worker(job: Any) -> None:
        try:
            await original_worker(job)
        finally:
            _mark_progress_terminal(str(job.job_id))

    async def tracked_stop() -> None:
        try:
            await original_stop()
        finally:
            clear_tracked_progress()

    # The queue currently has no terminal callback. Track the worker boundary
    # rather than only run_placeholder, because timeout/cancelled jobs may exit
    # before the placeholder is entered.
    queue._run_with_semaphore = tracked_worker  # type: ignore[method-assign]
    queue.stop = tracked_stop  # type: ignore[method-assign]
    _WIRED_QUEUE_IDS.add(queue_id)


@asynccontextmanager
async def with_db(
    conn: aiosqlite.Connection | None,
    db_path: Path | str | None,
) -> AsyncIterator[aiosqlite.Connection | None]:
    """Yield a usable DB connection, opening ``db_path`` when needed.

    When ``conn`` is provided it is yielded directly (caller owns its
    lifecycle). Otherwise ``db_path`` is opened via :func:`open_db`.
    Yields ``None`` when neither is available so callers can reply with
    a service-unavailable message instead of inventing fake records.
    """
    if conn is not None:
        yield conn
        return
    if db_path is not None:
        from research_agent.persistence.database import open_db

        async with open_db(db_path) as db_conn:
            yield db_conn
        return
    yield None


def extract_research_arg(text: str | None, command: str) -> str:
    """Extract the query argument following a /command prefix.

    Handles ``/research``, ``/research@botname``, extra whitespace, and
    preserves inner content (including Arabic RTL) verbatim. Matching is
    case-insensitive with a word boundary so ``/researcher`` is not treated
    as ``/research``; ``@mention`` may be separated by space or newline.
    """
    if not text:
        return ""
    stripped = text.strip()
    lowered = stripped.lower()
    cmd = command.lower()
    is_cmd = (
        lowered == cmd
        or lowered.startswith(cmd + " ")
        or lowered.startswith(cmd + "\n")
        or lowered.startswith(cmd + "@")
    )
    if not is_cmd:
        # Not this command: for plain-text routing return as-is, but a
        # leading-slash unknown command yields "" so callers show usage.
        if stripped.startswith("/"):
            return ""
        return stripped
    rest = stripped[len(command) :]
    if rest.startswith("@"):
        # Mention ends at first whitespace (space or newline).
        import re as _re

        m = _re.search(r"\s", rest)
        if m is None:
            return ""
        rest = rest[m.start() + 1 :]
    return rest.strip()


async def handle_research_request(
    message: Message,
    query: str,
    conn: aiosqlite.Connection | None = None,
    db_path: Path | str | None = None,
    job_queue: BoundedJobQueue | None = None,
) -> JobRef | None:
    """Validate a research query and enqueue it; reply with usage on empty.

    Accepts English, Arabic, and mixed-language text plus http(s) URLs.
    Inner content (including Arabic RTL) is preserved verbatim; only
    surrounding whitespace is stripped.
    """
    from_user = message.from_user
    if from_user is None:
        return None
    ctx = build_ctx(message, conn=conn, db_path=db_path, job_queue=job_queue)
    lang_code = await resolve_preferred_lang(ctx, from_user.id)
    if await reject_group_chat(message, lang_code):
        return None
    clean = query.strip()
    if not clean:
        await message.answer(render_research_usage(lang_code))
        return None
    # Leading-slash text that is not a known command: show usage, never enqueue.
    if clean.startswith("/"):
        await message.answer(render_research_usage(lang_code))
        return None
    if len(clean) > 4000 or (clean.startswith(("http://", "https://")) and len(clean) > 2000):
        await message.answer(render_too_long(lang_code))
        return None
    kind = classify_input(clean)
    if kind == "url" and not is_accepted_url(clean):
        await message.answer(render_invalid_url(lang_code))
        return None
    # URL-looking inputs that claim another scheme but were classified as
    # text should still be rejected when they contain a scheme-like prefix.
    lowered = clean.lower().lstrip()
    if kind == "text" and lowered.startswith(
        (
            "javascript:",
            "file:",
            "ftp:",
            "data:",
            "vbscript:",
            "mailto:",
            "tel:",
            "ssh:",
            "ws:",
            "wss:",
            "gopher:",
            "ldap:",
            "dict:",
        )
    ):
        await message.answer(render_invalid_url(lang_code))
        return None
    try:
        if kind == "url":
            request = ResearchRequest(
                user_id=from_user.id,
                query=clean,
                source_url=clean,  # type: ignore[arg-type]
            )
        else:
            request = ResearchRequest(user_id=from_user.id, query=clean)
    except ValidationError:
        # Distinguish overlong inputs from generic usage errors.
        if len(clean) > 4000:
            await message.answer(render_too_long(lang_code))
        elif kind == "url":
            await message.answer(render_invalid_url(lang_code))
        else:
            await message.answer(render_research_usage(lang_code))
        return None

    if ctx.job_queue is not None:
        wire_queue_worker(ctx.job_queue)

    async def _enqueue() -> JobRef | None:
        """Persist once and schedule when a live queue is injected."""
        if ctx.job_queue is not None:
            return await ctx.job_queue.enqueue(
                request.user_id,
                request.query,
                language=request.language.value,
            )
        async with with_db(ctx.conn, ctx.db_path) as db_conn:
            if db_conn is None:
                return None
            return await enqueue_request(
                db_conn,
                request.user_id,
                request.query,
                language=request.language.value,
            )

    try:
        job = await _enqueue()
    except UserBusyError as busy:
        await message.answer(render_busy(busy.job_id or None, lang_code))
        return None
    except Exception:  # noqa: BLE001 - gateway must not fake-accept unavailable jobs
        await message.answer(render_unavailable(lang_code))
        return None
    if job is None:
        # No persistence available: never invent a job ID.
        await message.answer(render_unavailable(lang_code))
        return None
    await message.answer(render_research_accepted(request.query, job.job_id, lang_code))
    if ctx.job_queue is not None:
        if not _is_progress_terminal(job.job_id):
            _remember_progress(job.job_id, lang_code)
            try:
                from research_agent.telegram.progress import ProgressStage, publish_progress

                await publish_progress(
                    message,
                    job.job_id,
                    ProgressStage.ANALYZING,
                    position=job.position,
                    lang=lang_code,
                )
                if _is_progress_terminal(job.job_id):
                    _clear_tracked_progress(job.job_id)
            except Exception:  # noqa: BLE001, S110 - progress failure must not fail enqueue
                _clear_tracked_progress(job.job_id)
    return job


def _status_stage(job_id: str) -> str:
    """Return a pipeline stage without exposing the persistence state.

    Reads the canonical ``get_progress_state`` store first (written by
    ``update_progress`` on confirmed edits), then the legacy adapter map.
    Unknown or evicted jobs report generic ``active`` rather than guessing.
    """
    try:
        from research_agent.telegram.progress import ProgressStage, get_progress_state

        state = get_progress_state(job_id)
        if state is not None:
            candidate = state.get("stage")
            if isinstance(candidate, str):
                try:
                    return ProgressStage(candidate).value
                except ValueError:
                    pass
        legacy = _PROGRESS_STAGES.get(job_id)
        if legacy is not None:
            try:
                return ProgressStage(legacy).value
            except ValueError:
                pass
        return "active"
    except Exception:  # noqa: BLE001 - status remains useful if progress is unavailable
        return _PROGRESS_STAGES.get(job_id, "active")


async def whoami_handler(message: Message) -> None:
    """Reply with the sender's numeric Telegram user ID."""
    if message.from_user is None:
        return
    await message.answer(render_whoami(message.from_user.id))


async def start_handler(
    message: Message,
    conn: aiosqlite.Connection | None = None,
    db_path: Path | str | None = None,
    limits: TelegramLimits | None = None,
) -> None:
    """Explain capabilities and limits; upsert the user on /start."""
    from_user = message.from_user
    ctx = build_ctx(message, conn=conn, db_path=db_path)
    lang_code = (
        await resolve_preferred_lang(ctx, from_user.id) if from_user is not None else ctx.lang_code
    )
    if from_user is not None:
        try:
            async with with_db(ctx.conn, ctx.db_path) as db_conn:
                if db_conn is not None:
                    await ensure_user(db_conn, from_user.id, lang_code)
        except Exception:  # noqa: BLE001, S110 - start reply must not fail on DB issues
            pass
    await message.answer(render_start(lang_code, limits))


async def help_handler(
    message: Message,
    conn: aiosqlite.Connection | None = None,
    db_path: Path | str | None = None,
    limits: TelegramLimits | None = None,
) -> None:
    """Show examples and limits without creating jobs."""
    ctx = build_ctx(message, conn=conn, db_path=db_path)
    lang_code: LangCode = (
        await resolve_preferred_lang(ctx, message.from_user.id)
        if message.from_user is not None
        else ctx.lang_code
    )
    await message.answer(render_help(lang_code, limits))


async def research_handler(
    message: Message,
    conn: aiosqlite.Connection | None = None,
    db_path: Path | str | None = None,
    job_queue: BoundedJobQueue | None = None,
) -> None:
    """Create a research job from /research <query> (usage reply when empty)."""
    text = message.text or ""
    query = extract_research_arg(text, "/research")
    await handle_research_request(message, query, conn=conn, db_path=db_path, job_queue=job_queue)


async def status_handler(
    message: Message,
    conn: aiosqlite.Connection | None = None,
    db_path: Path | str | None = None,
) -> None:
    """Show queue position and current stage for the user's active job."""
    from_user = message.from_user
    if from_user is None:
        return
    ctx = build_ctx(message, conn=conn, db_path=db_path)
    lang_code = await resolve_preferred_lang(ctx, from_user.id)
    if await reject_group_chat(message, lang_code):
        return

    async def _reply_with_conn(db_conn: aiosqlite.Connection) -> None:
        job = await get_user_active_job(db_conn, from_user.id)
        if job is None:
            await message.answer(format_status("none", lang_code=lang_code))
            return
        state = str(job["state"])
        job_id = str(job["job_id"])
        query_text = str(job["query"])
        if state == "queued":
            pos = await queue_position(db_conn, job_id)
            await message.answer(
                format_status(
                    "queued",
                    position=pos,
                    lang_code=lang_code,
                    job_id=job_id,
                    query=query_text,
                )
            )
        else:
            stage = _status_stage(job_id)
            await message.answer(
                format_status(
                    "active",
                    stage=stage,
                    lang_code=lang_code,
                    job_id=job_id,
                    query=query_text,
                )
            )

    async with with_db(ctx.conn, ctx.db_path) as db_conn:
        if db_conn is None:
            await message.answer(render_unavailable(lang_code))
            return
        await _reply_with_conn(db_conn)


async def cancel_handler(
    message: Message,
    conn: aiosqlite.Connection | None = None,
    db_path: Path | str | None = None,
) -> None:
    """Cooperatively cancel the user's active job."""
    from_user = message.from_user
    if from_user is None:
        return
    ctx = build_ctx(message, conn=conn, db_path=db_path)
    lang_code = await resolve_preferred_lang(ctx, from_user.id)
    if await reject_group_chat(message, lang_code):
        return

    async def _cancel_with_conn(db_conn: aiosqlite.Connection) -> None:
        try:
            # Atomic: cancel_user_job re-reads the live active job, so no
            # preliminary read can go stale across turnover.
            cancelled = await cancel_user_job(db_conn, from_user.id)
        except ValueError:
            # Completion or failure won the race; do not claim cancellation.
            cancelled = False
        except Exception:
            await message.answer(render_unavailable(lang_code))
            return
        if not cancelled:
            await message.answer(render_cancel_none(lang_code))
            return
        row = await get_user_active_job(db_conn, from_user.id)
        # Active is gone after cancel; confirm via cancelled state lookup.
        confirmed_id: str | None = None
        if row is None:
            # Look up the most recent cancelled job for this user is expensive;
            # acknowledge generically (truthful: something was cancelled).
            confirmed_id = None
        await message.answer(render_cancelled(confirmed_id, lang_code))

    async with with_db(ctx.conn, ctx.db_path) as db_conn:
        if db_conn is None:
            await message.answer(render_unavailable(lang_code))
            return
        await _cancel_with_conn(db_conn)


async def history_handler(
    message: Message,
    conn: aiosqlite.Connection | None = None,
    db_path: Path | str | None = None,
) -> None:
    """Show the user's last 5 reports (owner-scoped)."""
    from_user = message.from_user
    if from_user is None:
        return
    ctx = build_ctx(message, conn=conn, db_path=db_path)
    lang_code = await resolve_preferred_lang(ctx, from_user.id)
    if await reject_group_chat(message, lang_code):
        return

    async def _reply_with_conn(db_conn: aiosqlite.Connection) -> None:
        reports = await list_recent_reports(db_conn, from_user.id, limit=5)
        await message.answer(format_history(reports, lang_code))

    async with with_db(ctx.conn, ctx.db_path) as db_conn:
        if db_conn is None:
            await message.answer(render_unavailable(lang_code))
            return
        await _reply_with_conn(db_conn)


async def report_handler(
    message: Message,
    conn: aiosqlite.Connection | None = None,
    db_path: Path | str | None = None,
    reports_dir: Path | str | None = None,
) -> None:
    """Retrieve a prior report by ID with ownership check."""
    from_user = message.from_user
    if from_user is None:
        return
    ctx = build_ctx(message, conn=conn, db_path=db_path)
    lang_code = await resolve_preferred_lang(ctx, from_user.id)
    if await reject_group_chat(message, lang_code):
        return
    report_id = extract_research_arg(message.text or "", "/report")
    if not report_id:
        await message.answer(render_report_usage(lang_code))
        return

    async def _report_tools(
        db_conn: aiosqlite.Connection, report: aiosqlite.Row
    ) -> list[str] | None:
        """Use recorded executions as the authority for the tools line."""
        raw_tools = report["tools_used"]
        try:
            parsed = json.loads(str(raw_tools)) if raw_tools else []
        except (TypeError, ValueError):
            return None
        if not isinstance(parsed, list) or any(
            not isinstance(tool, str) or not tool.strip() for tool in parsed
        ):
            return None
        stored = [str(tool) for tool in parsed]
        job_id = str(report["job_id"])
        tool_runs = await ToolRunRepository().list_by_job(db_conn, job_id)
        if not tool_runs:
            # No recorded executions: fail closed (omit line) per PLAN 14/25
            # instead of trusting stored model claims (M5.22/M7.6).
            return None
        recorded: list[str] = []
        for run in tool_runs:
            name = str(run["tool_name"]).strip()
            if name and name not in recorded:
                recorded.append(name)
        return recorded if stored == recorded else None

    async def _reply_with_conn(db_conn: aiosqlite.Connection) -> None:
        report, sources = await get_report_bundle(db_conn, from_user.id, report_id)
        if report is None:
            await message.answer(render_report_not_found(lang_code))
            return
        try:
            topic = str(report["topic"])
            summary = str(report["summary"])
            tools = await _report_tools(db_conn, report)
            source_ids: list[int] = []
            for idx, src in enumerate(list(sources), start=1):
                sid = idx
                try:
                    sid = int(src["source_ref"])
                except Exception:  # noqa: BLE001, S110 - fall back to position
                    try:
                        sid = int(src["id"])
                    except Exception:  # noqa: BLE001, S110 - keep position
                        sid = idx
                if sid >= 1 and sid not in source_ids:
                    source_ids.append(sid)
            findings = [{"statement": summary, "citation_ids": source_ids or []}] if summary else []
            concise = render_concise_report(
                topic,
                findings,
                list(sources),
                lang_code,
                partial=False,
                disclaimer=None,
                tools_used=tools,
            )
            candidate: Path | None = None
            try:
                db_markdown = str(report["markdown_path"])
            except Exception:  # noqa: BLE001, S110 - fall back to reports_dir
                db_markdown = ""
            if db_markdown:
                candidate = Path(db_markdown)
            elif reports_dir is not None:
                try:
                    candidate = Path(reports_dir) / report_filename(report_id)
                except ValueError:  # noqa: BLE001, S110 - invalid id stays text-only
                    candidate = None
            if candidate is not None:
                # deliver_report sends the document when present and falls
                # back to concise MarkdownV2 text when missing/unsafe.
                await deliver_report(message, candidate, concise, reports_dir)
                return
            for chunk in split_message(concise):
                await message.answer(chunk, parse_mode="MarkdownV2")
        except Exception:  # noqa: BLE001 - validation/render failures stay safely escaped
            from research_agent.telegram.renderer import escape_markdown_v2

            failure = escape_markdown_v2(render_report_failure(lang_code))
            for chunk in split_message(failure):
                await message.answer(chunk, parse_mode="MarkdownV2")

    async with with_db(ctx.conn, ctx.db_path) as db_conn:
        if db_conn is None:
            await message.answer(render_unavailable(lang_code))
            return
        await _reply_with_conn(db_conn)


async def forget_handler(
    message: Message,
    conn: aiosqlite.Connection | None = None,
    db_path: Path | str | None = None,
) -> None:
    """Clear the temporary session; keep jobs and reports."""
    from_user = message.from_user
    if from_user is None:
        return
    ctx = build_ctx(message, conn=conn, db_path=db_path)
    lang_code = await resolve_preferred_lang(ctx, from_user.id)
    if await reject_group_chat(message, lang_code):
        return
    async with with_db(ctx.conn, ctx.db_path) as db_conn:
        if db_conn is None:
            await message.answer(render_unavailable(lang_code))
            return
        from research_agent.persistence.repositories import SessionRepository

        await SessionRepository().delete(db_conn, from_user.id)
        # open_db commits on exit; commit explicitly for injected conns.
        try:
            await db_conn.commit()
        except Exception:  # noqa: BLE001, S110 - forget reply must not fail
            pass
        await message.answer(render_forget_done(lang_code))


async def language_handler(
    message: Message,
    conn: aiosqlite.Connection | None = None,
    db_path: Path | str | None = None,
) -> None:
    """Show or set the English/Arabic language preference."""
    from_user = message.from_user
    if from_user is None:
        return
    ctx = build_ctx(message, conn=conn, db_path=db_path)
    lang_code = await resolve_preferred_lang(ctx, from_user.id)
    if await reject_group_chat(message, lang_code):
        return
    raw_arg = extract_research_arg(message.text or "", "/language")

    async def _current_with_conn(db_conn: aiosqlite.Connection) -> str:
        try:
            return await get_language(db_conn, from_user.id)
        except Exception:  # noqa: BLE001 - fall back to Telegram language
            return lang_code

    async def _set_with_conn(db_conn: aiosqlite.Connection, arg: str) -> str | None:
        try:
            return await set_language(db_conn, from_user.id, arg)
        except ValueError:
            return None

    if not raw_arg:
        async with with_db(ctx.conn, ctx.db_path) as db_conn:
            if db_conn is None:
                await message.answer(render_unavailable(lang_code))
                return
            current = await _current_with_conn(db_conn)
            await message.answer(render_language_current(current, current))
            return

    normalized = normalize_language(raw_arg)
    if normalized is None:
        await message.answer(render_language_invalid(lang_code))
        return
    async with with_db(ctx.conn, ctx.db_path) as db_conn:
        if db_conn is None:
            await message.answer(render_unavailable(lang_code))
            return
        new_code = await _set_with_conn(db_conn, raw_arg)
        if new_code is None:
            await message.answer(render_language_invalid(lang_code))
        else:
            await message.answer(render_language_set(new_code, new_code))


async def plaintext_handler(
    message: Message,
    conn: aiosqlite.Connection | None = None,
    db_path: Path | str | None = None,
    job_queue: BoundedJobQueue | None = None,
) -> None:
    """Route plain text through the same path as /research."""
    raw = message.text or ""
    # Captions on media arrive with text=None; surface them instead of non_text.
    caption = getattr(message, "caption", None)
    query = (raw or (str(caption) if caption else "")).strip()
    if not query:
        ctx = build_ctx(message, conn=conn, db_path=db_path)
        lang_code = (
            await resolve_preferred_lang(ctx, message.from_user.id)
            if message.from_user is not None
            else ctx.lang_code
        )
        await message.answer(render_non_text(lang_code))
        return
    await handle_research_request(message, query, conn=conn, db_path=db_path, job_queue=job_queue)


async def non_text_handler(
    message: Message,
    conn: aiosqlite.Connection | None = None,
    db_path: Path | str | None = None,
) -> None:
    """Gently ignore non-text messages without creating jobs."""
    ctx = build_ctx(message, conn=conn, db_path=db_path)
    lang_code: LangCode = (
        await resolve_preferred_lang(ctx, message.from_user.id)
        if message.from_user is not None
        else ctx.lang_code
    )
    if await reject_group_chat(message, lang_code):
        return
    await message.answer(render_non_text(lang_code))


def register_handlers(target: Router) -> None:
    """Register the Telegram handlers on a dispatcher-owned router."""
    harmless_group_flags = {"allow_unauthorized": True}
    target.message.register(whoami_handler, Command("whoami"), flags=harmless_group_flags)
    target.message.register(start_handler, Command("start"), flags=harmless_group_flags)
    target.message.register(help_handler, Command("help"), flags=harmless_group_flags)
    target.message.register(research_handler, Command("research"))
    target.message.register(status_handler, Command("status"))
    target.message.register(cancel_handler, Command("cancel"))
    target.message.register(history_handler, Command("history"))
    target.message.register(report_handler, Command("report"))
    target.message.register(forget_handler, Command("forget"))
    target.message.register(language_handler, Command("language"))
    target.message.register(plaintext_handler, F.text, ~F.text.startswith("/"))
    target.message.register(non_text_handler, ~F.text)


register_handlers(router)
