"""Telegram command handlers."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import aiosqlite
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message
from pydantic import ValidationError

from research_agent.models.requests import ResearchRequest
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
    format_history,
    format_report_bundle,
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
    render_report_not_found,
    render_report_usage,
    render_research_accepted,
    render_research_usage,
    render_start,
    render_whoami,
)
from research_agent.telegram.validators import classify_input, is_accepted_url

router = Router()


def extract_research_arg(text: str | None, command: str) -> str:
    """Extract the query argument following a /command prefix.

    Handles ``/research``, ``/research@botname``, extra whitespace, and
    preserves inner content (including Arabic RTL) verbatim.
    """
    if not text:
        return ""
    stripped = text.strip()
    if not stripped.startswith(command):
        return stripped
    rest = stripped[len(command) :]
    if rest.startswith("@"):
        space_idx = rest.find(" ")
        if space_idx == -1:
            return ""
        rest = rest[space_idx + 1 :]
    return rest.strip()


async def handle_research_request(
    message: Message,
    query: str,
    conn: aiosqlite.Connection | None = None,
    db_path: Path | str | None = None,
) -> JobRef | None:
    """Validate a research query and enqueue it; reply with usage on empty.

    Accepts English, Arabic, and mixed-language text plus http(s) URLs.
    Inner content (including Arabic RTL) is preserved verbatim; only
    surrounding whitespace is stripped.
    """
    from_user = message.from_user
    if from_user is None:
        return None
    lang_code: str | None = from_user.language_code
    clean = query.strip()
    if not clean:
        await message.answer(render_research_usage(lang_code))
        return None
    kind = classify_input(clean)
    if kind == "url" and not is_accepted_url(clean):
        await message.answer(render_invalid_url(lang_code))
        return None
    # URL-looking inputs that claim another scheme (javascript:/file:/ftp:)
    # but were classified as text should still be rejected when they contain
    # a scheme-like prefix.
    lowered = clean.lower().lstrip()
    if kind == "text" and lowered.startswith(
        ("javascript:", "file:", "ftp:", "data:", "vbscript:")
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
        # Distinguish overlong/invalid URLs from generic usage errors.
        if kind == "url":
            await message.answer(render_invalid_url(lang_code))
        else:
            await message.answer(render_research_usage(lang_code))
        return None
    if conn is not None:
        try:
            job = await enqueue_request(conn, request.user_id, request.query)
        except UserBusyError as busy:
            await message.answer(render_busy(busy.job_id or None, lang_code))
            return None
        await message.answer(render_research_accepted(request.query, job.job_id, lang_code))
        try:
            from research_agent.telegram.progress import ProgressStage, publish_progress

            await publish_progress(
                message, job.job_id, ProgressStage.ANALYZING, position=job.position
            )
        except Exception:  # noqa: BLE001, S110 - progress failure must not fail enqueue
            pass
        return job
    if db_path is not None:
        from research_agent.persistence.database import open_db

        try:
            async with open_db(db_path) as db_conn:
                job = await enqueue_request(db_conn, request.user_id, request.query)
        except UserBusyError as busy:
            await message.answer(render_busy(busy.job_id or None, lang_code))
            return None
        await message.answer(render_research_accepted(request.query, job.job_id, lang_code))
        try:
            from research_agent.telegram.progress import ProgressStage, publish_progress

            await publish_progress(
                message, job.job_id, ProgressStage.ANALYZING, position=job.position
            )
        except Exception:  # noqa: BLE001, S110 - progress failure must not fail enqueue
            pass
        return job
    # No DB available (e.g. unit test without persistence): validate and
    # acknowledge without persistence so the reply path stays testable.
    fake_id = str(uuid4())
    await message.answer(render_research_accepted(request.query, fake_id, lang_code))
    return JobRef(job_id=fake_id, user_id=request.user_id, query=request.query)


@router.message(Command("whoami"), flags={"allow_unauthorized": True})
async def whoami_handler(message: Message) -> None:
    """Reply with the sender's numeric Telegram user ID."""
    if message.from_user is None:
        return
    await message.answer(render_whoami(message.from_user.id))


