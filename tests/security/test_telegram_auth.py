"""Security tests for Telegram allowlist and /whoami (M2.26)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from research_agent.persistence.database import open_db
from research_agent.telegram.handlers import router, whoami_handler
from research_agent.telegram.middlewares import AllowlistMiddleware
from research_agent.telegram.texts import UNAUTHORIZED_EN


def _make_message(
    user_id: int | None,
    lang_code: str | None = "en",
    chat_type: str | None = "private",
) -> MagicMock:
    message = MagicMock()
    if user_id is None:
        message.from_user = None
    else:
        message.from_user = SimpleNamespace(id=user_id, language_code=lang_code)
    message.chat = SimpleNamespace(type=chat_type)
    message.answer = AsyncMock()
    return message


def _handler_by_name(name: str):  # type: ignore[no-untyped-def]
    for handler_obj in router.message.handlers:
        if handler_obj.callback.__name__ == name:
            return handler_obj
    raise AssertionError(f"handler {name} not registered")


async def _job_count(conn) -> int:  # type: ignore[no-untyped-def]
    cursor = await conn.execute("SELECT COUNT(*) AS n FROM jobs")
    row = await cursor.fetchone()
    assert row is not None
    return int(row["n"])


async def test_unauthorized_research_blocked_zero_jobs(tmp_path: Path) -> None:
    db_path = tmp_path / "sec_auth.db"
    async with open_db(db_path) as conn:
        assert await _job_count(conn) == 0
        middleware = AllowlistMiddleware({123})
        message = _make_message(999, "en", "private")
        research_obj = _handler_by_name("research_handler")
        handler = AsyncMock()
        result = await middleware(handler, message, {"handler": research_obj})
        assert result is None
        handler.assert_not_awaited()
        message.answer.assert_awaited_once()
        assert await _job_count(conn) == 0


async def test_unauthorized_plaintext_blocked_zero_jobs(tmp_path: Path) -> None:
    db_path = tmp_path / "sec_plain.db"
    async with open_db(db_path) as conn:
        middleware = AllowlistMiddleware({123})
        message = _make_message(999, "en", "private")
        plaintext_obj = _handler_by_name("plaintext_handler")
        handler = AsyncMock()
        result = await middleware(handler, message, {"handler": plaintext_obj})
        assert result is None
        handler.assert_not_awaited()
        assert await _job_count(conn) == 0


async def test_authorized_private_passes() -> None:
    middleware = AllowlistMiddleware({123})
    message = _make_message(123, "en", "private")
    research_obj = _handler_by_name("research_handler")
    handler = AsyncMock(return_value="ok")
    result = await middleware(handler, message, {"handler": research_obj})
    assert result == "ok"
    handler.assert_awaited_once()


async def test_whoami_works_unauthorized() -> None:
    middleware = AllowlistMiddleware({123})
    message = _make_message(999, "en", "private")
    whoami_obj = _handler_by_name("whoami_handler")
    handler = AsyncMock(return_value="ok")
    result = await middleware(handler, message, {"handler": whoami_obj})
    assert result == "ok"
    handler.assert_awaited_once()
    direct = _make_message(999, "en", "private")
    await whoami_handler(direct)  # type: ignore[arg-type]
    direct.answer.assert_awaited_once_with("Your Telegram user ID is: 999")


async def test_whoami_does_not_leak_allowlist() -> None:
    direct = _make_message(999, "en", "private")
    await whoami_handler(direct)  # type: ignore[arg-type]
    reply = direct.answer.call_args[0][0]
    assert "999" in reply
    assert "123" not in reply
    assert "TELEGRAM_" not in reply
    assert "TOKEN" not in reply


async def test_denial_reveals_no_secrets_or_ids() -> None:
    middleware = AllowlistMiddleware({111222333})
    message = _make_message(999888777, "en", "private")
    handler = AsyncMock()
    await middleware(handler, message, {})
    reply = message.answer.call_args[0][0]
    assert reply == UNAUTHORIZED_EN
    assert "111222333" not in reply
    assert "999888777" not in reply
    assert "TELEGRAM_" not in reply
    assert "TOKEN" not in reply
    assert "ALLOWED" not in reply


async def test_from_user_none_blocked() -> None:
    middleware = AllowlistMiddleware({123})
    message = _make_message(None, "en", "private")
    handler = AsyncMock()
    result = await middleware(handler, message, {})
    assert result is None
    handler.assert_not_awaited()


async def test_allowed_group_user_passes_allowlist(tmp_path: Path) -> None:
    db_path = tmp_path / "sec_group.db"
    async with open_db(db_path) as conn:
        middleware = AllowlistMiddleware({123})
        message = _make_message(123, "en", "group")
        research_obj = _handler_by_name("research_handler")
        handler = AsyncMock(return_value="ok")
        result = await middleware(handler, message, {"handler": research_obj})
        assert result == "ok"
        handler.assert_awaited_once()
        assert await _job_count(conn) == 0
