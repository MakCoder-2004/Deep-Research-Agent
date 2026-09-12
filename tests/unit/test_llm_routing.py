"""Mocked contract tests for capability routing and LLM adapters (M3.8-M3.11)."""

from __future__ import annotations

import asyncio
import pathlib

import httpx
import pytest
import respx
from pydantic import BaseModel

from research_agent.config import Settings
from research_agent.errors import ErrorCategory
from research_agent.llm.base import ProviderCapability, ProviderError, ProviderMetadata
from research_agent.llm.health import (
    check_llm_health,
    check_startup_health,
    resolve_configured_capabilities,
)
from research_agent.llm.providers._common import (
    category_for_status,
    extract_json_payload,
    map_transport_error,
    parse_json_object,
    read_choice_text,
)
from research_agent.llm.providers.cloudflare import CloudflareProvider
from research_agent.llm.providers.groq import GroqProvider
from research_agent.llm.providers.openrouter import OpenRouterProvider
from research_agent.llm.router import LLMRouter, build_router_from_settings


class _Answer(BaseModel):
    city: str


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"_env_file": None}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _fake_provider(
    name: str,
    caps: frozenset[ProviderCapability],
    model: str = "m",
    priority: int = 0,
) -> GroqProvider:
    return GroqProvider(
        model_id=model,
        api_key="key",
        capabilities=caps,
        priority=priority,
        base_url="https://example.com/v1",
    )


# M3.8 router registration and resolution


def test_router_register_and_resolve_priority_order() -> None:
    router = LLMRouter(provider_priority=["groq", "openrouter", "cloudflare"])
    groq = _fake_provider(
        "groq",
        frozenset({ProviderCapability.FAST_MULTILINGUAL}),
        model="gm",
        priority=0,
    )
    # Bypass name check by registering with groq metadata (name is groq).
    router.register(groq)
    cloud = CloudflareProvider(
        model_id="cm",
        api_key="key",
        account_id="acct",
        capabilities=frozenset({ProviderCapability.FAST_MULTILINGUAL}),
        priority=2,
    )
    router.register(cloud)
    assert len(router) == 2
    assert "groq" in router
    assert "missing" not in router
    assert router.get("GROQ") is groq
    resolved = router.resolve(ProviderCapability.FAST_MULTILINGUAL)
    assert resolved.metadata.name == "groq"
    ordered = router.resolve_all(ProviderCapability.FAST_MULTILINGUAL)
    assert [p.metadata.name for p in ordered] == ["groq", "cloudflare"]
    assert router.providers[0].metadata.name == "groq"
    assert router.provider_priority[:2] == ["groq", "openrouter"]


def test_router_resolve_missing_raises_unavailable() -> None:
    router = LLMRouter(provider_priority=["groq"])
    router.register(_fake_provider("groq", frozenset({ProviderCapability.FAST_MULTILINGUAL})))
    with pytest.raises(ProviderError) as excinfo:
        router.resolve(ProviderCapability.REASONING)
    assert excinfo.value.category is ErrorCategory.UNAVAILABLE
    assert excinfo.value.provider == "router"


def test_router_register_rejects_empty_name() -> None:
    router = LLMRouter()
    bad = ProviderMetadata(name="  ", model_id="m", capabilities=frozenset())

    class _Bad:
        @property
        def metadata(self) -> ProviderMetadata:
            return bad

        async def complete(self, prompt: str) -> str:
            return ""

        async def structured(self, prompt: str, response_model: type[_Answer]) -> _Answer:
            return _Answer(city="x")

        async def health_check(self) -> bool:
            return True

    with pytest.raises(ValueError, match="non-empty"):
        router.register(_Bad())  # type: ignore[arg-type]
    assert ("nope" in router) is False


def test_router_resolve_multiple_capabilities() -> None:
    router = LLMRouter(provider_priority=["groq", "openrouter"])
    router.register(
        GroqProvider(
            model_id="m1",
            api_key="k",
            capabilities=frozenset(
                {ProviderCapability.FAST_MULTILINGUAL, ProviderCapability.REASONING}
            ),
            base_url="https://example.com/v1",
        )
    )
    router.register(
        OpenRouterProvider(
            model_id="m2",
            api_key="k",
            capabilities=frozenset({ProviderCapability.FAST_MULTILINGUAL}),
            base_url="https://example.com/v1",
        )
    )
    both = router.resolve([ProviderCapability.FAST_MULTILINGUAL, ProviderCapability.REASONING])
    assert both.metadata.name == "groq"
    assert len(router.resolve_all({ProviderCapability.REASONING})) == 1


