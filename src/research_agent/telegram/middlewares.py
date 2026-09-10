"""Numeric allowlist enforcement for Telegram private chats."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.dispatcher.flags import get_flag
from aiogram.types import TelegramObject

from research_agent.telegram.texts import pick_unauthorized


def is_allowed(user_id: int | None, allowed: set[int]) -> bool:
    """Return True when user_id is present in the numeric allowlist."""
    return user_id is not None and user_id in allowed


class AllowlistMiddleware(BaseMiddleware):
    """Block messages from users missing from the numeric allowlist.

    Handlers flagged with ``allow_unauthorized=True`` (e.g. ``/whoami``)
    bypass the check so users can discover their numeric ID. Only private
    chats are served; group/supergroup/channel messages are blocked for
    all non-flagged handlers.
    """

    def __init__(self, allowed_user_ids: set[int]) -> None:
        self.allowed_user_ids = set(allowed_user_ids)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if get_flag(data, "allow_unauthorized", default=False):
            return await handler(event, data)
        from_user: Any = getattr(event, "from_user", None)
        user_id: int | None = from_user.id if from_user is not None else None
        chat: Any = getattr(event, "chat", None)
        chat_type: Any = getattr(chat, "type", None) if chat is not None else None
        if isinstance(chat_type, str) and chat_type != "private":
            lang_code: str | None = (
                getattr(from_user, "language_code", None) if from_user is not None else None
            )
            answer: Any = getattr(event, "answer", None)
            if callable(answer):
                await answer(pick_unauthorized(lang_code))
            return None
        if not is_allowed(user_id, self.allowed_user_ids):
            lang_code = getattr(from_user, "language_code", None) if from_user is not None else None
            answer = getattr(event, "answer", None)
            if callable(answer):
                await answer(pick_unauthorized(lang_code))
            return None
        return await handler(event, data)
