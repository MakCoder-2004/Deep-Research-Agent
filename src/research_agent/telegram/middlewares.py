"""Telegram access middleware (minimal stub; enforcement lands in M2.3)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject


def is_allowed(user_id: int | None, allowed: set[int]) -> bool:
    """Return True when user_id is present in the numeric allowlist."""
    return user_id is not None and user_id in allowed


class AllowlistMiddleware(BaseMiddleware):
    """Pass-through stub; M2.3 adds allowlist enforcement."""

    def __init__(self, allowed_user_ids: set[int]) -> None:
        self.allowed_user_ids = set(allowed_user_ids)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        return await handler(event, data)
