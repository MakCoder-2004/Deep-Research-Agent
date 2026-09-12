"""Numeric allowlist enforcement for Telegram messages."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.dispatcher.flags import get_flag
from aiogram.types import TelegramObject

from research_agent.telegram.texts import pick_unauthorized

logger = logging.getLogger(__name__)


def is_allowed(user_id: int | None, allowed: set[int]) -> bool:
    """Return True when user_id is present in the numeric allowlist."""
    if user_id is None or isinstance(user_id, bool):
        return False
    if not isinstance(user_id, int):
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            return False
    return user_id > 0 and user_id in allowed


class AllowlistMiddleware(BaseMiddleware):
    """Block messages from users missing from the numeric allowlist.

    Handlers flagged with ``allow_unauthorized=True`` (e.g. ``/whoami``)
    bypass the check so users can discover their numeric ID. Group privacy is
    enforced by the command handlers, which can resolve stored language for a
    localized private-chat response.
    """

    def __init__(self, allowed_user_ids: set[int]) -> None:
        self.allowed_user_ids = set(allowed_user_ids)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from_user: Any = getattr(event, "from_user", None)
        if get_flag(data, "allow_unauthorized", default=False):
            return await handler(event, data)
        user_id: int | None = from_user.id if from_user is not None else None
        if not is_allowed(user_id, self.allowed_user_ids):
            telegram_lang = (
                getattr(from_user, "language_code", None) if from_user is not None else None
            )
            answer = getattr(event, "answer", None)
            if callable(answer):
                try:
                    await answer(pick_unauthorized(telegram_lang))
                except Exception:  # noqa: S110, BLE001 - denial must not break dispatch
                    logger.debug("unauthorized answer failed")
            return None
        return await handler(event, data)