# M3.11 factory uses config, no hard-coded models


def test_build_router_uses_priority_and_model_map() -> None:
    settings = _settings(
        LLM_PROVIDER_PRIORITY="groq,openrouter,cloudflare",
        MODEL_MAP=(
            '{"fast_multilingual": "cfg-fast", "reasoning": "cfg-reason", '
            '"long_context": "cfg-long", "structured_output": "cfg-struct", '
            '"arabic_capable": "cfg-ar", "tool_calling": "cfg-tool"}'
        ),
        GROQ_API_KEY="g-key",
        OPENROUTER_API_KEY="o-key",
        CLOUDFLARE_API_KEY="c-key",
        CLOUDFLARE_ACCOUNT_ID="c-acct",
    )
    router = build_router_from_settings(settings)
    assert len(router) == 3
    assert router.provider_priority == ["groq", "openrouter", "cloudflare"]
    by_name = {p.metadata.name: p.metadata.model_id for p in router.providers}
    # Groq prefers fast_multilingual, OpenRouter prefers reasoning.
    assert by_name["groq"] == "cfg-fast"
    assert by_name["openrouter"] == "cfg-reason"
    assert by_name["cloudflare"] == "cfg-fast"
    assert router.resolve(ProviderCapability.FAST_MULTILINGUAL).metadata.model_id == "cfg-fast"
    assert router.resolve(ProviderCapability.REASONING).metadata.model_id == "cfg-reason"


def test_build_router_empty_model_map_yields_empty_model() -> None:
    settings = _settings(LLM_PROVIDER_PRIORITY="groq", GROQ_API_KEY="k")
    router = build_router_from_settings(settings)
    assert router.get("groq") is not None
    assert router.get("groq") is not None and router.get("groq").metadata.model_id == ""  # type: ignore[union-attr]


def test_no_model_literals_in_source() -> None:
    root = pathlib.Path("src/research_agent/llm")
    forbidden = ["llama", "gpt", "mistral", "mixtral", "qwen", "gemma", "whisper"]
    hits: list[str] = []
    for path in [*root.glob("*.py"), *root.glob("providers/*.py")]:
        text = path.read_text(encoding="utf-8").lower()
        for marker in forbidden:
            if marker in text:
                hits.append(f"{path.name}:{marker}")
    assert hits == []


# _common helpers


def test_category_for_status_mapping() -> None:
    assert category_for_status(401, "") is ErrorCategory.AUTH
    assert category_for_status(429, "") is ErrorCategory.RATE_LIMITED
    assert category_for_status(408, "") is ErrorCategory.TIMEOUT
    assert category_for_status(400, "") is ErrorCategory.INVALID_REQUEST
    assert category_for_status(503, "") is ErrorCategory.UNAVAILABLE
    assert category_for_status(500, "") is ErrorCategory.TRANSIENT
    assert category_for_status(418, "") is ErrorCategory.INVALID_REQUEST
    assert category_for_status(400, "content_filter triggered") is (ErrorCategory.CONTENT_FILTERED)
    assert category_for_status(200, "") is ErrorCategory.UNKNOWN


def test_map_transport_error() -> None:
    assert map_transport_error(httpx.TimeoutException("t")) is ErrorCategory.TIMEOUT
    assert map_transport_error(httpx.ConnectError("c")) is ErrorCategory.UNAVAILABLE
    assert map_transport_error(httpx.ReadError("r")) is ErrorCategory.TRANSIENT
    assert map_transport_error(ValueError("v")) is ErrorCategory.UNKNOWN


def test_extract_and_parse_json() -> None:
    assert parse_json_object('{"city": "Cairo"}') == {"city": "Cairo"}
    fenced = '```json\n{"city": "Dubai"}\n```'
    assert parse_json_object(fenced) == {"city": "Dubai"}
    assert parse_json_object('note {"city": "Riyadh"} tail') == {"city": "Riyadh"}
    assert extract_json_payload("  hello  ") == "hello"
    with pytest.raises(ValueError):
        parse_json_object("[1, 2]")


