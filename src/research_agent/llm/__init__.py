"""LLM provider interfaces, routing, and startup health."""

from research_agent.errors import ErrorCategory
from research_agent.llm.base import (
    LLMProvider,
    ProviderCapability,
    ProviderError,
    ProviderMetadata,
)
from research_agent.llm.health import (
    CapabilityResolution,
    ProviderHealth,
    StartupHealth,
    check_llm_health,
    check_startup_health,
    resolve_configured_capabilities,
)
from research_agent.llm.providers import (
    CloudflareProvider,
    GroqProvider,
    OpenRouterProvider,
)
from research_agent.llm.router import LLMRouter, build_router_from_settings

__all__ = [
    "CapabilityResolution",
    "CloudflareProvider",
    "ErrorCategory",
    "GroqProvider",
    "LLMProvider",
    "LLMRouter",
    "OpenRouterProvider",
    "ProviderCapability",
    "ProviderError",
    "ProviderHealth",
    "ProviderMetadata",
    "StartupHealth",
    "build_router_from_settings",
    "check_llm_health",
    "check_startup_health",
    "resolve_configured_capabilities",
]
