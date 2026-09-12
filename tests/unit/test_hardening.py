"""Regression tests for the M1/M2 hardening pass (M3-ready)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from research_agent.config import Settings, validate_at_startup
from research_agent.errors import (
    ErrorCategory,
    is_fallback_eligible,
    is_retryable,
    needs_cooldown,
    to_trace_error_type,
)
from research_agent.llm.base import ProviderCapability, ProviderMetadata
from research_agent.models.reports import canonicalize_url
from research_agent.observability.redaction import redact_mapping, redact_text
from research_agent.services.sessions import normalize_language
from research_agent.telegram.renderer import render_citation_marker


def test_priority_env_comma_form_parses() -> None:
    s = Settings(_env_file=None, LLM_PROVIDER_PRIORITY="groq,openrouter")
    assert s.llm_provider_priority == ["groq", "openrouter"]
    validate_at_startup(s)


def test_priority_normalizes_case_and_dedups() -> None:
    s = Settings(_env_file=None, LLM_PROVIDER_PRIORITY="Groq, groq, OPENROUTER")
    assert s.llm_provider_priority == ["groq", "openrouter"]


def test_priority_rejects_empty() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, LLM_PROVIDER_PRIORITY=[])


def test_model_map_rejects_bad_values() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, MODEL_MAP='{"fast_multilingual": 123}')
    with pytest.raises(ValidationError):
        Settings(_env_file=None, MODEL_MAP='{"nope_cap": "model-x"}')
    s = Settings(_env_file=None, MODEL_MAP='{"fast_multilingual": "groq-x"}')
    assert s.model_map == {"fast_multilingual": "groq-x"}


def test_reader_context_must_cover_source() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, CHARS_PER_SOURCE=20000, READER_CONTEXT_CHARS=4000)


def test_canonicalize_rejects_scheme_and_strips_trackers() -> None:
    with pytest.raises(ValueError):
        canonicalize_url("ftp://example.com/a")
    a = canonicalize_url("https://example.com/a?utm_source=x&b=2")
    b = canonicalize_url("https://example.com:443/a?b=2&gbraid=zzz")
    assert a == b == "https://example.com/a?b=2"


def test_canonicalize_dot_segment_and_fragment() -> None:
    assert canonicalize_url("https://example.com/a/../b#frag") == "https://example.com/b"


def test_provider_metadata_hashable_and_supports() -> None:
    meta = ProviderMetadata(
        name="groq",
        model_id="m",
        capabilities=frozenset({ProviderCapability.FAST_MULTILINGUAL}),
    )
    assert hash(meta) is not None
    assert meta.supports(frozenset({ProviderCapability.FAST_MULTILINGUAL}))
    assert not meta.supports(frozenset({ProviderCapability.REASONING}))


def test_error_routing_helpers() -> None:
    assert is_retryable(ErrorCategory.TRANSIENT)
    assert not is_retryable(ErrorCategory.AUTH)
    assert needs_cooldown(ErrorCategory.RATE_LIMITED)
    assert is_fallback_eligible(ErrorCategory.BUDGET_EXHAUSTED)
    assert to_trace_error_type(ErrorCategory.TIMEOUT) == "timeout"


def test_redaction_covers_providers_and_json() -> None:
    assert "gsk_" not in redact_text("key=gsk_abc1234567890")
    assert "tvly-" not in redact_text("key=tvly-abc1234567890")
    assert redact_text('{"api_key": "secret123"}') != '{"api_key": "secret123"}'
    assert redact_mapping({"X-API-Key": "v"})["X-API-Key"] == "***"
    assert redact_mapping({"bot_token": "v"})["bot_token"] == "***"  # noqa: S105


def test_normalize_language_prefix_fallback() -> None:
    assert normalize_language("ar-DZ") == "ar"
    assert normalize_language("en-AU") == "en"
    assert normalize_language("xx") is None


def test_citation_marker_escaped_for_markdownv2() -> None:
    assert render_citation_marker(1) == "\\[1\\]"
