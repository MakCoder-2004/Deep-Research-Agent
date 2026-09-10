"""Unit tests for the Telegram bot factory (M2.1). No live network."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Dispatcher

from research_agent.config import Settings
from research_agent.telegram.bot import create_bot, create_dispatcher, start_polling


def _make_settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[arg-type]
        ENVIRONMENT="development",
        TELEGRAM_BOT_TOKEN="test-bot-token-value",  # noqa: S105, S106
        TELEGRAM_ALLOWED_USER_IDS="123",
    )


def test_create_bot_passes_token_without_network() -> None:
    settings = _make_settings()
    with patch("research_agent.telegram.bot.Bot") as mock_bot:
        create_bot(settings)
        assert mock_bot.call_count == 1
        _, kwargs = mock_bot.call_args
        token = kwargs.get("token", mock_bot.call_args[0][0] if mock_bot.call_args[0] else None)
        assert token == "test-bot-token-value"  # noqa: S105
        default = kwargs.get("default")
        assert default is not None
        assert default.parse_mode is None


def test_create_dispatcher_returns_dispatcher() -> None:
    dp = create_dispatcher()
    assert isinstance(dp, Dispatcher)


async def test_start_polling_uses_message_updates_only() -> None:
    bot = MagicMock()
    dp = MagicMock()
    dp.start_polling = AsyncMock()
    await start_polling(bot, dp)  # type: ignore[arg-type]
    dp.start_polling.assert_awaited_once()
    args, kwargs = dp.start_polling.call_args
    assert args[0] is bot
    assert kwargs.get("allowed_updates") == ["message"]
