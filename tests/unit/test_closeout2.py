"""Second close-out batch: renderers, validators, sessions, progress edges."""

from __future__ import annotations

import io
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from research_agent.observability.logging import JsonFormatter, clear_context
from research_agent.observability.redaction import redact_mapping
from research_agent.services.sessions import get_language, normalize_language, set_language
from research_agent.telegram import progress as progress_module
from research_agent.telegram.progress import (
    ProgressStage,
    _resolve_lang,
    _resolve_stage,
    publish_progress,
)
from research_agent.telegram.renderer import (
    render_concise_report,
    render_safe_fallback,
    split_message,
)
from research_agent.telegram.texts import (
    TelegramLimits,
    format_status,
    pick_lang,
    render_busy,
    render_help,
    render_high_stakes_disclaimer,
    render_invalid_url,
    render_language_current,
    render_language_invalid,
    render_language_set,
    render_non_text,
    render_private_chat_only,
    render_report_failure,
    render_report_not_found,
    render_report_usage,
    render_start,
    render_unavailable,
    render_whoami,
)
from research_agent.telegram.validators import MAX_URL_CHARS, classify_input, is_accepted_url


def test_all_render_helpers_localize() -> None:
    for lang in ("en", "ar"):
        assert render_start(lang)
        assert render_help(lang)
        assert render_busy(None, lang)
        assert render_busy("abc", lang)
        assert render_invalid_url(lang)
        assert render_non_text(lang)
        assert render_unavailable(lang)
        assert render_private_chat_only(lang)
        assert render_report_usage(lang)
        assert render_report_not_found(lang)
        assert render_report_failure(lang)
        assert render_language_current("en", lang)
        assert render_language_invalid(lang)
        assert render_language_set("ar", lang)
    assert render_whoami(5) == "Your Telegram user ID is: 5"
    assert pick_lang(None) == "en"
    assert "queued" in format_status("queued", position=2, lang_code="en")
    assert (
        "none" in format_status("none", lang_code="en").lower()
        or "no active" in format_status("none", lang_code="en").lower()
    )


def test_high_stakes_disclaimers() -> None:
    assert render_high_stakes_disclaimer("medical", "en") is not None
    assert render_high_stakes_disclaimer("legal", "ar") is not None
    assert render_high_stakes_disclaimer("financial", "en") is not None
    assert render_high_stakes_disclaimer("general", "en") is None
    assert render_high_stakes_disclaimer("unknown-domain", "en") is None
    assert render_high_stakes_disclaimer(None, "en") is None


def test_limits_default_roundtrip() -> None:
    text = render_start("en", TelegramLimits()) + render_help("ar", TelegramLimits())
    assert "10 requests" in text or "10 طلبات" in text


def test_validators_edges() -> None:
    assert classify_input("   ") == "text"
    assert classify_input("HTTPS://example.com/a") == "url"
    assert not is_accepted_url("https://" + "a" * MAX_URL_CHARS)
    assert not is_accepted_url("not a url with spaces")
    assert not is_accepted_url("ftp://example.com/a")
    assert is_accepted_url("https://example.com/بحث-عربي")


def test_resolve_stage_synonyms_and_fallback() -> None:
    assert _resolve_stage("critic") is ProgressStage.CHECKING
    assert _resolve_stage("check") is ProgressStage.CHECKING
    assert _resolve_stage("CHECKING") is ProgressStage.CHECKING
    assert _resolve_stage("nope-unknown") is ProgressStage.ANALYZING
    assert _resolve_lang("mixed") == "en"
    assert _resolve_lang("ar") == "ar"


async def test_publish_without_sender_defaults_english() -> None:
    from research_agent.telegram.progress import cleanup_progress, get_progress_state

    message = MagicMock()
    message.from_user = None
    message.chat = SimpleNamespace(id=10)
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=31))
    await publish_progress(message, "job-nosender", lang="xx")
    assert get_progress_state("job-nosender")["lang"] == "en"
    cleanup_progress("job-nosender")


async def test_safe_edit_handles_non_numeric_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.methods import SendMessage

    from research_agent.telegram.progress import safe_edit

    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(progress_module.asyncio, "sleep", fake_sleep)
    bot = MagicMock()
    exc = TelegramRetryAfter(method=SendMessage(chat_id=1, text="x"), message="f", retry_after=0)
    object.__setattr__(exc, "retry_after", "garbage")
    bot.edit_message_text = AsyncMock(side_effect=[exc, True])
    assert await safe_edit(bot, 1, 2, "hello") is True
    assert sleeps == [1.0]


