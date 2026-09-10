"""Telegram progress messaging: single editable status message (M2.19+)."""

from __future__ import annotations

import asyncio
import logging
from enum import StrEnum
from typing import Any

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import Message

from research_agent.observability.redaction import redact_text
from research_agent.telegram.texts import pick_lang

logger = logging.getLogger(__name__)


class ProgressStage(StrEnum):
    """Pipeline stages from PLAN section 15 (single edited message)."""

    ANALYZING = "analyzing"
    SELECTING = "selecting"
    SEARCHING = "searching"
    READING = "reading"
    CHECKING = "checking"
    PREPARING = "preparing"


STAGE_TEXT: dict[ProgressStage, dict[str, str]] = {
    ProgressStage.ANALYZING: {
        "en": "Analyzing your query...",
        "ar": "جارٍ تحليل استفسارك...",
    },
    ProgressStage.SELECTING: {
        "en": "Selecting research sources...",
        "ar": "جارٍ اختيار مصادر البحث...",
    },
    ProgressStage.SEARCHING: {
        "en": "Searching selected tools...",
        "ar": "جارٍ البحث في الأدوات المحددة...",
    },
    ProgressStage.READING: {
        "en": "Reading the strongest sources...",
        "ar": "جارٍ قراءة أقوى المصادر...",
    },
    ProgressStage.CHECKING: {
        "en": "Checking evidence and citations...",
        "ar": "جارٍ التحقق من الأدلة والاستشهادات...",
    },
    ProgressStage.PREPARING: {
        "en": "Preparing your report...",
        "ar": "جارٍ إعداد تقريرك...",
    },
}

# job_id -> (chat_id, message_id) for the single editable progress message.
_PROGRESS_REGISTRY: dict[str, tuple[int, int]] = {}


def register_progress(job_id: str, chat_id: int, message_id: int) -> None:
    """Store the chat/message IDs for a job's progress message."""
    _PROGRESS_REGISTRY[job_id] = (chat_id, message_id)


def get_progress(job_id: str) -> tuple[int, int] | None:
    """Return (chat_id, message_id) for a job, if registered."""
    return _PROGRESS_REGISTRY.get(job_id)


def clear_progress(job_id: str) -> None:
    """Remove a job's progress registry entry."""
    _PROGRESS_REGISTRY.pop(job_id, None)


def format_initial_text(
    stage: ProgressStage | str,
    job_id: str,
    position: int | None,
    lang: str,
) -> str:
    """Format the initial progress text with stage, short ID, and queue slot."""
    try:
        key = ProgressStage(stage)
    except ValueError:
        key = ProgressStage.ANALYZING
    stage_line = STAGE_TEXT[key].get(lang, STAGE_TEXT[key]["en"])
    short_id = job_id[:8]
    if position is not None and position > 0:
        if lang == "ar":
            return f"{stage_line}\nالمهمة {short_id} في قائمة الانتظار #{position}."
        return f"{stage_line}\nJob {short_id} queued #{position}."
    if lang == "ar":
        return f"{stage_line}\nالمهمة {short_id}."
    return f"{stage_line}\nJob {short_id}."


def format_stage_text(
    stage: ProgressStage | str,
    job_id: str,
    lang: str,
    position: int | None = None,
) -> str:
    """Format an edit payload for a pipeline stage (same single message)."""
    return format_initial_text(stage, job_id, position, lang)


