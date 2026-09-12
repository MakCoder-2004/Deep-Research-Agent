"""Temporary session helpers for the Telegram gateway."""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite

from research_agent.persistence.repositories import SessionRepository, UserRepository

_EN_ALIASES = frozenset(
    {
        "en",
        "eng",
        "english",
        "en-us",
        "en-gb",
        "en_us",
        "en_gb",
        "الإنجليزية",
        "الانجليزية",
        "انجليزي",
        "إنجليزي",
    }
)

_AR_ALIASES = frozenset(
    {
        "ar",
        "ara",
        "arabic",
        "ar-eg",
        "ar-sa",
        "ar_eg",
        "ar_sa",
        "العربية",
        "عربي",
        "عربية",
    }
)


def normalize_language(value: str | None) -> str | None:
    """Normalize a language argument to 'en' or 'ar'; None when invalid."""
    if value is None:
        return None
    cleaned = value.strip().lower().replace("_", "-")
    if not cleaned:
        return None
    if cleaned in _EN_ALIASES:
        return "en"
    if cleaned in _AR_ALIASES:
        return "ar"
    # Prefix fallback for regional variants (ar-DZ, en-AU, ...).
    if cleaned.startswith("ar-") or cleaned == "ar":
        return "ar"
    if cleaned.startswith("en-") or cleaned == "en":
        return "en"
    return None


async def ensure_user(conn: aiosqlite.Connection, user_id: int, language: str = "en") -> None:
    """Ensure a users row exists (upsert) for Telegram /start."""
    normalized = normalize_language(language) or "en"
    await UserRepository().upsert(conn, user_id, normalized)
    await conn.commit()


async def get_language(conn: aiosqlite.Connection, user_id: int) -> str:
    """Return the stored 'en'/'ar' preference (users then sessions, else 'en')."""
    users = UserRepository()
    sessions = SessionRepository()
    row = await users.get(conn, user_id)
    if row is not None:
        stored = str(row["language"]).strip().lower()
        normalized = normalize_language(stored)
        if normalized is not None:
            return normalized
        if stored in ("en", "ar"):
            return stored
    session_row = await sessions.get_valid(conn, user_id)
    if session_row is not None:
        stored = str(session_row["language"]).strip().lower()
        normalized = normalize_language(stored)
        if normalized is not None:
            return normalized
        if stored in ("en", "ar"):
            return stored
    return "en"


async def set_language(conn: aiosqlite.Connection, user_id: int, language: str) -> str:
    """Validate and persist 'en'/'ar' to users and sessions; return normalized."""
    normalized = normalize_language(language)
    if normalized is None:
        raise ValueError(f"Unsupported language: {language!r}. Use en or ar.")
    await UserRepository().upsert(conn, user_id, normalized)
    sessions = SessionRepository()
    existing = await sessions.get(conn, user_id)
    if existing is None:
        await sessions.save(conn, user_id, normalized, [])
    else:
        await conn.execute(
            "UPDATE sessions SET language = ?, updated_at = ? WHERE user_id = ?",
            (normalized, datetime.now(UTC).isoformat(), user_id),
        )
    await conn.commit()
    return normalized