def test_read_choice_text_shapes() -> None:
    assert read_choice_text({}) == ""
    assert read_choice_text({"choices": []}) == ""
    payload = {"choices": [{"message": {"content": "hi"}}]}
    assert read_choice_text(payload) == "hi"
    listed = {"choices": [{"message": {"content": [{"text": "a"}, {"text": "b"}]}}]}
    assert read_choice_text(listed) == "ab"
    assert read_choice_text({"choices": [{"message": {"content": None}}]}) == ""


# Groq adapter


@respx.mock
async def test_groq_complete_and_structured() -> None:
    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})
    )
    provider = GroqProvider(model_id="cfg-fast", api_key="secret-key")
    assert await provider.complete("hi") == "hello"

    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": '{"city": "Cairo"}'}}]}
        )
    )
    answer = await provider.structured("capital?", _Answer)
    assert answer.city == "Cairo"
    await provider.aclose()


@respx.mock
async def test_groq_structured_invalid_is_invalid_request() -> None:
    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})
    )
    provider = GroqProvider(model_id="m", api_key="k")
    with pytest.raises(ProviderError) as excinfo:
        await provider.structured("q", _Answer)
    assert excinfo.value.category is ErrorCategory.INVALID_REQUEST


@respx.mock
async def test_groq_error_mapping() -> None:
    provider = GroqProvider(model_id="m", api_key="k")
    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(429, text="slow down")
    )
    with pytest.raises(ProviderError) as excinfo:
        await provider.complete("hi")
    assert excinfo.value.category is ErrorCategory.RATE_LIMITED

    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(401, text="bad key")
    )
    with pytest.raises(ProviderError) as excinfo2:
        await provider.complete("hi")
    assert excinfo2.value.category is ErrorCategory.AUTH


async def test_groq_validation_and_missing_config() -> None:
    provider = GroqProvider(model_id="m", api_key="k")
    with pytest.raises(ProviderError) as excinfo:
        await provider.complete("   ")
    assert excinfo.value.category is ErrorCategory.INVALID_REQUEST
    with pytest.raises(ValueError):
        GroqProvider(model_id="m", api_key="k", timeout_seconds=0)

    no_key = GroqProvider(model_id="m", api_key="")
    with pytest.raises(ProviderError) as excinfo2:
        await no_key.complete("hi")
    assert excinfo2.value.category is ErrorCategory.AUTH

    no_model = GroqProvider(model_id="", api_key="k")
    with pytest.raises(ProviderError) as excinfo3:
        await no_model.complete("hi")
    assert excinfo3.value.category is ErrorCategory.INVALID_REQUEST
    assert await no_key.health_check() is False
    assert await no_model.health_check() is False


@respx.mock
async def test_groq_health_and_empty() -> None:
    respx.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    assert await GroqProvider(model_id="m", api_key="k").health_check() is False
    respx.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "other"}]})
    )
    assert await GroqProvider(model_id="m", api_key="k").health_check() is False
    respx.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "m"}]})
    )
    assert await GroqProvider(model_id="m", api_key="k").health_check() is True
    respx.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(500, text="err")
    )
    assert await GroqProvider(model_id="m", api_key="k").health_check() is False


@respx.mock
async def test_groq_empty_completion_is_invalid() -> None:
    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": []})
    )
    with pytest.raises(ProviderError) as excinfo:
        await GroqProvider(model_id="m", api_key="k").complete("hi")
    assert excinfo.value.category is ErrorCategory.INVALID_REQUEST


async def test_groq_propagates_cancelled() -> None:
    provider = GroqProvider(model_id="m", api_key="k")

    async def _boom(*args: object, **kwargs: object) -> httpx.Response:
        raise asyncio.CancelledError

    client = httpx.AsyncClient(transport=httpx.MockTransport(_boom))  # type: ignore[arg-type]
    owned = GroqProvider(model_id="m", api_key="k", client=client)
    with pytest.raises(asyncio.CancelledError):
        await owned.complete("hi")
    await client.aclose()
    assert provider.metadata.name == "groq"


# OpenRouter adapter


