"""Unit tests for Telegram allowlist enforcement and /whoami bypass (M2.26)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from research_agent.persistence.database import open_db
from research_agent.telegram.handlers import router, whoami_handler
from research_agent.telegram.middlewares import AllowlistMiddleware, is_allowed
from research_agent.telegram.texts import UNAUTHORIZED_AR, UNAUTHORIZED_EN


def test_is_allowed() -> None:
    assert is_allowed(123, {123, 456}) is True
    assert is_allowed(999, {123, 456}) is False
    assert is_allowed(None, {123}) is False
    assert is_allowed(123, set()) is False


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


async def test_allowed_user_passes() -> None:
    middleware = AllowlistMiddleware({123})
    message = _make_message(123)
    handler = AsyncMock(return_value="ok")
    result = await middleware(handler, message, {})
    assert result == "ok"
    handler.assert_awaited_once()
    message.answer.assert_not_awaited()


async def test_denied_user_blocked_with_english_reply() -> None:
    middleware = AllowlistMiddleware({123})
    message = _make_message(999, "en")
    handler = AsyncMock()
    result = await middleware(handler, message, {})
    assert result is None
    handler.assert_not_awaited()
    message.answer.assert_awaited_once()
    reply = message.answer.call_args[0][0]
    assert reply == UNAUTHORIZED_EN


async def test_denied_user_blocked_with_arabic_reply() -> None:
    middleware = AllowlistMiddleware({123})
    message = _make_message(999, "ar-EG")
    handler = AsyncMock()
    await middleware(handler, message, {})
    handler.assert_not_awaited()
    message.answer.assert_awaited_once()
    reply = message.answer.call_args[0][0]
    assert reply == UNAUTHORIZED_AR


async def test_from_user_none_blocked() -> None:
    middleware = AllowlistMiddleware({123})
    message = _make_message(None)
    handler = AsyncMock()
    result = await middleware(handler, message, {})
    assert result is None
    handler.assert_not_awaited()
    message.answer.assert_awaited_once()


async def test_whoami_bypasses_allowlist() -> None:
    middleware = AllowlistMiddleware({123})
    message = _make_message(999, "en")
    handler = AsyncMock(return_value="ok")
    data = {"handler": SimpleNamespace(flags={"allow_unauthorized": True})}
    result = await middleware(handler, message, data)
    assert result == "ok"
    handler.assert_awaited_once()
    message.answer.assert_not_awaited()


async def test_whoami_flag_registered_on_router() -> None:
    assert router.message.handlers, "expected /whoami handler to be registered"
    data = {"handler": router.message.handlers[0]}
    middleware = AllowlistMiddleware({123})
    message = _make_message(999, "en")
    handler = AsyncMock(return_value="ok")
    result = await middleware(handler, message, data)
    assert result == "ok"
    handler.assert_awaited_once()


async def test_whoami_handler_replies_with_id() -> None:
    message = _make_message(777, "en")
    await whoami_handler(message)  # type: ignore[arg-type]
    message.answer.assert_awaited_once_with("Your Telegram user ID is: 777")


async def test_unauthorized_blocked_from_research_handler() -> None:
    middleware = AllowlistMiddleware({123})
    message = _make_message(999, "en", "private")
    research_obj = _handler_by_name("research_handler")
    handler = AsyncMock()
    result = await middleware(handler, message, {"handler": research_obj})
    assert result is None
    handler.assert_not_awaited()
    message.answer.assert_awaited_once()


async def test_unauthorized_blocked_from_plaintext_handler() -> None:
    middleware = AllowlistMiddleware({123})
    message = _make_message(999, "en", "private")
    plaintext_obj = _handler_by_name("plaintext_handler")
    handler = AsyncMock()
    result = await middleware(handler, message, {"handler": plaintext_obj})
    assert result is None
    handler.assert_not_awaited()
    message.answer.assert_awaited_once()


async def test_authorized_passes_research_handler() -> None:
    middleware = AllowlistMiddleware({123})
    message = _make_message(123, "en", "private")
    research_obj = _handler_by_name("research_handler")
    handler = AsyncMock(return_value="ok")
    result = await middleware(handler, message, {"handler": research_obj})
    assert result == "ok"
    handler.assert_awaited_once()
    message.answer.assert_not_awaited()


async def test_group_blocked_even_when_allowed() -> None:
    middleware = AllowlistMiddleware({123})
    message = _make_message(123, "en", "group")
    research_obj = _handler_by_name("research_handler")
    handler = AsyncMock()
    result = await middleware(handler, message, {"handler": research_obj})
    assert result is None
    handler.assert_not_awaited()
    message.answer.assert_awaited_once()


async def test_group_supergroup_blocked() -> None:
    middleware = AllowlistMiddleware({123})
    message = _make_message(123, "en", "supergroup")
    handler = AsyncMock()
    result = await middleware(handler, message, {})
    assert result is None
    handler.assert_not_awaited()


async def test_whoami_bypasses_group_block() -> None:
    middleware = AllowlistMiddleware({123})
    message = _make_message(999, "en", "group")
    whoami_obj = _handler_by_name("whoami_handler")
    handler = AsyncMock(return_value="ok")
    result = await middleware(handler, message, {"handler": whoami_obj})
    assert result == "ok"
    handler.assert_awaited_once()


async def test_denial_reveals_no_configuration() -> None:
    middleware = AllowlistMiddleware({123})
    for lang, _expected in (("en", UNAUTHORIZED_EN), ("ar", UNAUTHORIZED_AR)):
        message = _make_message(999, lang, "private")
        handler = AsyncMock()
        await middleware(handler, message, {})
        reply = message.answer.call_args[0][0]
        assert "TELEGRAM_" not in reply
        assert "TOKEN" not in reply
        assert "ALLOWED" not in reply
        assert "123" not in reply
        assert not any(ch.isdigit() for ch in reply)


async def test_zero_jobs_for_denied_user(tmp_path: Path) -> None:
    db_path = tmp_path / "auth.db"
    async with open_db(db_path) as conn:
        middleware = AllowlistMiddleware({123})
        message = _make_message(999, "en", "private")
        research_obj = _handler_by_name("research_handler")
        handler = AsyncMock()
        result = await middleware(handler, message, {"handler": research_obj})
        assert result is None
        handler.assert_not_awaited()
        cursor = await conn.execute("SELECT COUNT(*) AS n FROM jobs")
        row = await cursor.fetchone()
        assert row is not None and int(row["n"]) == 0
