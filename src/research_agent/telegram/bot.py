"""aiogram bot and dispatcher factory (long polling only)."""

from __future__ import annotations

import asyncio
import inspect
import logging
from pathlib import Path
from typing import cast

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties

from research_agent.config import Settings
from research_agent.models import JobState
from research_agent.persistence.database import open_db
from research_agent.services.queue import BoundedJobQueue, set_state
from research_agent.services.retention import RetentionPolicy, run_retention
from research_agent.telegram.handlers import (
    clear_tracked_progress,
    register_handlers,
    wire_queue_worker,
)
from research_agent.telegram.middlewares import AllowlistMiddleware
from research_agent.telegram.texts import TelegramLimits

logger = logging.getLogger(__name__)

_RETENTION_SWEEP_SECONDS = 3600


def create_bot(settings: Settings) -> Bot:
    """Create an aiogram Bot without performing any network I/O."""
    token = settings.telegram_bot_token.get_secret_value()
    return Bot(token=token, default=DefaultBotProperties(parse_mode=None))


def create_dispatcher(
    allowed_user_ids: set[int] | None = None,
    db_path: Path | str | None = None,
    job_queue: BoundedJobQueue | None = None,
    reports_dir: Path | str | None = None,
    bot: Bot | None = None,
    limits: TelegramLimits | None = None,
    retention: RetentionPolicy | None = None,
) -> Dispatcher:
    """Create a Dispatcher with allowlist middleware and app router."""
    dp = Dispatcher()
    # Inner middleware (not outer): flags set by handler registration
    # (allow_unauthorized for /whoami|/start|/help) are only visible here.
    dp.message.middleware(AllowlistMiddleware(allowed_user_ids or set()))
    if db_path is not None:
        dp.workflow_data["db_path"] = db_path
    if job_queue is not None:
        dp.workflow_data["job_queue"] = job_queue
        if bot is not None:
            job_queue.set_bot(bot)
    if reports_dir is not None:
        dp.workflow_data["reports_dir"] = reports_dir
    if limits is not None:
        dp.workflow_data["limits"] = limits
    if retention is not None:
        dp.workflow_data["retention"] = retention
    if bot is not None:
        dp.workflow_data["bot"] = bot
    app_router = Router()
    register_handlers(app_router)
    dp.include_router(app_router)
    return dp


async def start_polling(bot: Bot, dp: Dispatcher) -> None:
    """Start long polling for message updates only (no webhooks)."""
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:  # noqa: S110, BLE001 - stale webhook must not block polling
        logger.debug("delete_webhook failed before polling")
    await dp.start_polling(bot, allowed_updates=["message"])


async def start_queue_worker(
    dp: Dispatcher, queue: BoundedJobQueue, bot: Bot | None = None
) -> None:
    """Recover persisted work, attach the queue, and start its worker."""
    if bot is None:
        workflow_bot = dp.workflow_data.get("bot")
        if workflow_bot is not None:
            bot = cast(Bot, workflow_bot)
    if bot is not None:
        queue.set_bot(bot)
    dp.workflow_data["job_queue"] = queue
    wire_queue_worker(queue)
    await _recover_queue(queue)
    await queue.start()
    await _start_retention_task(dp, queue)


async def _run_retention_once(
    db_path: Path | str, policy: RetentionPolicy, reports_dir: Path | str | None
) -> None:
    """Run one best-effort retention sweep (sessions/reports/cache)."""
    try:
        async with open_db(db_path) as conn:
            await run_retention(conn, policy, reports_dir)
    except Exception as exc:  # noqa: BLE001 - sweeps never kill the worker
        logger.debug("retention sweep failed: %s", type(exc).__name__)


async def _start_retention_task(dp: Dispatcher, queue: BoundedJobQueue) -> None:
    """Run a retention sweep now, then repeat hourly until worker stop."""
    policy = dp.workflow_data.get("retention")
    if not isinstance(policy, RetentionPolicy):
        policy = RetentionPolicy()
    reports_dir = dp.workflow_data.get("reports_dir")
    db_path = queue.db_path
    await _run_retention_once(db_path, policy, reports_dir)

    async def _loop() -> None:
        while True:
            await asyncio.sleep(_RETENTION_SWEEP_SECONDS)
            await _run_retention_once(db_path, policy, reports_dir)

    dp.workflow_data["retention_task"] = asyncio.create_task(_loop())


async def _reconcile_persisted_jobs(queue: BoundedJobQueue) -> None:
    """Terminally reconcile rows after restart (cancel-on-restart policy).

    Queued jobs are cancelled (FIFO position is not reconstructible) and
    stale active jobs fail. Matches
    :meth:`BoundedJobQueue.recover_pending_jobs`; kept as the fallback for
    queue objects without that method.
    """
    if hasattr(queue, "recover_pending_jobs"):
        await queue.recover_pending_jobs()
        return
    async with open_db(queue.db_path) as conn:
        cursor = await conn.execute(
            """
            SELECT job_id, state FROM jobs
            WHERE state IN ('queued', 'active')
            ORDER BY created_at ASC, rowid ASC
            """
        )
        for row in await cursor.fetchall():
            job_id = str(row["job_id"])
            state = JobState(str(row["state"]))
            if state == JobState.ACTIVE:
                await set_state(
                    conn,
                    job_id,
                    JobState.FAILED,
                    error="worker restarted before the job completed",
                )
            else:
                await set_state(conn, job_id, JobState.CANCELLED, error="job cancelled on restart")


async def _recover_queue(queue: BoundedJobQueue) -> None:
    """Use a queue recovery hook when available, else reconcile persisted rows."""
    for method_name in (
        "recover_pending_jobs",
        "recover_pending",
        "recover",
        "reconcile",
    ):
        hook = getattr(queue, method_name, None)
        if not callable(hook):
            continue
        result = hook()
        if inspect.isawaitable(result):
            await result
        return
    await _reconcile_persisted_jobs(queue)


async def stop_queue_worker(dp: Dispatcher, queue: BoundedJobQueue | None = None) -> None:
    """Stop the dispatcher-attached queue worker, if any."""
    task = dp.workflow_data.pop("retention_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: S110, BLE001 - shutdown must not raise
            logger.debug("retention task stop failed")
    attached = queue or dp.workflow_data.get("job_queue")
    if isinstance(attached, BoundedJobQueue):
        await attached.stop()
    clear_tracked_progress()
