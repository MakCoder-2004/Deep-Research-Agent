"""Runtime configuration for the Deep Research Agent (Milestone 1 foundation).

All secrets are read from the environment at runtime and are never logged.
Telegram access uses numeric user IDs, not usernames.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

KNOWN_LLM_PROVIDERS = frozenset({"groq", "openrouter", "cloudflare"})

KNOWN_MODEL_CAPABILITIES = frozenset(
    {
        "fast_multilingual",
        "long_context",
        "reasoning",
        "structured_output",
        "arabic_capable",
        "tool_calling",
    }
)

_ID_RE = re.compile(r"[0-9]+")
_SPLIT_RE = re.compile(r"[,\s;]+")


def _split_list(value: str) -> list[str]:
    """Split a comma/semicolon/whitespace-separated environment value."""
    return [part.strip() for part in _SPLIT_RE.split(value) if part.strip()]


def _normalize_priority(items: list[str]) -> list[str]:
    """Lowercase, strip, and dedupe provider names preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in items:
        name = str(raw).strip().lower()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _parse_concurrency_limits(value: Any) -> dict[str, int]:
    """Parse optional per-tool/provider concurrency limits from settings."""
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        import json

        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("Concurrency limits must be a JSON object.") from exc
    else:
        parsed = value
    if not isinstance(parsed, dict):
        raise ValueError("Concurrency limits must be a JSON object.")
    limits: dict[str, int] = {}
    for raw_name, raw_limit in parsed.items():
        name = str(raw_name).strip().lower()
        if not name:
            raise ValueError("Concurrency limit names must be non-empty strings.")
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Concurrency limit for {name!r} must be an integer.") from exc
        if limit < 1 or limit > 100:
            raise ValueError(f"Concurrency limit for {name!r} must be between 1 and 100.")
        limits[name] = limit
    return limits


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
        # signed, zero, or otherwise non-numeric values. ASCII digits only
        # (isdigit() would accept unicode digits/superscripts).
        if not _ID_RE.fullmatch(text):
            raise ValueError(
                f"Invalid Telegram user ID {text!r}: must be a positive numeric ID, not a username."
            )
        num = int(text)
        if num <= 0 or num > 2**63 - 1:
            raise ValueError(
                f"Invalid Telegram user ID {text!r}: must be a positive numeric ID, not a username."
            )
        parsed.add(num)
    return parsed


AllowedUserIds = Annotated[set[int], BeforeValidator(_parse_allowed_user_ids), NoDecode]

ProviderPriority = Annotated[list[str], NoDecode]


def _parse_domain_list(value: Any) -> list[str]:
    """Parse OFFICIAL_ALLOWED_DOMAINS into a normalized domain list."""
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        items: list[Any] = list(value)
    elif isinstance(value, str):
        items = _split_list(value)
    else:
        raise ValueError("OFFICIAL_ALLOWED_DOMAINS must be comma-separated domains.")
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        domain = str(item).strip().lower().rstrip(".")
        if not domain:
            continue
        if "://" in domain or "/" in domain or " " in domain:
            raise ValueError(f"Invalid domain {item!r}: must be a bare hostname.")
        if domain not in seen:
            seen.add(domain)
            out.append(domain)
    return out


