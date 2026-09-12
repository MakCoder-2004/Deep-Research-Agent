"""Unit tests for settings parsing and startup validation (M1.36)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from research_agent.config import Settings, validate_at_startup


def _make_settings(**overrides):  # type: ignore[no-untyped-def]
    base = {"_env_file": None, "ENVIRONMENT": "development"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_allowlist_parses_comma_separated_ids() -> None:
    settings = _make_settings(TELEGRAM_ALLOWED_USER_IDS="123, 456;789 101")
    assert settings.telegram_allowed_user_ids == {123, 456, 789, 101}


def test_allowlist_empty_by_default() -> None:
    settings = _make_settings()
    assert settings.telegram_allowed_user_ids == set()


def test_settings_loads_values_from_dotenv_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settings must pick up local .env values (requires python-dotenv)."""
    (tmp_path / ".env").write_text(
        "ENVIRONMENT=development\n"
        "TELEGRAM_BOT_TOKEN=file-loaded-value\n"
        "TELEGRAM_ALLOWED_USER_IDS=\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    settings = Settings()
    assert settings.telegram_bot_token.get_secret_value() == "file-loaded-value"
    assert settings.telegram_allowed_user_ids == set()


def test_allowlist_parses_comma_separated_ids_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raw env strings must reach the allowlist parser without JSON decoding."""
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123, 456;789")
    settings = Settings(_env_file=None, ENVIRONMENT="development")
    assert settings.telegram_allowed_user_ids == {123, 456, 789}


def test_allowlist_rejects_usernames() -> None:
    with pytest.raises(ValidationError):
        _make_settings(TELEGRAM_ALLOWED_USER_IDS="123,@someuser")


def test_allowlist_rejects_signed_zero_and_non_positive_ids() -> None:
    for bad in ("+123", "-5", "0", "12a", "1.5"):
        with pytest.raises(ValidationError):
            _make_settings(TELEGRAM_ALLOWED_USER_IDS=bad)


def test_plan_defaults() -> None:
    settings = _make_settings()
    assert settings.max_concurrent_jobs == 3
    assert settings.max_active_per_user == 1
    assert settings.requests_per_user_per_day == 10
    assert settings.deep_requests_per_user_per_day == 3
    assert settings.search_subqueries_per_job == 6
    assert settings.sources_per_job == 12
    assert settings.chars_per_source == 20000
    assert settings.reader_context_chars == 40000
    assert settings.repair_cycles == 1
    assert settings.job_timeout_seconds == 300
    assert settings.report_retention_days == 90
    assert settings.session_ttl_hours == 24
    assert settings.session_max_interactions == 6
    assert settings.search_cache_general_seconds == 6 * 3600
    assert settings.search_cache_news_seconds == 30 * 60
    assert settings.page_cache_general_seconds == 24 * 3600
    assert settings.page_cache_news_seconds == 3600


def test_search_concurrency_limits_parse_and_validate() -> None:
    settings = _make_settings(
        SEARCH_TOOL_CONCURRENCY=2,
        SEARCH_PROVIDER_CONCURRENCY=3,
        SEARCH_TOOL_LIMITS='{"Tavily": 2}',
        SEARCH_PROVIDER_LIMITS='{"shared": 4}',
    )
    assert settings.search_tool_concurrency == 2
    assert settings.search_provider_concurrency == 3
    assert settings.search_tool_limits == {"tavily": 2}
    assert settings.search_provider_limits == {"shared": 4}
    with pytest.raises(ValidationError):
        _make_settings(SEARCH_PROVIDER_LIMITS='{"shared": 0}')


def test_startup_validation_development_ok_without_token() -> None:
    settings = _make_settings()
    assert validate_at_startup(settings) is settings


def test_startup_validation_production_requires_token_and_allowlist() -> None:
    settings = _make_settings(ENVIRONMENT="production")
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        validate_at_startup(settings)
    settings = _make_settings(ENVIRONMENT="production", TELEGRAM_BOT_TOKEN="x" * 10)
    with pytest.raises(ValueError, match="TELEGRAM_ALLOWED_USER_IDS"):
        validate_at_startup(settings)


def test_startup_validation_does_not_leak_secret() -> None:
    secret = "super-secret-bot-token-value-123"  # noqa: S105
    settings = _make_settings(ENVIRONMENT="production", TELEGRAM_BOT_TOKEN=secret)
    try:
        validate_at_startup(settings)
    except ValueError as exc:
        assert secret not in str(exc)
    assert secret not in repr(settings)


def test_startup_validation_rejects_unknown_providers() -> None:
    settings = _make_settings(LLM_PROVIDER_PRIORITY="groq,nope")
    with pytest.raises(ValueError, match="Unknown LLM providers"):
        validate_at_startup(settings)


def test_startup_validation_production_requires_a_configured_provider_key() -> None:
    settings = _make_settings(
        ENVIRONMENT="production",
        TELEGRAM_BOT_TOKEN="x" * 10,
        TELEGRAM_ALLOWED_USER_IDS="123",
        LLM_PROVIDER_PRIORITY="groq",
    )
    with pytest.raises(ValueError, match="API key"):
        validate_at_startup(settings)
    settings = _make_settings(
        ENVIRONMENT="production",
        TELEGRAM_BOT_TOKEN="x" * 10,
        TELEGRAM_ALLOWED_USER_IDS="123",
        LLM_PROVIDER_PRIORITY="groq",
        GROQ_API_KEY="gsk-test-key",  # noqa: S105
    )
    assert validate_at_startup(settings) is settings
