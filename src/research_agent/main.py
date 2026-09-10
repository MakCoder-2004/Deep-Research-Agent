"""Application entrypoint for the Deep Research Agent (Telegram long polling)."""

from __future__ import annotations

import asyncio
import sys

from research_agent.config import Settings, validate_at_startup
from research_agent.observability.logging import configure_logging
from research_agent.persistence.database import open_db
from research_agent.telegram.bot import create_bot, create_dispatcher, start_polling


def _collect_secrets(settings: Settings) -> list[str]:
    candidates = [
        settings.telegram_bot_token.get_secret_value(),
        settings.groq_api_key.get_secret_value(),
        settings.openrouter_api_key.get_secret_value(),
        settings.cloudflare_api_key.get_secret_value(),
        settings.tavily_api_key.get_secret_value(),
        settings.brave_api_key.get_secret_value(),
        settings.langsmith_api_key.get_secret_value(),
    ]
    return [secret for secret in candidates if secret]


async def run_telegram(settings: Settings) -> None:
    """Validate config, init storage, and run Telegram long polling."""
    settings = validate_at_startup(settings)
    configure_logging(secrets=_collect_secrets(settings))
    async with open_db(settings.database_path):
        pass
    bot = create_bot(settings)
    dp = create_dispatcher(settings.telegram_allowed_user_ids)
    try:
        await start_polling(bot, dp)
    finally:
        await bot.session.close()


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
