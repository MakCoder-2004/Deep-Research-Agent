"""Temporary session helpers for the Telegram gateway."""

from __future__ import annotations

import aiosqlite

from research_agent.persistence.repositories import UserRepository


async def ensure_user(conn: aiosqlite.Connection, user_id: int, language: str = "en") -> None:
    """Ensure a users row exists (upsert) for Telegram /start."""
    normalized = language if language in ("en", "ar") else "en"
    await UserRepository().upsert(conn, user_id, normalized)
    await conn.commit()