@respx.mock
async def test_openrouter_complete_structured_health() -> None:
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    )
    provider = OpenRouterProvider(model_id="cfg-reason", api_key="k")
    assert await provider.complete("hi") == "ok"

    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": '{"city": "Amman"}'}}]}
        )
    )
    assert (await provider.structured("q", _Answer)).city == "Amman"

    respx.get("https://openrouter.ai/api/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "cfg-reason"}]})
    )
    assert await provider.health_check() is True
    assert await OpenRouterProvider(model_id="", api_key="k").health_check() is False
    await provider.aclose()


@respx.mock
async def test_openrouter_transient_and_invalid() -> None:
    provider = OpenRouterProvider(model_id="m", api_key="k")
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(500, text="boom")
    )
    with pytest.raises(ProviderError) as excinfo:
        await provider.complete("hi")
    assert excinfo.value.category is ErrorCategory.TRANSIENT

    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "oops"}}]})
    )
    with pytest.raises(ProviderError) as excinfo2:
        await provider.structured("q", _Answer)
    assert excinfo2.value.category is ErrorCategory.INVALID_REQUEST

    with pytest.raises(ProviderError):
        await OpenRouterProvider(model_id="m", api_key="").complete("hi")
    with pytest.raises(ProviderError):
        await OpenRouterProvider(model_id="", api_key="k").complete("hi")
    with pytest.raises(ProviderError):
        await provider.complete("  ")
    with pytest.raises(ValueError):
        OpenRouterProvider(model_id="m", api_key="k", timeout_seconds=-1)


async def test_openrouter_propagates_cancelled() -> None:
    async def _boom(*args: object, **kwargs: object) -> httpx.Response:
        raise asyncio.CancelledError

    client = httpx.AsyncClient(transport=httpx.MockTransport(_boom))  # type: ignore[arg-type]
    provider = OpenRouterProvider(model_id="m", api_key="k", client=client)
    with pytest.raises(asyncio.CancelledError):
        await provider.complete("hi")
    await client.aclose()


# Cloudflare adapter


@respx.mock
async def test_cloudflare_complete_structured_health() -> None:
    run = "https://api.cloudflare.com/client/v4/accounts/acct/ai/run/cfg-fast"
    respx.post(run).mock(return_value=httpx.Response(200, json={"result": {"response": "hello"}}))
    provider = CloudflareProvider(model_id="cfg-fast", api_key="k", account_id="acct")
    assert await provider.complete("hi") == "hello"

    respx.post(run).mock(return_value=httpx.Response(200, json={"result": '{"city": "Doha"}'}))
    assert (await provider.structured("q", _Answer)).city == "Doha"

    respx.get("https://api.cloudflare.com/client/v4/accounts/acct/ai/models").mock(
        return_value=httpx.Response(200, json={"success": True, "result": [{"name": "cfg-fast"}]})
    )
    assert await provider.health_check() is True
    await provider.aclose()


@respx.mock
async def test_cloudflare_result_shapes_and_errors() -> None:
    from research_agent.llm.providers.cloudflare import _read_result_text

    assert _read_result_text({"result": "direct"}) == "direct"
    assert _read_result_text({"result": {"response": "nested"}}) == "nested"
    assert _read_result_text({"result": ["a", "b"]}) == "ab"
    assert _read_result_text({}) == ""
    assert _read_result_text({"result": {"other": "fallback"}}) == "fallback"

    run = "https://api.cloudflare.com/client/v4/accounts/acct/ai/run/m"
    respx.post(run).mock(return_value=httpx.Response(403, text="forbidden"))
    with pytest.raises(ProviderError) as excinfo:
        await CloudflareProvider(model_id="m", api_key="k", account_id="acct").complete("hi")
    assert excinfo.value.category is ErrorCategory.AUTH

    respx.post(run).mock(return_value=httpx.Response(200, json={"result": {"response": ""}}))
    with pytest.raises(ProviderError) as excinfo2:
        await CloudflareProvider(model_id="m", api_key="k", account_id="acct").complete("hi")
    assert excinfo2.value.category is ErrorCategory.INVALID_REQUEST

    respx.post(run).mock(return_value=httpx.Response(200, json={"result": {"response": "oops"}}))
    with pytest.raises(ProviderError) as excinfo3:
        await CloudflareProvider(model_id="m", api_key="k", account_id="acct").structured(
            "q", _Answer
        )
    assert excinfo3.value.category is ErrorCategory.INVALID_REQUEST


