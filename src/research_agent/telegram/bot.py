"""aiogram bot and dispatcher factory (long polling only)."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties

from research_agent.config import Settings
from research_agent.services.queue import BoundedJobQueue
from research_agent.telegram.handlers import register_handlers
from research_agent.telegram.middlewares import AllowlistMiddleware


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
) -> Dispatcher:
    """Create a Dispatcher with allowlist middleware and app router."""
    dp = Dispatcher()
    dp.message.outer_middleware(AllowlistMiddleware(allowed_user_ids or set()))
    if db_path is not None:
        dp.workflow_data["db_path"] = db_path
    if job_queue is not None:
        dp.workflow_data["job_queue"] = job_queue
        if bot is not None:
            job_queue.set_bot(bot)
    if reports_dir is not None:
        dp.workflow_data["reports_dir"] = reports_dir
    if bot is not None:
        dp.workflow_data["bot"] = bot
    app_router = Router()
    register_handlers(app_router)
    dp.include_router(app_router)
    return dp


async def start_polling(bot: Bot, dp: Dispatcher) -> None:
    """Start long polling for message updates only (no webhooks)."""
    await dp.start_polling(bot, allowed_updates=["message"])


async def start_queue_worker(
    dp: Dispatcher, queue: BoundedJobQueue, bot: Bot | None = None
) -> None:
    """Attach a BoundedJobQueue to the dispatcher and start its worker."""
    if bot is None:
        workflow_bot = dp.workflow_data.get("bot")
        if workflow_bot is not None:
            bot = cast(Bot, workflow_bot)
    if bot is not None:
        queue.set_bot(bot)
    dp.workflow_data["job_queue"] = queue
    await queue.start()


async def stop_queue_worker(dp: Dispatcher) -> None:
    """Stop the dispatcher-attached queue worker, if any."""
    queue = dp.workflow_data.get("job_queue")
    if isinstance(queue, BoundedJobQueue):
        await queue.stop()
