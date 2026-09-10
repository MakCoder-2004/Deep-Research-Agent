"""Telegram command handlers."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from research_agent.services.sessions import ensure_user
from research_agent.telegram.texts import pick_lang, render_help, render_start, render_whoami

router = Router()


@router.message(Command("whoami"), flags={"allow_unauthorized": True})
async def whoami_handler(message: Message) -> None:
    """Reply with the sender's numeric Telegram user ID."""
    if message.from_user is None:
        return
    await message.answer(render_whoami(message.from_user.id))


@router.message(Command("start"), flags={"allow_unauthorized": True})
async def start_handler(
    message: Message,
    conn: aiosqlite.Connection | None = None,
    db_path: Path | str | None = None,
) -> None:
    """Explain capabilities and limits; upsert the user on /start."""
    from_user = message.from_user
    lang_code: str | None = from_user.language_code if from_user is not None else None
    lang = pick_lang(lang_code)
    if from_user is not None:
        try:
            if conn is not None:
                await ensure_user(conn, from_user.id, lang)
            elif db_path is not None:
                from research_agent.persistence.database import open_db

                async with open_db(db_path) as db_conn:
                    await ensure_user(db_conn, from_user.id, lang)
        except Exception:  # noqa: BLE001, S110 - start reply must not fail on DB issues
            pass
    await message.answer(render_start(lang_code))


@router.message(Command("help"), flags={"allow_unauthorized": True})
async def help_handler(message: Message) -> None:
    """Show examples and limits without creating jobs."""
    lang_code: str | None = (
        message.from_user.language_code if message.from_user is not None else None
    )
    await message.answer(render_help(lang_code))
