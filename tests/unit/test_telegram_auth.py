"""Unit tests for Telegram allowlist enforcement and /whoami bypass (M2.5)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from research_agent.telegram.handlers import router, whoami_handler
from research_agent.telegram.middlewares import AllowlistMiddleware, is_allowed
from research_agent.telegram.texts import UNAUTHORIZED_AR, UNAUTHORIZED_EN


def test_is_allowed() -> None:
    assert is_allowed(123, {123, 456}) is True
    assert is_allowed(999, {123, 456}) is False
    assert is_allowed(None, {123}) is False
    assert is_allowed(123, set()) is False


def _make_message(user_id: int | None, lang_code: str | None = "en") -> MagicMock:
    message = MagicMock()
    if user_id is None:
        message.from_user = None
    else:
        message.from_user = SimpleNamespace(id=user_id, language_code=lang_code)
    message.answer = AsyncMock()
    return message


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
