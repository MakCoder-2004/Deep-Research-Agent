"""Capability-based LLM routing (M3.8) with config-driven models (M3.11)."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

from research_agent.config import Settings
from research_agent.errors import ErrorCategory
from research_agent.llm.base import LLMProvider, ProviderCapability, ProviderError, ProviderMetadata

__all__ = [
    "LLMRouter",
    "PROVIDER_CAPABILITIES",
    "build_router_from_settings",
]

logger = logging.getLogger(__name__)

_ALL_CAPABILITIES: frozenset[ProviderCapability] = frozenset(
    {
        ProviderCapability.FAST_MULTILINGUAL,
        ProviderCapability.LONG_CONTEXT,
        ProviderCapability.REASONING,
        ProviderCapability.STRUCTURED_OUTPUT,
        ProviderCapability.ARABIC_CAPABLE,
        ProviderCapability.TOOL_CALLING,
    }
)

_LIGHTWEIGHT_CAPABILITIES: frozenset[ProviderCapability] = frozenset(
    {
        ProviderCapability.FAST_MULTILINGUAL,
        ProviderCapability.ARABIC_CAPABLE,
        ProviderCapability.STRUCTURED_OUTPUT,
    }
)

PROVIDER_CAPABILITIES: dict[str, frozenset[ProviderCapability]] = {
    "groq": _ALL_CAPABILITIES,
    "openrouter": _ALL_CAPABILITIES,
    "cloudflare": _LIGHTWEIGHT_CAPABILITIES,
}

# Preferred capability order when selecting a configured model identifier
# for a provider. Keys are capabilities (allowed); values come from
# Settings.model_map so no model identifier lives in source (M3.11).
_PROVIDER_MODEL_PREFERENCE: dict[str, tuple[ProviderCapability, ...]] = {
    "groq": (
        ProviderCapability.FAST_MULTILINGUAL,
        ProviderCapability.LONG_CONTEXT,
        ProviderCapability.ARABIC_CAPABLE,
        ProviderCapability.STRUCTURED_OUTPUT,
        ProviderCapability.TOOL_CALLING,
        ProviderCapability.REASONING,
    ),
    "openrouter": (
        ProviderCapability.REASONING,
        ProviderCapability.LONG_CONTEXT,
        ProviderCapability.STRUCTURED_OUTPUT,
        ProviderCapability.FAST_MULTILINGUAL,
        ProviderCapability.ARABIC_CAPABLE,
        ProviderCapability.TOOL_CALLING,
    ),
    "cloudflare": (
        ProviderCapability.FAST_MULTILINGUAL,
        ProviderCapability.ARABIC_CAPABLE,
        ProviderCapability.STRUCTURED_OUTPUT,
        ProviderCapability.LONG_CONTEXT,
        ProviderCapability.REASONING,
        ProviderCapability.TOOL_CALLING,
    ),
}


def _select_model_id(provider_name: str, model_map: dict[str, str]) -> str:
    """Pick a configured model identifier for a provider without hard-coding names."""
    preferences = _PROVIDER_MODEL_PREFERENCE.get(provider_name, ())
    for capability in preferences:
        model_id = model_map.get(capability.value)
        if model_id:
            return model_id
    for key in sorted(model_map):
        model_id = model_map[key]
        if model_id:
            return model_id
    return ""


def _normalize_required(
    required: ProviderCapability | Iterable[ProviderCapability],
) -> frozenset[ProviderCapability]:
    if isinstance(required, ProviderCapability):
        return frozenset({required})
    return frozenset(required)


class LLMRouter:
    """Register providers and resolve capability sets in priority order (M3.8)."""

    def __init__(self, provider_priority: Sequence[str] | None = None) -> None:
        self._providers: dict[str, LLMProvider] = {}
        self._priority: list[str] = []
        if provider_priority is not None:
            for name in provider_priority:
                normalized = str(name).strip().lower()
                if normalized and normalized not in self._priority:
                    self._priority.append(normalized)

    @property
    def provider_priority(self) -> list[str]:
        return list(self._priority)

    @property
    def providers(self) -> list[LLMProvider]:
        return sorted(self._providers.values(), key=self._sort_key)

    def _sort_key(self, provider: LLMProvider) -> tuple[int, str]:
        name = provider.metadata.name.strip().lower()
        try:
            index = self._priority.index(name)
        except ValueError:
            index = len(self._priority)
        return (index, name)

    def register(self, provider: LLMProvider) -> None:
        name = provider.metadata.name.strip().lower()
        if not name:
            raise ValueError("Provider name must be non-empty.")
        self._providers[name] = provider
        if name not in self._priority:
            self._priority.append(name)
        logger.debug("llm provider registered: name=%s", name)

    def get(self, name: str) -> LLMProvider | None:
        return self._providers.get(name.strip().lower())

    def resolve_all(
        self, required: ProviderCapability | Iterable[ProviderCapability]
    ) -> list[LLMProvider]:
        needed = _normalize_required(required)
        matching = [
            provider for provider in self._providers.values() if provider.metadata.supports(needed)
        ]
        return sorted(matching, key=self._sort_key)

    def resolve(self, required: ProviderCapability | Iterable[ProviderCapability]) -> LLMProvider:
        candidates = self.resolve_all(required)
        if not candidates:
            needed = _normalize_required(required)
            names = sorted(cap.value for cap in needed)
            raise ProviderError(
                f"No LLM provider supports capabilities: {names}.",
                category=ErrorCategory.UNAVAILABLE,
                provider="router",
            )
        return candidates[0]

    def __len__(self) -> int:
        return len(self._providers)

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        return name.strip().lower() in self._providers


def build_router_from_settings(
    settings: Settings,
    *,
    timeout_seconds: float = 30.0,
) -> LLMRouter:
    """Build a router from Settings priority and model map (M3.11).

    Model identifiers come only from ``settings.model_map`` and ordering
    only from ``settings.llm_provider_priority``. No model name literal
    lives in source.
    """
    # Local imports keep router usable without optional HTTP deps at import time.
    from research_agent.llm.providers.cloudflare import CloudflareProvider
    from research_agent.llm.providers.groq import GroqProvider
    from research_agent.llm.providers.openrouter import OpenRouterProvider

    router = LLMRouter(provider_priority=list(settings.llm_provider_priority))
    for index, raw_name in enumerate(settings.llm_provider_priority):
        name = str(raw_name).strip().lower()
        capabilities = PROVIDER_CAPABILITIES.get(name, frozenset())
        metadata = ProviderMetadata(
            name=name,
            model_id=_select_model_id(name, settings.model_map),
            capabilities=capabilities,
            priority=index,
        )
        if name == "groq":
            key = settings.groq_api_key.get_secret_value()
            router.register(
                GroqProvider(
                    model_id=metadata.model_id,
                    api_key=key,
                    timeout_seconds=timeout_seconds,
                    capabilities=metadata.capabilities,
                    priority=index,
                )
            )
        elif name == "openrouter":
            key = settings.openrouter_api_key.get_secret_value()
            router.register(
                OpenRouterProvider(
                    model_id=metadata.model_id,
                    api_key=key,
                    timeout_seconds=timeout_seconds,
                    capabilities=metadata.capabilities,
                    priority=index,
                )
            )
        elif name == "cloudflare":
            key = settings.cloudflare_api_key.get_secret_value()
            account = settings.cloudflare_account_id.get_secret_value()
            router.register(
                CloudflareProvider(
                    model_id=metadata.model_id,
                    api_key=key,
                    account_id=account,
                    timeout_seconds=timeout_seconds,
                    capabilities=metadata.capabilities,
                    priority=index,
                )
            )
        else:
            logger.debug("llm provider skipped: name=%s", name)
    return router
