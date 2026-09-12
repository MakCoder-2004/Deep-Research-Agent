"""OpenRouter fallback adapter (M3.9). Model comes from config."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
from pydantic import SecretStr, ValidationError

from research_agent.errors import ErrorCategory
from research_agent.llm.base import (
    ProviderCapability,
    ProviderError,
    ProviderMetadata,
    ResponseT,
)
from research_agent.llm.providers._common import (
    category_for_status,
    map_transport_error,
    parse_json_object,
    read_choice_text,
)

__all__ = ["OpenRouterProvider"]

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_CAPABILITIES: frozenset[ProviderCapability] = frozenset(
    {
        ProviderCapability.FAST_MULTILINGUAL,
        ProviderCapability.LONG_CONTEXT,
        ProviderCapability.REASONING,
        ProviderCapability.STRUCTURED_OUTPUT,
        ProviderCapability.ARABIC_CAPABLE,
        ProviderCapability.TOOL_CALLING,
    }
)


class OpenRouterProvider:
    """Async OpenRouter chat adapter honoring deadlines and normalized errors."""

    def __init__(
        self,
        *,
        model_id: str,
        api_key: str | SecretStr,
        timeout_seconds: float = 30.0,
        base_url: str = _DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
        capabilities: frozenset[ProviderCapability] | None = None,
        priority: int = 1,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")
        key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        self._api_key = SecretStr(key)
        self._model_id = model_id
        self._timeout_seconds = timeout_seconds
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._owned = client is None
        self._metadata = ProviderMetadata(
            name="openrouter",
            model_id=model_id,
            capabilities=capabilities if capabilities is not None else _DEFAULT_CAPABILITIES,
            priority=priority,
        )

    @property
    def metadata(self) -> ProviderMetadata:
        return self._metadata

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        timeout = httpx.Timeout(
            connect=5.0,
            read=self._timeout_seconds,
            write=self._timeout_seconds,
            pool=5.0,
        )
        created = httpx.AsyncClient(timeout=timeout)
        self._client = created
        return created

    async def aclose(self) -> None:
        if self._owned and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }

    def _payload(self, prompt: str, *, json_mode: bool) -> dict[str, object]:
        body: dict[str, object] = {
            "model": self._model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    async def _post(self, prompt: str, *, json_mode: bool) -> str:
        if not prompt.strip():
            raise ProviderError(
                "Prompt must not be empty.",
                category=ErrorCategory.INVALID_REQUEST,
                provider="openrouter",
            )
        if not self._api_key.get_secret_value():
            raise ProviderError(
                "OpenRouter API key is not configured.",
                category=ErrorCategory.AUTH,
                provider="openrouter",
            )
        if not self._model_id:
            raise ProviderError(
                "OpenRouter model is not configured.",
                category=ErrorCategory.INVALID_REQUEST,
                provider="openrouter",
            )
        url = f"{self._base_url}/chat/completions"
        try:
            async with asyncio.timeout(self._timeout_seconds):
                response = await self._get_client().post(
                    url,
                    headers=self._headers(),
                    json=self._payload(prompt, json_mode=json_mode),
                )
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            raise ProviderError(
                "OpenRouter request timed out.",
                category=ErrorCategory.TIMEOUT,
                provider="openrouter",
            ) from exc
        except Exception as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise ProviderError(
                "OpenRouter request failed.",
                category=map_transport_error(exc),
                provider="openrouter",
            ) from exc
        if response.status_code != 200:
            raise ProviderError(
                "OpenRouter request failed.",
                category=category_for_status(response.status_code, response.text),
                provider="openrouter",
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(
                "OpenRouter returned invalid JSON.",
                category=ErrorCategory.INVALID_REQUEST,
                provider="openrouter",
            ) from exc
        text = read_choice_text(data)
        if not text.strip():
            raise ProviderError(
                "OpenRouter returned an empty completion.",
                category=ErrorCategory.INVALID_REQUEST,
                provider="openrouter",
            )
        return text

    async def complete(self, prompt: str) -> str:
        return await self._post(prompt, json_mode=False)

    async def structured(self, prompt: str, response_model: type[ResponseT]) -> ResponseT:
        guided = (
            f"{prompt}\n\nReturn ONLY a valid JSON object matching this schema:\n"
            f"{response_model.model_json_schema()}"
        )
        text = await self._post(guided, json_mode=True)
        try:
            obj = parse_json_object(text)
            return response_model.model_validate(obj)
        except (ValueError, ValidationError, json.JSONDecodeError) as exc:
            raise ProviderError(
                "OpenRouter returned invalid structured output.",
                category=ErrorCategory.INVALID_REQUEST,
                provider="openrouter",
            ) from exc

    async def health_check(self) -> bool:
        if not self._api_key.get_secret_value() or not self._model_id:
            return False
        url = f"{self._base_url}/models"
        try:
            async with asyncio.timeout(min(self._timeout_seconds, 10.0)):
                response = await self._get_client().get(url, headers=self._headers())
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        return response.status_code == 200
