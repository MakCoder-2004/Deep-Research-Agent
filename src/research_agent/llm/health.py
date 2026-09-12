"""Startup health checks resolving configured capabilities to models (M3.10)."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass

from research_agent.config import Settings
from research_agent.llm.base import LLMProvider, ProviderCapability
from research_agent.llm.router import LLMRouter, build_router_from_settings
from research_agent.observability.redaction import redact_text

__all__ = [
    "CapabilityResolution",
    "ProviderHealth",
    "StartupHealth",
    "check_llm_health",
    "check_startup_health",
    "resolve_configured_capabilities",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderHealth:
    name: str
    model_id: str
    available: bool
    error: str | None = None
    latency_ms: float | None = None


@dataclass(frozen=True)
class CapabilityResolution:
    capability: str
    provider: str | None
    model_id: str | None
    available: bool


@dataclass(frozen=True)
class StartupHealth:
    providers: dict[str, ProviderHealth]
    capabilities: dict[str, CapabilityResolution]


async def check_llm_health(
    router: LLMRouter,
    *,
    timeout_seconds: float = 5.0,
) -> dict[str, ProviderHealth]:
    """Check each registered provider without leaking secrets.

    Never raises for provider failures; only ``asyncio.CancelledError``
    propagates. Each check honors ``timeout_seconds`` via ``asyncio.timeout``
    so a hung provider cannot block startup.
    """
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive.")
    results: dict[str, ProviderHealth] = {}
    for provider in router.providers:
        metadata = provider.metadata
        name = metadata.name
        start = time.perf_counter()
        try:
            async with asyncio.timeout(timeout_seconds):
                ok = await provider.health_check()
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            latency = (time.perf_counter() - start) * 1000.0
            results[name] = ProviderHealth(
                name=name,
                model_id=metadata.model_id,
                available=False,
                error="timeout",
                latency_ms=latency,
            )
            logger.warning("llm health timeout: provider=%s", name)
            continue
        except Exception as exc:
            latency = (time.perf_counter() - start) * 1000.0
            safe = redact_text(str(exc))
            logger.warning("llm health failed: provider=%s error=%s", name, safe[:200])
            results[name] = ProviderHealth(
                name=name,
                model_id=metadata.model_id,
                available=False,
                error="unavailable",
                latency_ms=latency,
            )
            continue
        latency = (time.perf_counter() - start) * 1000.0
        if ok:
            results[name] = ProviderHealth(
                name=name,
                model_id=metadata.model_id,
                available=True,
                error=None,
                latency_ms=latency,
            )
            logger.info("llm health ok: provider=%s", name)
        else:
            results[name] = ProviderHealth(
                name=name,
                model_id=metadata.model_id,
                available=False,
                error="unavailable",
                latency_ms=latency,
            )
            logger.warning("llm health unavailable: provider=%s", name)
    return results


async def _close_temporary_providers(router: LLMRouter) -> None:
    """Close clients created by a temporary startup router, if supported."""
    for provider in router.providers:
        close = getattr(provider, "aclose", None)
        if not callable(close):
            continue
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - cleanup must not hide startup status
            logger.debug("llm provider close failed: provider=%s", provider.metadata.name)


def resolve_configured_capabilities(
    settings: Settings,
    router: LLMRouter,
    health: Mapping[str, ProviderHealth] | None = None,
) -> dict[str, CapabilityResolution]:
    """Resolve each configured capability to the first available provider model."""
    resolved: dict[str, CapabilityResolution] = {}
    for capability_name in sorted(settings.model_map):
        try:
            capability = ProviderCapability(capability_name)
        except ValueError:
            continue
        configured_model = settings.model_map[capability_name]
        candidates: list[LLMProvider] = router.resolve_all(capability)
        chosen: LLMProvider | None = None
        for provider in candidates:
            # This guard also protects callers that provide a manually-built
            # router whose metadata does not match the configured runtime model.
            if provider.metadata.model_id != configured_model:
                continue
            if health is None:
                chosen = provider
                break
            status = health.get(provider.metadata.name)
            if (
                status is not None
                and status.available
                and status.model_id == provider.metadata.model_id
            ):
                chosen = provider
                break
        if chosen is None:
            resolved[capability_name] = CapabilityResolution(
                capability=capability_name,
                provider=None,
                model_id=configured_model,
                available=False,
            )
        else:
            resolved[capability_name] = CapabilityResolution(
                capability=capability_name,
                provider=chosen.metadata.name,
                model_id=configured_model or chosen.metadata.model_id,
                available=True,
            )
    # Capabilities with no configured model but supported by the router are
    # left out: M3.11 requires model identifiers to come from configuration.
    return resolved


async def check_startup_health(
    settings: Settings,
    router: LLMRouter | None = None,
    *,
    timeout_seconds: float = 5.0,
) -> StartupHealth:
    """Build (if needed), check, and resolve providers to configured models."""
    owns_router = router is None
    active = router if router is not None else build_router_from_settings(settings)
    try:
        provider_health = await check_llm_health(active, timeout_seconds=timeout_seconds)
        # Mark capabilities unavailable when their chosen provider is unhealthy.
        capabilities = resolve_configured_capabilities(settings, active, provider_health)
        unavailable = sorted(name for name, cap in capabilities.items() if not cap.available)
        if unavailable:
            logger.warning("llm capabilities unresolved: count=%d", len(unavailable))
        else:
            logger.info("llm startup health complete: providers=%d", len(provider_health))
        return StartupHealth(providers=provider_health, capabilities=capabilities)
    finally:
        if owns_router:
            await _close_temporary_providers(active)
