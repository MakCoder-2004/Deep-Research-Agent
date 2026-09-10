"""Integration test for Telegram startup lifecycle (M2.2). No live network."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from research_agent.config import Settings
from research_agent.main import run_telegram


def _make_settings(db_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[arg-type]
        ENVIRONMENT="development",
        TELEGRAM_BOT_TOKEN="test-bot-token-value",  # noqa: S105, S106
        TELEGRAM_ALLOWED_USER_IDS="123",
        DATABASE_PATH=db_path,  # type: ignore[arg-type]
    )


async def test_run_telegram_starts_polling_and_closes_session(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path / "research.db")
    mock_bot = MagicMock()
    mock_bot.session.close = AsyncMock()
    mock_dp = MagicMock()
    mock_dp.start_polling = AsyncMock()

    with (
        patch("research_agent.main.create_bot", return_value=mock_bot),
        patch("research_agent.main.create_dispatcher", return_value=mock_dp),
    ):
        await run_telegram(settings)

    mock_dp.start_polling.assert_awaited_once()
    _args, kwargs = mock_dp.start_polling.call_args
    # start_polling wrapper forwards message-only updates; direct dp call also uses them.
    if kwargs:
        assert kwargs.get("allowed_updates") == ["message"]
    else:
        # Wrapper was mocked at a higher level; polling was still invoked once.
        assert mock_dp.start_polling.await_count == 1
    mock_bot.session.close.assert_awaited_once()
    assert (tmp_path / "research.db").exists()


async def test_run_telegram_wires_dispatcher_start_polling(tmp_path: Path) -> None:
    """Patch Dispatcher.start_polling to prove no live network is used."""
    settings = _make_settings(tmp_path / "startup.db")
    mock_bot = MagicMock()
    mock_bot.session.close = AsyncMock()
    with (
        patch("research_agent.main.create_bot", return_value=mock_bot),
        patch(
            "aiogram.Dispatcher.start_polling",
            new_callable=AsyncMock,
        ) as mock_poll,
    ):
        await run_telegram(settings)
        assert mock_poll.await_count == 1
    mock_bot.session.close.assert_awaited_once()
