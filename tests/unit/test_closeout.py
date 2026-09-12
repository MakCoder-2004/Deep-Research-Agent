"""Close-out regression tests for the M1/M2 verification findings."""

from __future__ import annotations

import io
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage
from pydantic import ValidationError

from research_agent.config import Settings
from research_agent.errors import ErrorCategory
from research_agent.llm.base import ProviderCapability, ProviderMetadata
from research_agent.models.reports import (
    Finding,
    ResearchReport,
    Source,
    canonicalize_url,
)
from research_agent.models.requests import ResearchJob, SessionContext
from research_agent.models.research import SearchHit, SourceDocument
from research_agent.observability.logging import JsonFormatter, clear_context, set_correlation_id
from research_agent.observability.redaction import redact_mapping, redact_text
from research_agent.services.queue import BoundedJobQueue, JobRef
from research_agent.services.retention import RetentionPolicy
from research_agent.services.sessions import normalize_language
from research_agent.telegram.handlers import extract_research_arg, whoami_handler
from research_agent.telegram.middlewares import AllowlistMiddleware, is_allowed
from research_agent.telegram.progress import publish_progress, register_progress
from research_agent.telegram.renderer import render_concise_report, split_message
from research_agent.telegram.texts import TelegramLimits, format_history, render_quota_exceeded


def _aware() -> datetime:
    return datetime.now(UTC)


def _source(source_id: int = 1, url: str = "https://example.com/a") -> Source:
    return Source(id=source_id, title="T", url=url, accessed_at=_aware())  # type: ignore[arg-type]


def _report(**overrides: Any) -> ResearchReport:
    base: dict[str, Any] = {
        "topic": "T",
        "key_findings": [Finding(statement="S", citation_ids=[1])],
        "summary": "Sum",
        "sources": [_source()],
        "tools_used": ["tavily"],
    }
    base.update(overrides)
    return ResearchReport(**base)


# --- config edge cases -----------------------------------------------------


def test_allowlist_rejects_overflow_and_unicode_digits() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, TELEGRAM_ALLOWED_USER_IDS="99999999999999999999")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, TELEGRAM_ALLOWED_USER_IDS="١٢٣")


def test_queue_rejects_non_singleton_per_user_cap() -> None:
    settings = Settings(_env_file=None, MAX_ACTIVE_PER_USER=2)
    with pytest.raises(ValueError, match="max_active_per_user"):
        BoundedJobQueue.from_settings(settings, db_path=":memory:")


def test_retention_policy_from_settings() -> None:
    settings = Settings(_env_file=None)
    policy = RetentionPolicy.from_settings(settings)
    assert (policy.session_ttl_hours, policy.session_max_interactions) == (24, 6)
    assert policy.report_retention_days == 90


# --- model validation ------------------------------------------------------


def test_duplicate_citation_ids_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicates"):
        Finding(statement="S", citation_ids=[1, 1])


@pytest.mark.parametrize("key", ["cot", "system_prompt", "reasoning", "thought"])
def test_forbidden_report_field_variants_rejected(key: str) -> None:
    with pytest.raises(ValidationError, match="Forbidden report fields"):
        ResearchReport.model_validate(
            {
                "topic": "T",
                "key_findings": [{"statement": "S", "citation_ids": [1]}],
                "summary": "Sum",
                "sources": [
                    {
                        "id": 1,
                        "title": "T",
                        "url": "https://example.com/a",
                        "accessed_at": _aware().isoformat(),
                    }
                ],
                "tools_used": [],
                key: "sneaky",
            }
        )


def test_naive_datetimes_rejected_across_models() -> None:
    naive = datetime(2026, 1, 1, 12, 0, 0)
    with pytest.raises(ValidationError, match="timezone-aware"):
        Source(id=1, title="T", url="https://example.com/a", accessed_at=naive)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="timezone-aware"):
        SearchHit(
            url="https://example.com/a",
            title="T",
            tool_name="tavily",
            accessed_at=naive,
        )
    with pytest.raises(ValidationError, match="timezone-aware"):
        SourceDocument(source_id=1, url="https://example.com/a", published_at=naive)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="timezone-aware"):
        ResearchJob(user_id=1, query="q", created_at=naive)
    with pytest.raises(ValidationError, match="timezone-aware"):
        SessionContext(user_id=1, updated_at=naive)


