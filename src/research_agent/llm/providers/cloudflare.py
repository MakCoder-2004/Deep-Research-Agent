"""Cloudflare Workers AI fallback adapter (M3.9). Model comes from config."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

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
)

__all__ = ["CloudflareProvider"]

logger = logging.getLogger(__name__)

_DEFAULT_CAPABILITIES: frozenset[ProviderCapability] = frozenset(
    {
        ProviderCapability.FAST_MULTILINGUAL,
        ProviderCapability.ARABIC_CAPABLE,
        ProviderCapability.STRUCTURED_OUTPUT,
    }
)


class CloudflareProvider:
    """Async Cloudflare Workers AI adapter with deadline and error mapping."""

    def __init__(
        self,
        *,
        model_id: str,
        api_key: str | SecretStr,
        account_id: str | SecretStr,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
        capabilities: frozenset[ProviderCapability] | None = None,
        priority: int = 2,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")
        key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        account = account_id.get_secret_value() if isinstance(account_id, SecretStr) else account_id
        self._api_key = SecretStr(key)
        self._account_id = SecretStr(account)
        self._model_id = model_id
        self._timeout_seconds = timeout_seconds
        self._client = client
        self._owned = client is None
        self._metadata = ProviderMetadata(
            name="cloudflare",
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

    def _run_url(self) -> str:
        account = self._account_id.get_secret_value()
        return f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{self._model_id}"

    def _models_url(self) -> str:
        account = self._account_id.get_secret_value()
        return f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/models"

    def _payload(self, prompt: str) -> dict[str, object]:
        return {"messages": [{"role": "user", "content": prompt}]}

    async def _post(self, prompt: str) -> str:
        if not prompt.strip():
            raise ProviderError(
                "Prompt must not be empty.",
                category=ErrorCategory.INVALID_REQUEST,
                provider="cloudflare",
            )
        if not self._api_key.get_secret_value():
            raise ProviderError(
                "Cloudflare API key is not configured.",
                category=ErrorCategory.AUTH,
                provider="cloudflare",
            )
        if not self._account_id.get_secret_value():
            raise ProviderError(
                "Cloudflare account is not configured.",
                category=ErrorCategory.AUTH,
                provider="cloudflare",
            )
        if not self._model_id:
            raise ProviderError(
                "Cloudflare model is not configured.",
                category=ErrorCategory.INVALID_REQUEST,
                provider="cloudflare",
            )
        try:
            async with asyncio.timeout(self._timeout_seconds):
                response = await self._get_client().post(
                    self._run_url(),
                    headers=self._headers(),
                    json=self._payload(prompt),
                )
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            raise ProviderError(
                "Cloudflare request timed out.",
                category=ErrorCategory.TIMEOUT,
                provider="cloudflare",
            ) from exc
        except Exception as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise ProviderError(
                "Cloudflare request failed.",
                category=map_transport_error(exc),
                provider="cloudflare",
            ) from exc
        if response.status_code != 200:
            raise ProviderError(
                "Cloudflare request failed.",
                category=category_for_status(response.status_code, response.text),
                provider="cloudflare",
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(
                "Cloudflare returned invalid JSON.",
                category=ErrorCategory.INVALID_REQUEST,
                provider="cloudflare",
            ) from exc
        text = _read_result_text(data)
        if not text.strip():
            raise ProviderError(
                "Cloudflare returned an empty completion.",
                category=ErrorCategory.INVALID_REQUEST,
                provider="cloudflare",
            )
        return text

    async def complete(self, prompt: str) -> str:
        return await self._post(prompt)

    async def structured(self, prompt: str, response_model: type[ResponseT]) -> ResponseT:
        guided = (
            f"{prompt}\n\nReturn ONLY a valid JSON object matching this schema:\n"
            f"{response_model.model_json_schema()}"
        )
        text = await self._post(guided)
        try:
            obj = parse_json_object(text)
            return response_model.model_validate(obj)
        except (ValueError, ValidationError, json.JSONDecodeError) as exc:
            raise ProviderError(
                "Cloudflare returned invalid structured output.",
                category=ErrorCategory.INVALID_REQUEST,
                provider="cloudflare",
            ) from exc

    async def health_check(self) -> bool:
        if (
            not self._api_key.get_secret_value()
            or not self._account_id.get_secret_value()
            or not self._model_id
        ):
            return False
        try:
            async with asyncio.timeout(min(self._timeout_seconds, 10.0)):
                response = await self._get_client().get(self._models_url(), headers=self._headers())
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        return response.status_code == 200


def _read_result_text(data: object) -> str:
    if not isinstance(data, dict):
        return ""
    result: Any = data.get("result")
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("response", "output", "text", "answer"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value
        # Some responses nest one more level, e.g. {"result": {"result": "..."}}.
        for value in result.values():
            if isinstance(value, str) and value.strip():
                return value
            if isinstance(value, dict):
                nested = _read_result_text({"result": value})
                if nested.strip():
                    return nested
    if isinstance(result, list):
        parts: list[str] = []
        for item in result:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""
