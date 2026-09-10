"""aiogram bot and dispatcher factory (long polling only)."""

from __future__ import annotations

from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from research_agent.config import Settings
from research_agent.services.queue import BoundedJobQueue
from research_agent.telegram.handlers import router
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
) -> Dispatcher:
    """Create a Dispatcher with allowlist middleware and app router."""
    dp = Dispatcher()
    dp.message.outer_middleware(AllowlistMiddleware(allowed_user_ids or set()))
    if db_path is not None:
        dp.workflow_data["db_path"] = db_path
    if job_queue is not None:
        dp.workflow_data["job_queue"] = job_queue
    if reports_dir is not None:
        dp.workflow_data["reports_dir"] = reports_dir
    # Global router is a singleton; allow the factory to be called repeatedly
    # (e.g. across unit tests) by re-parenting it to the newest dispatcher.
    parent = router.parent_router
    if parent is not None and parent is not dp:
        try:
            parent.sub_routers.remove(router)
        except ValueError:
            pass
        router._parent_router = None
    if router.parent_router is None:
        dp.include_router(router)
    return dp


async def start_polling(bot: Bot, dp: Dispatcher) -> None:
    """Start long polling for message updates only (no webhooks)."""
    await dp.start_polling(bot, allowed_updates=["message"])


async def start_queue_worker(dp: Dispatcher, queue: BoundedJobQueue) -> None:
    """Attach a BoundedJobQueue to the dispatcher and start its worker."""
    queue.set_bot(dp.workflow_data.get("bot"))
    dp.workflow_data["job_queue"] = queue
    await queue.start()


async def stop_queue_worker(dp: Dispatcher) -> None:
    """Stop the dispatcher-attached queue worker, if any."""
    queue = dp.workflow_data.get("job_queue")
    if isinstance(queue, BoundedJobQueue):
        await queue.stop()