async def safe_edit(bot: Bot | Any, chat_id: int, message_id: int, text: str) -> bool:
    """Edit a message without ever failing the job.

    - ``TelegramBadRequest`` (e.g. message not modified/deleted): ok-continue.
    - ``TelegramRetryAfter`` / ``TelegramNetworkError``: log sanitized,
      back off once, retry once, then continue the job.
    - Any other error: log sanitized and continue.
    Returns True when the edit succeeded, False otherwise; never raises.
    """
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
        return True
    except TelegramRetryAfter as exc:
        raw_wait = getattr(exc, "retry_after", 1)
        try:
            delay = float(raw_wait)
        except (TypeError, ValueError):
            delay = 1.0
        delay = min(max(delay, 0.0), 10.0)
        logger.warning(
            "progress edit rate-limited, backing off once: %s",
            redact_text(f"{type(exc).__name__}: {exc}"),
        )
        try:
            await asyncio.sleep(delay)
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
            return True
        except Exception as retry_exc:  # noqa: BLE001 - never fail job on edit
            logger.warning(
                "progress edit retry failed: %s",
                redact_text(f"{type(retry_exc).__name__}: {retry_exc}"),
            )
            return False
    except TelegramBadRequest as exc:
        # Message identical, too old, or deleted: safe to continue the job.
        logger.info(
            "progress edit skipped (bad request): %s",
            redact_text(f"{type(exc).__name__}: {exc}"),
        )
        return False
    except TelegramNetworkError as exc:
        logger.warning(
            "progress edit network error, backing off once: %s",
            redact_text(f"{type(exc).__name__}: {exc}"),
        )
        try:
            await asyncio.sleep(1.0)
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
            return True
        except Exception as retry_exc:  # noqa: BLE001 - never fail job on edit
            logger.warning(
                "progress edit retry failed: %s",
                redact_text(f"{type(retry_exc).__name__}: {retry_exc}"),
            )
            return False
    except Exception as exc:  # noqa: BLE001 - never fail job on edit
        logger.warning("progress edit failed: %s", redact_text(f"{type(exc).__name__}: {exc}"))
        return False


async def update_progress(
    bot: Bot | Any,
    chat_id: int,
    message_id: int,
    stage: ProgressStage | str,
    lang: str = "en",
    *,
    job_id: str | None = None,
    position: int | None = None,
) -> bool:
    """Edit the single progress message to a new pipeline stage.

    Always uses ``bot.edit_message_text`` via :func:`safe_edit`;
    never sends a new message and never raises on edit failure.
    Returns True when the edit succeeded.
    """
    try:
        key = ProgressStage(stage)
    except ValueError:
        key = ProgressStage.ANALYZING
    resolved = lang if lang in ("en", "ar") else "en"
    short = job_id[:8] if job_id else ""
    if short and position:
        text = format_stage_text(key, job_id or "", resolved, position)
    elif short:
        text = format_stage_text(key, job_id or "", resolved, None)
    else:
        stage_line = STAGE_TEXT[key].get(resolved, STAGE_TEXT[key]["en"])
        text = stage_line
    return await safe_edit(bot, chat_id, message_id, text)


async def publish_progress(
    message: Message,
    job_id: str,
    stage: ProgressStage | str = ProgressStage.ANALYZING,
    *,
    position: int | None = None,
    lang: str | None = None,
) -> int:
    """Answer the initial progress message and register it for later edits.

    Sends one localized stage line (default Analyzing) plus the job
    short-ID and queued #N slot, stores (chat_id, message_id) in the
    registry, and returns the message_id.
    """
    from_user = getattr(message, "from_user", None)
    user_lang = getattr(from_user, "language_code", None) if from_user is not None else None
    resolved = lang or pick_lang(user_lang)
    if resolved not in ("en", "ar"):
        resolved = "en"
    if isinstance(stage, str):
        try:
            stage_key: ProgressStage | str = ProgressStage(stage)
        except ValueError:
            stage_key = ProgressStage.ANALYZING
    else:
        stage_key = stage
    text = format_initial_text(stage_key, job_id, position, resolved)
    sent = await message.answer(text)
    message_id = int(getattr(sent, "message_id", 0) or 0)
    try:
        chat = getattr(message, "chat", None)
        chat_id = int(getattr(chat, "id", 0) or 0)
    except (TypeError, ValueError):
        chat_id = 0
    if chat_id and message_id:
        register_progress(job_id, chat_id, message_id)
    return message_id