@router.message(Command("start"), flags={"allow_unauthorized": True})
async def start_handler(
    message: Message,
    conn: aiosqlite.Connection | None = None,
    db_path: Path | str | None = None,
) -> None:
    """Explain capabilities and limits; upsert the user on /start."""
    from_user = message.from_user
    lang_code: str | None = from_user.language_code if from_user is not None else None
    lang = pick_lang(lang_code)
    if from_user is not None:
        try:
            if conn is not None:
                await ensure_user(conn, from_user.id, lang)
            elif db_path is not None:
                from research_agent.persistence.database import open_db

                async with open_db(db_path) as db_conn:
                    await ensure_user(db_conn, from_user.id, lang)
        except Exception:  # noqa: BLE001, S110 - start reply must not fail on DB issues
            pass
    await message.answer(render_start(lang_code))


@router.message(Command("help"), flags={"allow_unauthorized": True})
async def help_handler(message: Message) -> None:
    """Show examples and limits without creating jobs."""
    lang_code: str | None = (
        message.from_user.language_code if message.from_user is not None else None
    )
    await message.answer(render_help(lang_code))


@router.message(Command("research"))
async def research_handler(
    message: Message,
    conn: aiosqlite.Connection | None = None,
    db_path: Path | str | None = None,
) -> None:
    """Create a research job from /research <query> (usage reply when empty)."""
    text = message.text or ""
    query = extract_research_arg(text, "/research")
    await handle_research_request(message, query, conn=conn, db_path=db_path)


@router.message(Command("status"))
async def status_handler(
    message: Message,
    conn: aiosqlite.Connection | None = None,
    db_path: Path | str | None = None,
) -> None:
    """Show queue position and current stage for the user's active job."""
    from_user = message.from_user
    if from_user is None:
        return
    lang_code: str | None = from_user.language_code

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
            await message.answer(
                format_status(
                    "active",
                    stage=state,
                    lang_code=lang_code,
                    job_id=job_id,
                    query=query_text,
                )
            )

    if conn is not None:
        await _reply_with_conn(conn)
        return
    if db_path is not None:
        from research_agent.persistence.database import open_db

        async with open_db(db_path) as db_conn:
            await _reply_with_conn(db_conn)
        return
    await message.answer(format_status("none", lang_code=lang_code))


@router.message(Command("cancel"))
async def cancel_handler(
    message: Message,
    conn: aiosqlite.Connection | None = None,
    db_path: Path | str | None = None,
) -> None:
    """Cooperatively cancel the user's active job."""
    from_user = message.from_user
    if from_user is None:
        return
    lang_code: str | None = from_user.language_code

    async def _cancel_with_conn(db_conn: aiosqlite.Connection) -> None:
        existing = await get_user_active_job(db_conn, from_user.id)
        job_id = str(existing["job_id"]) if existing is not None else ""
        cancelled = await cancel_user_job(db_conn, from_user.id)
        if cancelled and job_id:
            await message.answer(render_cancelled(job_id, lang_code))
        elif cancelled:
            await message.answer(render_cancelled("unknown", lang_code))
        else:
            await message.answer(render_cancel_none(lang_code))

    if conn is not None:
        await _cancel_with_conn(conn)
        return
    if db_path is not None:
        from research_agent.persistence.database import open_db

        async with open_db(db_path) as db_conn:
            await _cancel_with_conn(db_conn)
        return
    await message.answer(render_cancel_none(lang_code))


@router.message(Command("history"))
async def history_handler(
    message: Message,
    conn: aiosqlite.Connection | None = None,
    db_path: Path | str | None = None,
) -> None:
    """Show the user's last 5 reports (owner-scoped)."""
    from_user = message.from_user
    if from_user is None:
        return
    lang_code: str | None = from_user.language_code

    async def _reply_with_conn(db_conn: aiosqlite.Connection) -> None:
        reports = await list_recent_reports(db_conn, from_user.id, limit=5)
        await message.answer(format_history(reports, lang_code))

    if conn is not None:
        await _reply_with_conn(conn)
        return
    if db_path is not None:
        from research_agent.persistence.database import open_db

        async with open_db(db_path) as db_conn:
            await _reply_with_conn(db_conn)
        return
    await message.answer(format_history([], lang_code))


