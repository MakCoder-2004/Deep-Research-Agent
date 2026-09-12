"""Shared HTTP helpers for LLM adapters (M3.9). No model names live here."""

from __future__ import annotations

import json
from typing import Any

import httpx

from research_agent.errors import ErrorCategory

__all__ = [
    "category_for_status",
    "contains_model_id",
    "extract_json_payload",
    "map_transport_error",
    "parse_json_object",
    "read_choice_text",
]

_CONTENT_FILTER_MARKERS: tuple[str, ...] = (
    "content_filter",
    "content-filter",
    "moderation",
    "blocked_by_filter",
    "safety_filter",
)


def category_for_status(status: int, body: str) -> ErrorCategory:
    lowered = body.lower()
    if any(marker in lowered for marker in _CONTENT_FILTER_MARKERS):
        return ErrorCategory.CONTENT_FILTERED
    if status in (401, 403):
        return ErrorCategory.AUTH
    if status == 429:
        return ErrorCategory.RATE_LIMITED
    if status == 408:
        return ErrorCategory.TIMEOUT
    if status in (400, 404, 422):
        return ErrorCategory.INVALID_REQUEST
    if status == 503:
        return ErrorCategory.UNAVAILABLE
    if 500 <= status <= 599:
        return ErrorCategory.TRANSIENT
    if 400 <= status <= 499:
        return ErrorCategory.INVALID_REQUEST
    return ErrorCategory.UNKNOWN


def map_transport_error(exc: Exception) -> ErrorCategory:
    if isinstance(exc, httpx.TimeoutException):
        return ErrorCategory.TIMEOUT
    if isinstance(exc, httpx.ConnectError):
        return ErrorCategory.UNAVAILABLE
    if isinstance(exc, httpx.NetworkError):
        return ErrorCategory.TRANSIENT
    if isinstance(exc, httpx.HTTPError):
        return ErrorCategory.TRANSIENT
    return ErrorCategory.UNKNOWN


def contains_model_id(data: object, expected: str) -> bool:
    """Return whether a provider model catalog contains the exact model ID."""
    if not expected:
        return False
    if isinstance(data, dict):
        for key in ("id", "name", "model", "model_id"):
            value = data.get(key)
            if isinstance(value, str) and value == expected:
                return True
        return any(contains_model_id(value, expected) for value in data.values())
    if isinstance(data, list):
        return any(contains_model_id(value, expected) for value in data)
    return False


def extract_json_payload(text: str) -> str:
    """Strip fences/prose around a JSON object so structured parsing is robust."""
    cleaned = text.strip()
    if "```" in cleaned:
        parts = cleaned.split("```")
        for part in parts:
            candidate = part.strip()
            if candidate.lower().startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{") and candidate.endswith("}"):
                return candidate
        # Fall through to brace search when fence handling finds nothing.
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        return cleaned[start : end + 1].strip()
    return cleaned


def parse_json_object(text: str) -> dict[str, Any]:
    payload = extract_json_payload(text)
    decoded: Any = json.loads(payload)
    if not isinstance(decoded, dict):
        raise ValueError("LLM structured response must be a JSON object.")
    return decoded


def read_choice_text(data: object) -> str:
    """Extract chat completion text from an OpenAI-compatible payload."""
    if not isinstance(data, dict):
        return ""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""
