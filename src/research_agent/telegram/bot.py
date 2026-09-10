"""aiogram bot and dispatcher factory (long polling only)."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from research_agent.config import Settings


def create_bot(settings: Settings) -> Bot:
    """Create an aiogram Bot without performing any network I/O."""
    token = settings.telegram_bot_token.get_secret_value()
    return Bot(token=token, default=DefaultBotProperties(parse_mode=None))


def create_dispatcher() -> Dispatcher:
    """Create a Dispatcher for long polling."""
    return Dispatcher()


async def start_polling(bot: Bot, dp: Dispatcher) -> None:
    """Start long polling for message updates only (no webhooks)."""
    await dp.start_polling(bot, allowed_updates=["message"])