@router.message(Command("report"))
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
    lang_code: str | None = from_user.language_code
    report_id = extract_research_arg(message.text or "", "/report")
    if not report_id:
        await message.answer(render_report_usage(lang_code))
        return

    async def _reply_with_conn(db_conn: aiosqlite.Connection) -> None:
        report, sources = await get_report_bundle(db_conn, from_user.id, report_id)
        if report is None:
            await message.answer(render_report_not_found(lang_code))
            return
        try:
            import json as _json

            topic = str(report["topic"])
            summary = str(report["summary"])
            try:
                raw_tools = report["tools_used"]
                tools: list[str] = list(_json.loads(str(raw_tools))) if raw_tools else []
                tools = [str(tool) for tool in tools]
            except Exception:  # noqa: BLE001, S110 - tools line is optional
                tools = []
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
                pick_lang(lang_code),
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
        except Exception:  # noqa: BLE001 - fall back to plain bundle on render issues
            await message.answer(format_report_bundle(report, sources, lang_code))

    if conn is not None:
        await _reply_with_conn(conn)
        return
    if db_path is not None:
        from research_agent.persistence.database import open_db

        async with open_db(db_path) as db_conn:
            await _reply_with_conn(db_conn)
        return
    await message.answer(render_report_not_found(lang_code))


@router.message(Command("forget"))
async def forget_handler(
    message: Message,
    conn: aiosqlite.Connection | None = None,
    db_path: Path | str | None = None,
) -> None:
    """Clear the temporary session; keep jobs and reports."""
    from_user = message.from_user
    if from_user is None:
        return
    lang_code: str | None = from_user.language_code
    if conn is not None:
        from research_agent.persistence.repositories import SessionRepository

        await SessionRepository().delete(conn, from_user.id)
        await conn.commit()
        await message.answer(render_forget_done(lang_code))
        return
    if db_path is not None:
        from research_agent.persistence.database import open_db
        from research_agent.persistence.repositories import SessionRepository

        async with open_db(db_path) as db_conn:
            await SessionRepository().delete(db_conn, from_user.id)
        await message.answer(render_forget_done(lang_code))
        return
    await message.answer(render_forget_done(lang_code))


@router.message(Command("language"))
async def language_handler(
    message: Message,
    conn: aiosqlite.Connection | None = None,
    db_path: Path | str | None = None,
) -> None:
    """Show or set the English/Arabic language preference."""
    from_user = message.from_user
    if from_user is None:
        return
    lang_code: str | None = from_user.language_code
    raw_arg = extract_research_arg(message.text or "", "/language")

    async def _current_with_conn(db_conn: aiosqlite.Connection) -> str:
        try:
            return await get_language(db_conn, from_user.id)
        except Exception:  # noqa: BLE001 - fall back to Telegram language
            return pick_lang(lang_code)

    async def _set_with_conn(db_conn: aiosqlite.Connection, arg: str) -> str | None:
        try:
            return await set_language(db_conn, from_user.id, arg)
        except ValueError:
            return None

    if not raw_arg:
        if conn is not None:
            current = await _current_with_conn(conn)
            await message.answer(render_language_current(current, current))
            return
        if db_path is not None:
            from research_agent.persistence.database import open_db

            async with open_db(db_path) as db_conn:
                current = await _current_with_conn(db_conn)
            await message.answer(render_language_current(current, current))
            return
        await message.answer(render_language_current(pick_lang(lang_code), lang_code))
        return

    normalized = normalize_language(raw_arg)
    if normalized is None:
        await message.answer(render_language_invalid(lang_code))
        return
    if conn is not None:
        new_code = await _set_with_conn(conn, raw_arg)
        if new_code is None:
            await message.answer(render_language_invalid(lang_code))
        else:
            await message.answer(render_language_set(new_code, new_code))
        return
    if db_path is not None:
        from research_agent.persistence.database import open_db

        async with open_db(db_path) as db_conn:
            new_code = await _set_with_conn(db_conn, raw_arg)
        if new_code is None:
            await message.answer(render_language_invalid(lang_code))
        else:
            await message.answer(render_language_set(new_code, new_code))
        return
    await message.answer(render_language_set(normalized, normalized))


@router.message(F.text, ~F.text.startswith("/"))
async def plaintext_handler(
    message: Message,
    conn: aiosqlite.Connection | None = None,
    db_path: Path | str | None = None,
) -> None:
    """Route plain text through the same path as /research."""
    query = (message.text or "").strip()
    await handle_research_request(message, query, conn=conn, db_path=db_path)


@router.message(~F.text)
async def non_text_handler(message: Message) -> None:
    """Gently ignore non-text messages without creating jobs."""
    lang_code: str | None = (
        message.from_user.language_code if message.from_user is not None else None
    )
    await message.answer(render_non_text(lang_code))
