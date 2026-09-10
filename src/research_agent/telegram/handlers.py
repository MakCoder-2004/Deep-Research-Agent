"""Telegram command handlers."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from research_agent.telegram.texts import render_whoami

router = Router()


@router.message(Command("whoami"), flags={"allow_unauthorized": True})
async def whoami_handler(message: Message) -> None:
    """Reply with the sender's numeric Telegram user ID."""
    if message.from_user is None:
        return
    await message.answer(render_whoami(message.from_user.id))