async def test_cloudflare_missing_config() -> None:
    with pytest.raises(ProviderError):
        await CloudflareProvider(model_id="m", api_key="", account_id="a").complete("hi")
    with pytest.raises(ProviderError):
        await CloudflareProvider(model_id="m", api_key="k", account_id="").complete("hi")
    with pytest.raises(ProviderError):
        await CloudflareProvider(model_id="", api_key="k", account_id="a").complete("hi")
    with pytest.raises(ProviderError):
        await CloudflareProvider(model_id="m", api_key="k", account_id="a").complete("  ")
    with pytest.raises(ValueError):
        CloudflareProvider(model_id="m", api_key="k", account_id="a", timeout_seconds=0)
    assert (
        await CloudflareProvider(model_id="", api_key="k", account_id="a").health_check() is False
    )


async def test_cloudflare_propagates_cancelled() -> None:
    async def _boom(*args: object, **kwargs: object) -> httpx.Response:
        raise asyncio.CancelledError

    client = httpx.AsyncClient(transport=httpx.MockTransport(_boom))  # type: ignore[arg-type]
    provider = CloudflareProvider(model_id="m", api_key="k", account_id="a", client=client)
    with pytest.raises(asyncio.CancelledError):
        await provider.complete("hi")
    await client.aclose()


# M3.10 health checks


@respx.mock
async def test_check_llm_health_maps_providers() -> None:
    respx.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "m1"}]})
    )
    respx.get("https://openrouter.ai/api/v1/models").mock(
        return_value=httpx.Response(500, text="down")
    )
    router = LLMRouter(provider_priority=["groq", "openrouter"])
    router.register(GroqProvider(model_id="m1", api_key="k"))
    router.register(OpenRouterProvider(model_id="m2", api_key="k"))
    health = await check_llm_health(router, timeout_seconds=5.0)
    assert health["groq"].available is True
    assert health["openrouter"].available is False
    assert health["groq"].model_id == "m1"
    assert health["groq"].latency_ms is not None
    # Secrets never appear in the status map.
    assert "k" not in str(health.values())


async def test_check_llm_health_propagates_cancelled() -> None:
    class _Hanging:
        @property
        def metadata(self) -> ProviderMetadata:
            return ProviderMetadata(
                name="hang",
                model_id="m",
                capabilities=frozenset({ProviderCapability.FAST_MULTILINGUAL}),
            )

        async def complete(self, prompt: str) -> str:
            return ""

        async def structured(self, prompt: str, response_model: type[_Answer]) -> _Answer:
            return _Answer(city="x")

        async def health_check(self) -> bool:
            raise asyncio.CancelledError

    router = LLMRouter()
    router.register(_Hanging())  # type: ignore[arg-type]
    with pytest.raises(asyncio.CancelledError):
        await check_llm_health(router, timeout_seconds=1.0)


async def test_check_llm_health_rejects_bad_timeout() -> None:
    router = LLMRouter()
    with pytest.raises(ValueError):
        await check_llm_health(router, timeout_seconds=0)


def test_resolve_configured_capabilities_uses_matching_healthy_fallback() -> None:
    settings = _settings(
        LLM_PROVIDER_PRIORITY="groq,openrouter,cloudflare",
        MODEL_MAP='{"fast_multilingual": "cfg-fast", "reasoning": "cfg-reason"}',
        GROQ_API_KEY="k",
        OPENROUTER_API_KEY="k",
        CLOUDFLARE_API_KEY="k",
        CLOUDFLARE_ACCOUNT_ID="a",
    )
    router = build_router_from_settings(settings)
    healthy = {
        "groq": __import__("research_agent.llm.health", fromlist=["ProviderHealth"]).ProviderHealth(
            name="groq", model_id="cfg-fast", available=False
        ),
        "openrouter": __import__(
            "research_agent.llm.health", fromlist=["ProviderHealth"]
        ).ProviderHealth(name="openrouter", model_id="cfg-reason", available=True),
        "cloudflare": __import__(
            "research_agent.llm.health", fromlist=["ProviderHealth"]
        ).ProviderHealth(name="cloudflare", model_id="cfg-fast", available=True),
    }
    resolved = resolve_configured_capabilities(settings, router, healthy)
    assert resolved["fast_multilingual"].provider == "cloudflare"
    assert resolved["fast_multilingual"].model_id == "cfg-fast"
    assert resolved["fast_multilingual"].available is True
    assert resolved["reasoning"].provider == "openrouter"
    assert resolved["reasoning"].model_id == "cfg-reason"