def test_canonicalize_idna_ipv6_encoded_dots_and_trackers() -> None:
    puny = "مثال.مصر".encode("idna").decode("ascii")
    assert canonicalize_url("http://مثال.مصر/a?utm_source=x") == f"http://{puny}/a"
    assert canonicalize_url("http://[::1]/a") == canonicalize_url("http://[::1]:80/a")
    assert canonicalize_url("https://example.com/a/%2e%2e/b") == "https://example.com/b"
    assert (
        canonicalize_url("https://example.com/a?ref=home&spm=a.b.c&x=1")
        == "https://example.com/a?x=1"
    )
    assert (
        canonicalize_url("https://example.com/a?spm_xyz=1&gclsrc=aw&x=1")
        == "https://example.com/a?x=1"
    )
    with pytest.raises(ValueError):
        _report(
            sources=[_source(1, "https://example.com/a?ref=x"), _source(2, "https://example.com/a")]
        )


def test_supports_accepts_single_list_and_set() -> None:
    meta = ProviderMetadata(
        name="groq",
        model_id="m",
        capabilities=frozenset({ProviderCapability.FAST_MULTILINGUAL}),
    )
    assert meta.supports(ProviderCapability.FAST_MULTILINGUAL)
    assert meta.supports([ProviderCapability.FAST_MULTILINGUAL])
    assert meta.supports({ProviderCapability.FAST_MULTILINGUAL})
    assert not meta.supports(ProviderCapability.REASONING)


def test_error_category_reexported_from_packages() -> None:
    import research_agent.llm as llm_pkg
    import research_agent.tools as tools_pkg

    assert llm_pkg.ErrorCategory is ErrorCategory
    assert tools_pkg.ErrorCategory is ErrorCategory


# --- redaction -------------------------------------------------------------


def test_redact_url_userinfo_password() -> None:
    assert "s3cret" not in redact_text("fetch https://user:s3cret@example.com/a failed")
    assert "user:" in redact_text("fetch https://user:s3cret@example.com/a failed")


def test_redact_header_equals_and_json_forms() -> None:
    assert "xyz789" not in redact_text("authorization=xyz789 failed")
    assert "xyz789" not in redact_text('{"authorization": "Bearer xyz789"}')
    assert "s3cr3t" not in redact_text('{"x-api-key": "s3cr3t"}')
    assert "s3cr3t" not in redact_text("X-Api-Key: s3cr3t")


def test_redact_nested_collections_and_bytes_type() -> None:
    redacted = redact_mapping({"k": [["sk-1234567890abcdef"]]}, [])
    assert "sk-1234567890abcdef" not in str(redacted)
    assert redacted["k"] == [["***"]]
    redacted_bytes = redact_mapping({"k": b"api_key=s3cr3t"}, [])
    assert isinstance(redacted_bytes["k"], bytes)
    assert redacted_bytes["k"] == b"api_key=***"


def test_redact_arabic_national_id() -> None:
    assert "1234567890" not in redact_text("رقم الهوية: 1234567890")


# --- renderer --------------------------------------------------------------


def test_render_escapes_parens_in_source_urls() -> None:
    text = render_concise_report(
        "T",
        [{"statement": "S", "citation_ids": [1]}],
        [
            {
                "source_ref": 1,
                "title": "A",
                "url": "https://example.com/a_(b)",
                "publisher": None,
                "published_at": None,
                "accessed_at": _aware().isoformat(),
                "source_type": "web",
            }
        ],
        "en",
    )
    assert "\\(" in text


def test_split_never_breaks_escaped_markers_or_escapes() -> None:
    text = "word " * 800 + "\\[7\\] tail " + "more text " * 50
    chunks = split_message(text, 100)
    assert any("\\[7\\]" in chunk for chunk in chunks)
    for chunk in chunks:
        assert not chunk.endswith("\\")
        assert not chunk.endswith("\\[")


def test_arabic_report_has_lrm_and_sanitized_newlines() -> None:
    text = render_concise_report(
        "موضوع\nجديد",
        [{"statement": "نتيجة [1]", "citation_ids": [1]}],
        [
            {
                "source_ref": 1,
                "title": "عنوان\nفرعي",
                "url": "https://example.com/a",
                "publisher": None,
                "published_at": None,
                "accessed_at": _aware().isoformat(),
                "source_type": "web",
            }
        ],
        "ar",
    )
    assert "‎" in text
    assert "موضوع جديد" in text
    assert "عنوان فرعي" in text


def test_history_counts_rendered_lines_only() -> None:
    rendered = format_history(
        [
            {"report_id": "r1", "topic": "T1"},
            {"report_id": None, "topic": None},
            {"report_id": "r2", "topic": "T2"},
        ],
        "en",
    )
    assert "last 2" in rendered
    assert "- r1: T1" in rendered
    assert "- r2: T2" in rendered