async def test_safe_edit_network_retry_failure_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiogram.exceptions import TelegramNetworkError

    from research_agent.telegram.progress import safe_edit

    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(progress_module.asyncio, "sleep", fake_sleep)
    bot = MagicMock()
    bot.edit_message_text = AsyncMock(side_effect=TelegramNetworkError(method=None, message="down"))  # type: ignore[arg-type]
    assert await safe_edit(bot, 1, 2, "hello") is False


def test_redact_tuple_set_and_undecodable_bytes() -> None:
    redacted = redact_mapping({"t": ("sk-1234567890abcdef", 1), "s": {"plain", "x"}}, [])
    assert "sk-1234567890abcdef" not in str(redacted)
    assert isinstance(redacted["t"], tuple)
    assert isinstance(redacted["s"], set)
    redacted_bad = redact_mapping({"b": b"\xff\xfe api_key=s3cr3t"}, [])
    assert isinstance(redacted_bad["b"], bytes)
    assert b"s3cr3t" not in redacted_bad["b"]


def test_split_multi_chunk_headers_rejoin() -> None:
    text = ("Paragraph one has content. " * 40 + "\n\n") * 6
    chunks = split_message(text, 400)
    assert len(chunks) > 1
    assert all(len(chunk) <= 420 for chunk in chunks)


def test_split_flattens_oversized_link_and_bold() -> None:
    text = "[click here](https://example.com/" + "a" * 300 + ") and *bold statement here* end"
    chunks = split_message(text, 120)
    joined = " ".join(chunks)
    assert "](" not in joined


def test_coerce_invalid_finding_and_empty_report_raise() -> None:
    with pytest.raises(Exception, match="[Ee]very finding|existing source|Invalid|source"):
        render_concise_report(
            "T",
            [{"statement": "S", "citation_ids": []}],
            [],
            "en",
        )
    with pytest.raises((ValidationError, ValueError)):
        render_concise_report("T", [], [], "en")


def test_safe_fallback_escapes_both_languages() -> None:
    assert render_safe_fallback("T", "S", "en")
    assert render_safe_fallback("T", "S", "ar")
    assert render_safe_fallback("T", "S", "xx")


def test_json_formatter_records_exception_type() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("test.exc")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            logger.exception("failed")
        output = stream.getvalue()
    finally:
        logger.handlers.clear()
        clear_context()
    assert "RuntimeError" in output


def test_normalize_language_variants() -> None:
    assert normalize_language("EN") == "en"
    assert normalize_language("AR") == "ar"
    assert normalize_language("english") == "en"
    assert normalize_language("arabic") == "ar"
    assert normalize_language("fr") is None


async def test_middleware_denial_survives_answer_failure() -> None:
    from research_agent.telegram.middlewares import AllowlistMiddleware

    middleware = AllowlistMiddleware({123})
    message = MagicMock()
    message.from_user = SimpleNamespace(id=999, language_code="en")
    message.answer = AsyncMock(side_effect=RuntimeError("send failed"))
    handler = AsyncMock()
    assert await middleware(handler, message, {}) is None
    handler.assert_not_awaited()


async def test_sessions_language_roundtrip_and_rejection(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from research_agent.persistence.database import open_db

    async with open_db(tmp_path / "sess.db") as conn:
        assert await get_language(conn, 4242) == "en"
        assert await set_language(conn, 4242, "arabic") == "ar"
        assert await get_language(conn, 4242) == "ar"
        with pytest.raises(ValueError):
            await set_language(conn, 4242, "xx")


async def test_retention_never_raises_on_broken_connection(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from research_agent.persistence.database import open_db
    from research_agent.services.retention import RetentionPolicy, run_retention

    db_path = tmp_path / "broken.db"
    async with open_db(db_path):
        pass
    broken = await open_db(db_path).__aenter__()
    await broken.close()
    outcome = await run_retention(broken, RetentionPolicy(), tmp_path)
    assert set(outcome) == {"sessions", "reports", "cache"}
    assert all(str(value).startswith("error:") for value in outcome.values())