@respx.mock
async def test_check_startup_health_end_to_end() -> None:
    settings = _settings(
        LLM_PROVIDER_PRIORITY="groq,openrouter",
        MODEL_MAP='{"fast_multilingual": "cfg-fast"}',
        GROQ_API_KEY="k",
        OPENROUTER_API_KEY="k",
    )
    respx.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "cfg-fast"}]})
    )
    respx.get("https://openrouter.ai/api/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "cfg-fast"}]})
    )
    report = await check_startup_health(settings, timeout_seconds=5.0)
    assert report.providers["groq"].available is True
    assert report.capabilities["fast_multilingual"].available is True
    assert report.capabilities["fast_multilingual"].model_id == "cfg-fast"


def test_common_edge_branches() -> None:
    assert map_transport_error(httpx.HTTPError("boom")) is ErrorCategory.TRANSIENT
    # Fenced block without JSON falls through to brace search.
    assert parse_json_object('```\nnote\n``` {"city": "X"}') == {"city": "X"}
    assert read_choice_text("nope") == ""
    assert read_choice_text({"choices": "nope"}) == ""
    assert read_choice_text({"choices": ["nope"]}) == ""
    assert read_choice_text({"choices": [{"message": "nope"}]}) == ""
    assert read_choice_text({"choices": [{"message": {"content": 123}}]}) == ""
    mixed = {"choices": [{"message": {"content": ["skip", {"text": 1}, {"text": "ok"}]}}]}
    assert read_choice_text(mixed) == "ok"


def test_router_fallback_and_contains() -> None:
    from research_agent.llm.router import _select_model_id

    assert _select_model_id("unknown-provider", {"fast_multilingual": "m1"}) == "m1"
    assert _select_model_id("unknown-provider", {}) == ""
    router = LLMRouter()
    assert (123 in router) is False  # type: ignore[operator]
    # Provider outside the priority list sorts last but still resolves.
    router2 = LLMRouter(provider_priority=["groq"])
    router2.register(
        GroqProvider(
            model_id="m",
            api_key="k",
            capabilities=frozenset({ProviderCapability.REASONING}),
            base_url="https://example.com/v1",
        )
    )
    assert router2.resolve(ProviderCapability.REASONING).metadata.name == "groq"
    # Empty-name entries in priority are ignored.
    router3 = LLMRouter(provider_priority=["", "  ", "groq"])
    assert router3.provider_priority == ["groq"]


async def test_health_timeout_and_error_paths() -> None:
    from research_agent.llm.health import ProviderHealth

    class _TimeoutProv:
        @property
        def metadata(self) -> ProviderMetadata:
            return ProviderMetadata(
                name="tprov",
                model_id="m",
                capabilities=frozenset({ProviderCapability.FAST_MULTILINGUAL}),
            )

        async def complete(self, prompt: str) -> str:
            return ""

        async def structured(self, prompt: str, response_model: type[_Answer]) -> _Answer:
            return _Answer(city="x")

        async def health_check(self) -> bool:
            raise TimeoutError("slow")

    class _BoomProv:
        @property
        def metadata(self) -> ProviderMetadata:
            return ProviderMetadata(
                name="bprov",
                model_id="m",
                capabilities=frozenset({ProviderCapability.FAST_MULTILINGUAL}),
            )

        async def complete(self, prompt: str) -> str:
            return ""

        async def structured(self, prompt: str, response_model: type[_Answer]) -> _Answer:
            return _Answer(city="x")

        async def health_check(self) -> bool:
            raise RuntimeError("down")

    router = LLMRouter()
    router.register(_TimeoutProv())  # type: ignore[arg-type]
    router.register(_BoomProv())  # type: ignore[arg-type]
    health = await check_llm_health(router, timeout_seconds=2.0)
    assert health["tprov"].available is False
    assert health["tprov"].error == "timeout"
    assert health["bprov"].available is False
    assert health["bprov"].error == "unavailable"
    assert isinstance(health["tprov"], ProviderHealth)