def test_quota_message_is_localized_with_limit() -> None:
    assert "3" in render_quota_exceeded(3, "en")
    assert "3" in render_quota_exceeded(3, "ar")
    assert "quota" in render_quota_exceeded(3, "en").lower()


# --- routing / language ----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/research\tquery here", "query here"),
        ("/research\rquery here", "query here"),
        ("/research\nquery here", "query here"),
        ("/RESEARCH query here", "query here"),
        ("/research@mybot query here", "query here"),
        ("/research@mybot", ""),
        ("/researcher query", ""),
        ("/unknown query", ""),
        ("plain query", "plain query"),
    ],
)
def test_extract_research_arg_boundaries(text: str, expected: str) -> None:
    assert extract_research_arg(text, "/research") == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("en_US", "en"), ("", None), ("   ", None), ("العربية", "ar"), ("انجليزي", "en")],
)
def test_normalize_language_edge_cases(value: str, expected: str | None) -> None:
    assert normalize_language(value) == expected


# --- auth ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [("123", True), (123, True), (True, False), (None, False), (0, False), (-5, False)],
)
def test_is_allowed_coercion(candidate: Any, expected: bool) -> None:
    assert is_allowed(candidate, {123}) is expected


async def test_whoami_with_no_sender_stays_silent() -> None:
    message = MagicMock()
    message.from_user = None
    message.answer = AsyncMock()
    await whoami_handler(message)
    message.answer.assert_not_called()


def test_allowlist_middleware_is_inner_not_outer() -> None:
    from research_agent.telegram.bot import create_dispatcher

    dp = create_dispatcher({123})
    inner = dp.message.middleware._middlewares  # type: ignore[attr-defined]
    outer_manager = getattr(dp.message, "outer_middleware", None)
    outer = getattr(outer_manager, "_middlewares", [])
    assert any(isinstance(m, AllowlistMiddleware) for m in inner)
    assert not any(isinstance(m, AllowlistMiddleware) for m in outer)


# --- progress --------------------------------------------------------------


def test_register_progress_defaults_to_english_but_accepts_lang() -> None:
    from research_agent.telegram.progress import cleanup_progress, get_progress_state

    register_progress("job-en", 1, 2)
    assert get_progress_state("job-en")["lang"] == "en"
    register_progress("job-ar", 1, 2, "ar")
    assert get_progress_state("job-ar")["lang"] == "ar"
    cleanup_progress("job-en")
    cleanup_progress("job-ar")


async def test_publish_progress_records_resolved_language() -> None:
    from research_agent.telegram.progress import cleanup_progress, get_progress_state

    message = MagicMock()
    message.from_user = SimpleNamespace(id=1, language_code="ar")
    message.chat = SimpleNamespace(id=10)
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=31))
    await publish_progress(message, "job-pub", position=1, lang="ar")
    assert get_progress_state("job-pub")["lang"] == "ar"
    cleanup_progress("job-pub")


async def test_safe_edit_sleeps_full_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    from research_agent.telegram.progress import safe_edit

    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("research_agent.telegram.progress.asyncio.sleep", fake_sleep)
    bot = MagicMock()
    bot.edit_message_text = AsyncMock(
        side_effect=TelegramRetryAfter(
            method=SendMessage(chat_id=1, text="x"), message="flood", retry_after=30
        )
    )
    assert await safe_edit(bot, 1, 2, "hello") is False
    assert sleeps == [30.0]


# --- logging ---------------------------------------------------------------


def test_json_formatter_keeps_correlation_id() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("test.correlation")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    set_correlation_id("corr-9")
    try:
        logger.info("hello")
        output = stream.getvalue()
    finally:
        logger.handlers.clear()
        clear_context()
    assert "corr-9" in output


# --- queue -----------------------------------------------------------------


def test_put_nowait_validates_identifiers() -> None:
    queue = BoundedJobQueue(":memory:")
    queue.put_nowait(JobRef(job_id="req-1", user_id=1, query="q"))
    assert queue.pending == 1
    queue.put_nowait(JobRef(job_id="req-1", user_id=1, query="q"))
    assert queue.pending == 1
    with pytest.raises(ValueError):
        queue.put_nowait(JobRef(job_id="../evil", user_id=1, query="q"))
    with pytest.raises(ValueError):
        queue.put_nowait(JobRef(job_id="req-2", user_id=0, query="q"))
    with pytest.raises(ValueError):
        queue.put_nowait(JobRef(job_id="req-3", user_id=1, query="   "))


def test_telegram_limits_default_quota() -> None:
    assert TelegramLimits().requests_per_user_per_day == 10
