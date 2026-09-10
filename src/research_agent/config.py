"""Runtime configuration for the Deep Research Agent (Milestone 1 foundation).

All secrets are read from the environment at runtime and are never logged.
Telegram access uses numeric user IDs, not usernames.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

KNOWN_LLM_PROVIDERS = frozenset({"groq", "openrouter", "cloudflare"})


def _split_list(value: str) -> list[str]:
    """Split a comma/semicolon/whitespace-separated environment value."""
    normalized = value.replace(";", ",").replace(" ", ",")
    return [part.strip() for part in normalized.split(",") if part.strip()]


def _parse_allowed_user_ids(value: Any) -> set[int]:
    """Parse TELEGRAM_ALLOWED_USER_IDS into a set of numeric Telegram IDs."""
    if value is None or value == "":
        return set()
    if isinstance(value, set):
        items: list[Any] = list(value)
    elif isinstance(value, (list, tuple)):
        items = list(value)
    elif isinstance(value, int):
        items = [value]
    elif isinstance(value, str):
        items = _split_list(value)
    else:
        raise ValueError("TELEGRAM_ALLOWED_USER_IDS must be comma-separated numeric IDs.")
    parsed: set[int] = set()
    for item in items:
        text = str(item).strip()
        # Telegram user IDs are positive integers: reject usernames as well as
        # signed, zero, or otherwise non-numeric values.
        if not text.isdigit() or int(text) <= 0:
            raise ValueError(
                f"Invalid Telegram user ID {text!r}: must be a positive numeric ID, not a username."
            )
        parsed.add(int(text))
    return parsed


AllowedUserIds = Annotated[set[int], BeforeValidator(_parse_allowed_user_ids)]


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    # Runtime environment
    runtime_environment: Literal["development", "staging", "production"] = Field(
        default="development", alias="ENVIRONMENT"
    )

    # Telegram
    telegram_bot_token: SecretStr = Field(default=SecretStr(""), alias="TELEGRAM_BOT_TOKEN")
    telegram_allowed_user_ids: AllowedUserIds = Field(
        default_factory=set, alias="TELEGRAM_ALLOWED_USER_IDS"
    )

    # Storage
    database_path: Path = Field(default=Path("data/research.db"), alias="DATABASE_PATH")
    reports_dir: Path = Field(default=Path("data/reports"), alias="REPORTS_DIR")
    report_retention_days: int = Field(default=90, alias="REPORT_RETENTION_DAYS", ge=1)
    session_ttl_hours: int = Field(default=24, alias="SESSION_TTL_HOURS", ge=1)
    session_max_interactions: int = Field(default=6, alias="SESSION_MAX_INTERACTIONS", ge=1)

    # Budgets and limits (PLAN section 17 defaults)
    max_concurrent_jobs: int = Field(default=3, alias="MAX_CONCURRENT_JOBS", ge=1)
    max_active_per_user: int = Field(default=1, alias="MAX_ACTIVE_PER_USER", ge=1)
    requests_per_user_per_day: int = Field(default=10, alias="REQUESTS_PER_USER_PER_DAY", ge=1)
    deep_requests_per_user_per_day: int = Field(
        default=3, alias="DEEP_REQUESTS_PER_USER_PER_DAY", ge=0
    )
    search_subqueries_per_job: int = Field(default=6, alias="SEARCH_SUBQUERIES_PER_JOB", ge=1)
    sources_per_job: int = Field(default=12, alias="SOURCES_PER_JOB", ge=1)
    chars_per_source: int = Field(default=20000, alias="CHARS_PER_SOURCE", ge=1000)
    reader_context_chars: int = Field(default=40000, alias="READER_CONTEXT_CHARS", ge=4000)
    repair_cycles: int = Field(default=1, alias="REPAIR_CYCLES", ge=0, le=1)
    job_timeout_seconds: int = Field(default=300, alias="JOB_TIMEOUT_SECONDS", ge=30)
    search_cache_general_seconds: int = Field(
        default=6 * 3600, alias="SEARCH_CACHE_GENERAL_SECONDS", ge=60
    )
    search_cache_news_seconds: int = Field(
        default=30 * 60, alias="SEARCH_CACHE_NEWS_SECONDS", ge=60
    )
    page_cache_general_seconds: int = Field(
        default=24 * 3600, alias="PAGE_CACHE_GENERAL_SECONDS", ge=60
    )
    page_cache_news_seconds: int = Field(default=3600, alias="PAGE_CACHE_NEWS_SECONDS", ge=60)

    # Provider credentials (injected at runtime, never logged)
    groq_api_key: SecretStr = Field(default=SecretStr(""), alias="GROQ_API_KEY")
    openrouter_api_key: SecretStr = Field(default=SecretStr(""), alias="OPENROUTER_API_KEY")
    cloudflare_api_key: SecretStr = Field(default=SecretStr(""), alias="CLOUDFLARE_API_KEY")
    cloudflare_account_id: str = Field(default="", alias="CLOUDFLARE_ACCOUNT_ID")
    tavily_api_key: SecretStr = Field(default=SecretStr(""), alias="TAVILY_API_KEY")
    brave_api_key: SecretStr = Field(default=SecretStr(""), alias="BRAVE_API_KEY")

    # Capability -> model resolution stays in configuration, not source constants.
    llm_provider_priority: list[str] = Field(
        default_factory=lambda: ["groq", "openrouter", "cloudflare"],
        alias="LLM_PROVIDER_PRIORITY",
    )
    model_map: dict[str, str] = Field(default_factory=dict, alias="MODEL_MAP")

    # LangSmith observability (optional; local logs/SQLite are the fallback)
    langsmith_tracing: bool = Field(default=False, alias="LANGSMITH_TRACING")
    langsmith_api_key: SecretStr = Field(default=SecretStr(""), alias="LANGSMITH_API_KEY")
    langsmith_project: str = Field(default="deep-research-agent", alias="LANGSMITH_PROJECT")
    langsmith_workspace_id: str = Field(default="", alias="LANGSMITH_WORKSPACE_ID")
    langsmith_endpoint: str = Field(
        default="https://api.smith.langchain.com", alias="LANGSMITH_ENDPOINT"
    )
    langsmith_environment: str = Field(default="development", alias="LANGSMITH_ENVIRONMENT")
    langsmith_trace_content: bool = Field(default=True, alias="LANGSMITH_TRACE_CONTENT")
    langsmith_trace_sensitive_content: bool = Field(
        default=False, alias="LANGSMITH_TRACE_SENSITIVE_CONTENT"
    )
    langsmith_sample_rate: float = Field(default=1.0, alias="LANGSMITH_SAMPLE_RATE", ge=0.0, le=1.0)

    @field_validator("llm_provider_priority", mode="before")
    @classmethod
    def _split_priority(cls, value: Any) -> Any:
        if isinstance(value, str):
            return _split_list(value)
        return value

    @field_validator("model_map", mode="before")
    @classmethod
    def _parse_model_map(cls, value: Any) -> Any:
        if value is None or value == "":
            return {}
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            import json

            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("MODEL_MAP must be a JSON object.") from exc
            if not isinstance(parsed, dict):
                raise ValueError("MODEL_MAP must be a JSON object.")
            return parsed
        return value


def validate_at_startup(settings_or_cls: Any = Settings) -> Settings:
    """Instantiate (if a class is given) and validate required settings.

    Never logs secret values. Raises ValueError with a sanitized message.
    """
    settings = settings_or_cls() if isinstance(settings_or_cls, type) else settings_or_cls
    if not isinstance(settings, Settings):
        raise ValueError("validate_at_startup expects a Settings instance or class.")
    if not settings.llm_provider_priority:
        raise ValueError("LLM_PROVIDER_PRIORITY must list at least one provider.")
    unknown = [name for name in settings.llm_provider_priority if name not in KNOWN_LLM_PROVIDERS]
    if unknown:
        raise ValueError(f"Unknown LLM providers in LLM_PROVIDER_PRIORITY: {sorted(unknown)}.")
    if settings.runtime_environment == "production":
        token = settings.telegram_bot_token.get_secret_value()
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required in production.")
        if not settings.telegram_allowed_user_ids:
            raise ValueError("TELEGRAM_ALLOWED_USER_IDS is required in production.")
        provider_keys = {
            "groq": settings.groq_api_key.get_secret_value(),
            "openrouter": settings.openrouter_api_key.get_secret_value(),
            "cloudflare": settings.cloudflare_api_key.get_secret_value(),
        }
        if not any(provider_keys.get(name) for name in settings.llm_provider_priority):
            raise ValueError(
                "At least one API key for the configured LLM_PROVIDER_PRIORITY "
                "is required in production."
            )
    if settings.langsmith_tracing and not settings.langsmith_api_key.get_secret_value():
        raise ValueError("LANGSMITH_API_KEY is required when LANGSMITH_TRACING is true.")
    return settings
