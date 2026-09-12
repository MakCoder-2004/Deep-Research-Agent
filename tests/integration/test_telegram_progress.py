"""Integration tests for single-message progress editing (M2.28)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage

from research_agent.telegram.progress import (
    ProgressStage,
    cleanup_progress,
    clear_progress,
    format_initial_text,
    format_stage_text,
    get_progress,
    get_progress_state,
    publish_progress,
    register_progress,
    safe_edit,
    update_progress,
)


def _bot() -> MagicMock:
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    return bot


def _message() -> MagicMock:
    message = MagicMock()
    message.from_user = SimpleNamespace(id=123, language_code="en")
    message.chat = SimpleNamespace(id=123, type="private")
    sent = SimpleNamespace(message_id=777)
    message.answer = AsyncMock(return_value=sent)
    return message


async def test_publish_then_six_edits_same_id() -> None:
    job_id = "job-progress-1"
    clear_progress(job_id)
    message = _message()
    bot = _bot()
    message_id = await publish_progress(message, job_id, ProgressStage.ANALYZING, position=1)
    assert message_id == 777
    assert get_progress(job_id) == (123, 777)
    message.answer.assert_awaited_once()
    for stage in list(ProgressStage):
        ok = await update_progress(bot, 123, 777, stage, "en", job_id=job_id)
        assert ok is True
    assert bot.edit_message_text.await_count == 6
    ids = {
        (call.kwargs.get("chat_id"), call.kwargs.get("message_id"))
        for call in bot.edit_message_text.call_args_list
    }
    # All six edits target the same single progress message.
    assert ids == {(123, 777)}
    clear_progress(job_id)
    assert get_progress(job_id) is None


async def test_stage_texts_en_and_ar() -> None:
    assert "Analyzing" in format_initial_text(ProgressStage.ANALYZING, "abcdef123456", 1, "en")
    assert "تحليل" in format_initial_text(ProgressStage.ANALYZING, "abcdef123456", 1, "ar")
    assert "Selecting" in format_stage_text(ProgressStage.SELECTING, "abcdef123456", "en")
    assert "اختيار" in format_stage_text(ProgressStage.SELECTING, "abcdef123456", "ar")
    assert register_progress("x", 1, 2) is None
    assert get_progress("x") == (1, 2)
    clear_progress("x")


async def test_progress_registry_is_bounded_and_terminal_cleanup_is_idempotent() -> None:
    job_ids = [f"bounded-{index}" for index in range(300)]
    for index, job_id in enumerate(job_ids):
        register_progress(job_id, index, index + 1)
    assert get_progress(job_ids[0]) is None
    assert get_progress(job_ids[-1]) == (299, 300)
    for job_id in job_ids:
        clear_progress(job_id)
        clear_progress(job_id)
    assert get_progress(job_ids[-1]) is None


async def test_update_tracks_localized_stage_and_cleanup_api() -> None:
    job_id = "job-progress-state"
    cleanup_progress(job_id)
    register_progress(job_id, 123, 777)
    bot = _bot()

    assert (
        await update_progress(
            bot,
            123,
            777,
            ProgressStage.CHECKING,
            "ar",
            job_id=job_id,
        )
        is True
    )
    state = get_progress_state(job_id)
    assert state is not None
    assert state["chat_id"] == 123
    assert state["message_id"] == 777
    assert state["stage"] == ProgressStage.CHECKING.value
    assert state["lang"] == "ar"
    assert "التحقق" in state["text"]

    assert cleanup_progress(job_id) is True
    assert get_progress(job_id) is None
    assert get_progress_state(job_id) is None
    assert cleanup_progress(job_id) is False


async def test_edit_failure_matrix_non_fatal() -> None:
    method = SendMessage(chat_id=123, text="hi")
    bot = _bot()
    bot.edit_message_text.side_effect = TelegramBadRequest(method=method, message="bad")
    assert await safe_edit(bot, 123, 1, "text") is False
    bot = _bot()
    bot.edit_message_text.side_effect = TelegramNetworkError(method=method, message="net")
    # Network failure backs off once then retries; mock fails twice -> False, no raise.
    bot.edit_message_text.side_effect = [
        TelegramNetworkError(method=method, message="net"),
        TelegramNetworkError(method=method, message="net"),
    ]
    assert await safe_edit(bot, 123, 1, "text") is False
    bot = _bot()
    bot.edit_message_text.side_effect = [
        TelegramRetryAfter(method=method, message="flood", retry_after=0),
        None,
    ]
    assert await safe_edit(bot, 123, 1, "text") is True
    bot = _bot()
    bot.edit_message_text.side_effect = RuntimeError("boom")
    assert await safe_edit(bot, 123, 1, "text") is False
    bot = _bot()
    bot.edit_message_text.side_effect = TelegramBadRequest(method=method, message="gone")
    assert await update_progress(bot, 123, 1, ProgressStage.SEARCHING, "en") is False


async def test_update_never_raises_on_generic_failure() -> None:
    bot = _bot()
    bot.edit_message_text.side_effect = Exception("unexpected")
    assert await update_progress(bot, 1, 2, "searching", "en", job_id="jid") is False
    assert await safe_edit(bot, 1, 2, "text") is False