def test_resolve_unhealthy_and_unknown_capability() -> None:
    from research_agent.llm.health import ProviderHealth

    settings = _settings(
        LLM_PROVIDER_PRIORITY="groq",
        MODEL_MAP='{"fast_multilingual": "cfg-fast"}',
        GROQ_API_KEY="k",
    )
    router = build_router_from_settings(settings)
    down = {
        "groq": ProviderHealth(name="groq", model_id="cfg-fast", available=False),
    }
    resolved = resolve_configured_capabilities(settings, router, down)
    assert resolved["fast_multilingual"].available is False
    assert resolved["fast_multilingual"].provider is None
    # Unknown capability keys are skipped without raising.
    settings.model_map["not_a_capability"] = "m"
    resolved2 = resolve_configured_capabilities(settings, router, None)
    assert "not_a_capability" not in resolved2


@respx.mock
async def test_startup_reports_unresolved() -> None:
    settings = _settings(
        LLM_PROVIDER_PRIORITY="groq",
        MODEL_MAP='{"fast_multilingual": "cfg-fast"}',
        GROQ_API_KEY="k",
    )
    respx.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(500, text="down")
    )
    report = await check_startup_health(settings, timeout_seconds=2.0)
    assert report.providers["groq"].available is False
    assert report.capabilities["fast_multilingual"].available is False


async def test_provider_transport_timeout_and_bad_json() -> None:
    async def _timeout(*args: object, **kwargs: object) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    client = httpx.AsyncClient(transport=httpx.MockTransport(_timeout))  # type: ignore[arg-type]
    with pytest.raises(ProviderError) as excinfo:
        await GroqProvider(model_id="m", api_key="k", client=client).complete("hi")
    assert excinfo.value.category is ErrorCategory.TIMEOUT
    await client.aclose()

    async def _timeout2(*args: object, **kwargs: object) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    client2 = httpx.AsyncClient(transport=httpx.MockTransport(_timeout2))  # type: ignore[arg-type]
    with pytest.raises(ProviderError):
        await OpenRouterProvider(model_id="m", api_key="k", client=client2).complete("hi")
    await client2.aclose()

    async def _timeout3(*args: object, **kwargs: object) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    client3 = httpx.AsyncClient(transport=httpx.MockTransport(_timeout3))  # type: ignore[arg-type]
    with pytest.raises(ProviderError) as excinfo3:
        await CloudflareProvider(
            model_id="m", api_key="k", account_id="a", client=client3
        ).complete("hi")
    assert excinfo3.value.category is ErrorCategory.TIMEOUT
    await client3.aclose()


@respx.mock
async def test_provider_invalid_json_body() -> None:
    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(200, text="not json {{{")
    )
    with pytest.raises(ProviderError) as excinfo:
        await GroqProvider(model_id="m", api_key="k").complete("hi")
    assert excinfo.value.category is ErrorCategory.INVALID_REQUEST

    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(200, text="not json {{{")
    )
    with pytest.raises(ProviderError):
        await OpenRouterProvider(model_id="m", api_key="k").complete("hi")

    run = "https://api.cloudflare.com/client/v4/accounts/a/ai/run/m"
    respx.post(run).mock(return_value=httpx.Response(200, text="not json {{{"))
    with pytest.raises(ProviderError):
        await CloudflareProvider(model_id="m", api_key="k", account_id="a").complete("hi")


@respx.mock
async def test_health_check_transport_failure_returns_false() -> None:
    respx.get("https://api.groq.com/openai/v1/models").mock(side_effect=RuntimeError("x"))
    assert await GroqProvider(model_id="m", api_key="k").health_check() is False
    respx.get("https://openrouter.ai/api/v1/models").mock(side_effect=RuntimeError("x"))
    assert await OpenRouterProvider(model_id="m", api_key="k").health_check() is False
    respx.get("https://api.cloudflare.com/client/v4/accounts/a/ai/models").mock(
        side_effect=RuntimeError("x")
    )
    assert (
        await CloudflareProvider(model_id="m", api_key="k", account_id="a").health_check() is False
    )
