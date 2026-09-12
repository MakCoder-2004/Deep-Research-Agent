"""Application entrypoint for the Deep Research Agent (Telegram long polling)."""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Dispatcher

from research_agent.config import Settings, validate_at_startup
from research_agent.llm.health import check_startup_health
from research_agent.observability.logging import configure_logging
from research_agent.persistence.database import open_db
from research_agent.services.queue import BoundedJobQueue, set_default_queue
from research_agent.services.retention import RetentionPolicy
from research_agent.telegram.bot import (
    create_bot,
    create_dispatcher,
    start_polling,
    start_queue_worker,
    stop_queue_worker,
)
from research_agent.telegram.texts import TelegramLimits

logger = logging.getLogger(__name__)


def _collect_secrets(settings: Settings) -> list[str]:
    candidates = [
        settings.telegram_bot_token.get_secret_value(),
        settings.groq_api_key.get_secret_value(),
        settings.openrouter_api_key.get_secret_value(),
        settings.cloudflare_api_key.get_secret_value(),
        settings.cloudflare_account_id.get_secret_value(),
        settings.tavily_api_key.get_secret_value(),
        settings.brave_api_key.get_secret_value(),
        settings.exa_api_key.get_secret_value(),
        settings.serpapi_api_key.get_secret_value(),
        settings.github_token.get_secret_value(),
        settings.stackexchange_api_key.get_secret_value(),
        settings.semantic_scholar_api_key.get_secret_value(),
        settings.ncbi_api_key.get_secret_value(),
        settings.jina_reader_api_key.get_secret_value(),
        settings.langsmith_api_key.get_secret_value(),
    ]
    return [secret for secret in candidates if secret]


def _queue_from_settings(settings: Settings) -> BoundedJobQueue:
    """Build a queue from typed settings (keeps ``Any`` inside queue.py).

    ``BoundedJobQueue.from_settings`` accepts ``Any`` for historic reasons;
    this wrapper restores proper :class:`Settings` typing on the caller side
    without touching the queue implementation.
    """
    return BoundedJobQueue.from_settings(settings, db_path=settings.database_path)


async def run_telegram(settings: Settings) -> None:
    """Validate config, init storage, and run Telegram long polling."""
    settings = validate_at_startup(settings)
    configure_logging(secrets=_collect_secrets(settings))
    try:
        health = await check_startup_health(settings)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - provider health is degraded-safe
        logger.warning(
            "llm startup health check failed; continuing in degraded mode: error_type=%s",
            type(exc).__name__,
        )
    else:
        available_providers = sum(status.available for status in health.providers.values())
        available_capabilities = sum(status.available for status in health.capabilities.values())
        logger.info(
            "llm startup health: providers=%d/%d capabilities=%d/%d",
            available_providers,
            len(health.providers),
            available_capabilities,
            len(health.capabilities),
        )
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    # open_db initializes the schema exactly once for the startup connection.
    async with open_db(settings.database_path):
        pass
    bot = None
    queue = None
    dp: Dispatcher | None = None
    try:
        bot = create_bot(settings)
        # Semaphore is sized from settings.max_concurrent_jobs (default 3 globally).
        queue = _queue_from_settings(settings)
        queue.set_bot(bot)
        set_default_queue(queue)
        dp = create_dispatcher(
            settings.telegram_allowed_user_ids,
            settings.database_path,
            job_queue=queue,
            reports_dir=settings.reports_dir,
            bot=bot,
            limits=TelegramLimits.from_settings(settings),
            retention=RetentionPolicy.from_settings(settings),
        )
        await start_queue_worker(dp, queue, bot)
        await start_polling(bot, dp)
    finally:
        try:
            if dp is not None and queue is not None:
                await stop_queue_worker(dp, queue)
            elif queue is not None:
                await queue.stop()
        finally:
            set_default_queue(None)
            if bot is not None:
                try:
                    await bot.session.close()
                except Exception:  # noqa: S110, BLE001 - shutdown must not raise
                    logger.debug("bot session close failed during shutdown")


def main() -> None:
    """Validate configuration and start the Telegram bot."""
    try:
        settings = validate_at_startup(Settings())
    except Exception as exc:  # noqa: BLE001 - surface startup errors clearly
        print("Configuration error: startup validation failed.", file=sys.stderr)
        raise SystemExit(1) from exc
    asyncio.run(run_telegram(settings))


if __name__ == "__main__":
    main()