OfficialDomains = Annotated[list[str], BeforeValidator(_parse_domain_list), NoDecode]


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
    max_concurrent_jobs: int = Field(default=3, alias="MAX_CONCURRENT_JOBS", ge=1, le=10)
    max_active_per_user: int = Field(default=1, alias="MAX_ACTIVE_PER_USER", ge=1, le=5)
    requests_per_user_per_day: int = Field(
        default=10, alias="REQUESTS_PER_USER_PER_DAY", ge=1, le=1000
    )
    deep_requests_per_user_per_day: int = Field(
        default=3, alias="DEEP_REQUESTS_PER_USER_PER_DAY", ge=0, le=100
    )
    search_subqueries_per_job: int = Field(default=6, alias="SEARCH_SUBQUERIES_PER_JOB", ge=1, le=6)
    search_variants_per_job: int = Field(default=6, alias="SEARCH_VARIANTS_PER_JOB", ge=1, le=6)
    search_tasks_per_job: int = Field(default=12, alias="SEARCH_TASKS_PER_JOB", ge=1, le=50)
    sources_per_job: int = Field(default=12, alias="SOURCES_PER_JOB", ge=1, le=15)
    source_budget: int = Field(default=12, alias="SOURCE_BUDGET", ge=1, le=15)
    query_budget: int = Field(default=6, alias="QUERY_BUDGET", ge=1, le=6)
    variant_budget: int = Field(default=6, alias="VARIANT_BUDGET", ge=1, le=6)
    task_budget: int = Field(default=12, alias="TASK_BUDGET", ge=1, le=50)
    search_tool_concurrency: int = Field(default=1, alias="SEARCH_TOOL_CONCURRENCY", ge=1, le=100)
    search_provider_concurrency: int = Field(
        default=1, alias="SEARCH_PROVIDER_CONCURRENCY", ge=1, le=100
    )
    search_tool_limits: dict[str, int] = Field(default_factory=dict, alias="SEARCH_TOOL_LIMITS")
    search_provider_limits: dict[str, int] = Field(
        default_factory=dict, alias="SEARCH_PROVIDER_LIMITS"
    )
    token_budget: int = Field(default=40_000, alias="TOKEN_BUDGET", ge=1_000, le=100_000)
    time_budget_seconds: int = Field(default=300, alias="TIME_BUDGET_SECONDS", ge=30, le=600)
    quick_source_budget: int = Field(default=5, alias="QUICK_SOURCE_BUDGET", ge=3, le=5)
    standard_source_budget: int = Field(default=8, alias="STANDARD_SOURCE_BUDGET", ge=5, le=10)
    deep_source_budget: int = Field(default=12, alias="DEEP_SOURCE_BUDGET", ge=8, le=15)
    quick_token_budget: int = Field(default=8_000, alias="QUICK_TOKEN_BUDGET", ge=1_000, le=100_000)
    standard_token_budget: int = Field(
        default=20_000, alias="STANDARD_TOKEN_BUDGET", ge=1_000, le=100_000
    )
    deep_token_budget: int = Field(default=40_000, alias="DEEP_TOKEN_BUDGET", ge=1_000, le=100_000)
    quick_time_budget_seconds: int = Field(
        default=120, alias="QUICK_TIME_BUDGET_SECONDS", ge=30, le=600
    )
    standard_time_budget_seconds: int = Field(
        default=240, alias="STANDARD_TIME_BUDGET_SECONDS", ge=30, le=600
    )
    deep_time_budget_seconds: int = Field(
        default=300, alias="DEEP_TIME_BUDGET_SECONDS", ge=30, le=600
    )
    quick_max_tools: int = Field(default=2, alias="QUICK_MAX_TOOLS", ge=1, le=6)
    standard_max_tools: int = Field(default=4, alias="STANDARD_MAX_TOOLS", ge=1, le=6)
    deep_max_tools: int = Field(default=6, alias="DEEP_MAX_TOOLS", ge=1, le=6)
    chars_per_source: int = Field(default=20000, alias="CHARS_PER_SOURCE", ge=1000, le=100000)
    reader_context_chars: int = Field(
        default=40000, alias="READER_CONTEXT_CHARS", ge=4000, le=500000
    )
    repair_cycles: int = Field(default=1, alias="REPAIR_CYCLES", ge=0, le=1)
    job_timeout_seconds: int = Field(default=300, alias="JOB_TIMEOUT_SECONDS", ge=30, le=600)
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

    # Safe extraction (M4)
    extraction_connect_timeout_seconds: float = Field(
        default=5.0, alias="EXTRACTION_CONNECT_TIMEOUT_SECONDS", gt=0
    )
    extraction_read_timeout_seconds: float = Field(
        default=20.0, alias="EXTRACTION_READ_TIMEOUT_SECONDS", gt=0
    )
    extraction_write_timeout_seconds: float = Field(
        default=5.0, alias="EXTRACTION_WRITE_TIMEOUT_SECONDS", gt=0
    )
    extraction_pool_timeout_seconds: float = Field(
        default=5.0, alias="EXTRACTION_POOL_TIMEOUT_SECONDS", gt=0
    )
    max_redirects: int = Field(default=3, alias="MAX_REDIRECTS", ge=0, le=10)
    max_response_bytes: int = Field(
        default=2_000_000, alias="MAX_RESPONSE_BYTES", ge=1_024, le=50_000_000
    )
    respect_robots_txt: bool = Field(default=True, alias="RESPECT_ROBOTS_TXT")
    jina_reader_enabled: bool = Field(default=False, alias="JINA_READER_ENABLED")
    jina_reader_base_url: str = Field(default="https://r.jina.ai/", alias="JINA_READER_BASE_URL")
    jina_reader_api_key: SecretStr = Field(default=SecretStr(""), alias="JINA_READER_API_KEY")

    # Provider credentials (injected at runtime, never logged)
    groq_api_key: SecretStr = Field(default=SecretStr(""), alias="GROQ_API_KEY")
    openrouter_api_key: SecretStr = Field(default=SecretStr(""), alias="OPENROUTER_API_KEY")
    cloudflare_api_key: SecretStr = Field(default=SecretStr(""), alias="CLOUDFLARE_API_KEY")
    cloudflare_account_id: SecretStr = Field(default=SecretStr(""), alias="CLOUDFLARE_ACCOUNT_ID")
    tavily_api_key: SecretStr = Field(default=SecretStr(""), alias="TAVILY_API_KEY")
    brave_api_key: SecretStr = Field(default=SecretStr(""), alias="BRAVE_API_KEY")
    exa_api_key: SecretStr = Field(default=SecretStr(""), alias="EXA_API_KEY")
    serpapi_api_key: SecretStr = Field(default=SecretStr(""), alias="SERPAPI_API_KEY")
    github_token: SecretStr = Field(default=SecretStr(""), alias="GITHUB_TOKEN")
    searxng_base_url: str = Field(default="", alias="SEARXNG_BASE_URL")
    stackexchange_api_key: SecretStr = Field(default=SecretStr(""), alias="STACKEXCHANGE_API_KEY")
    semantic_scholar_api_key: SecretStr = Field(
        default=SecretStr(""), alias="SEMANTIC_SCHOLAR_API_KEY"
    )
    ncbi_api_key: SecretStr = Field(default=SecretStr(""), alias="NCBI_API_KEY")
    crossref_mailto: str = Field(default="", alias="CROSSREF_MAILTO")
    openalex_mailto: str = Field(default="", alias="OPENALEX_MAILTO")
    ncbi_email: str = Field(default="", alias="NCBI_EMAIL")
    ncbi_tool: str = Field(default="deep-research-agent", alias="NCBI_TOOL")
    official_allowed_domains: OfficialDomains = Field(
        default_factory=list, alias="OFFICIAL_ALLOWED_DOMAINS"
    )

    # Capability -> model resolution stays in configuration, not source constants.
    llm_provider_priority: ProviderPriority = Field(
        default_factory=lambda: ["groq", "openrouter", "cloudflare"],
        alias="LLM_PROVIDER_PRIORITY",
        min_length=1,
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
    langsmith_environment: Literal["development", "staging", "production"] = Field(
        default="development", alias="LANGSMITH_ENVIRONMENT"
    )
    langsmith_trace_content: bool = Field(default=False, alias="LANGSMITH_TRACE_CONTENT")
    langsmith_trace_sensitive_content: bool = Field(
        default=False, alias="LANGSMITH_TRACE_SENSITIVE_CONTENT"
    )
    langsmith_sample_rate: float = Field(default=1.0, alias="LANGSMITH_SAMPLE_RATE", ge=0.0, le=1.0)

    @field_validator("llm_provider_priority", mode="before")
    @classmethod
    def _split_priority(cls, value: Any) -> Any:
        if isinstance(value, str):
            return _normalize_priority(_split_list(value))
        if isinstance(value, (list, tuple, set)):
            return _normalize_priority([str(v) for v in value])
        return value

    @field_validator("model_map", mode="before")
    @classmethod
    def _parse_model_map(cls, value: Any) -> Any:
        if value is None or value == "":
            return {}
        if isinstance(value, dict):
            parsed = value
        elif isinstance(value, str):
            import json

            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("MODEL_MAP must be a JSON object.") from exc
            if not isinstance(parsed, dict):
                raise ValueError("MODEL_MAP must be a JSON object.")
        else:
            raise ValueError("MODEL_MAP must be a JSON object.")
        out: dict[str, str] = {}
        for key, val in parsed.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("MODEL_MAP keys must be non-empty strings.")
            if not isinstance(val, str) or not val.strip():
                raise ValueError("MODEL_MAP values must be non-empty strings.")
            out[key.strip()] = val.strip()
        unknown_caps = sorted(k for k in out if k not in KNOWN_MODEL_CAPABILITIES)
        if unknown_caps:
            raise ValueError(f"Unknown MODEL_MAP capabilities: {unknown_caps}.")
        return out

    @field_validator("search_tool_limits", "search_provider_limits", mode="before")
    @classmethod
    def _parse_search_limits(cls, value: Any) -> Any:
        return _parse_concurrency_limits(value)

    @model_validator(mode="after")
    def _check_context_budgets(self) -> Settings:
        if self.reader_context_chars < self.chars_per_source:
            raise ValueError("READER_CONTEXT_CHARS must be >= CHARS_PER_SOURCE.")
        return self


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
        if (
            "cloudflare" in settings.llm_provider_priority
            and provider_keys.get("cloudflare")
            and not settings.cloudflare_account_id.get_secret_value()
        ):
            raise ValueError("CLOUDFLARE_ACCOUNT_ID is required when CLOUDFLARE_API_KEY is set.")
    if settings.langsmith_tracing and not settings.langsmith_api_key.get_secret_value():
        raise ValueError("LANGSMITH_API_KEY is required when LANGSMITH_TRACING is true.")
    return settings
