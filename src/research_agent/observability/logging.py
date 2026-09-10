"""Structured JSON logging with correlation and job identifiers."""

from __future__ import annotations

import contextvars
import json
import logging
from datetime import UTC, datetime
from typing import Any

from research_agent.observability.redaction import redact_text

_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="")
_job_id: contextvars.ContextVar[str] = contextvars.ContextVar("job_id", default="")


def set_correlation_id(value: str) -> None:
    _correlation_id.set(value)


def set_job_id(value: str) -> None:
    _job_id.set(value)


def clear_context() -> None:
    _correlation_id.set("")
    _job_id.set("")


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON with redaction."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(message),
            "correlation_id": getattr(record, "correlation_id", None)
            or _correlation_id.get()
            or None,
            "job_id": getattr(record, "job_id", None) or _job_id.get() or None,
        }
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> None:
    """Configure the root logger with JSON formatting (idempotent)."""
    root = logging.getLogger()
    root.setLevel(level)
    for handler in root.handlers:
        if isinstance(handler.formatter, JsonFormatter):
            handler.setLevel(level)
            return
    handler = logging.StreamHandler()
    handler.setLevel(level)
    handler.setFormatter(JsonFormatter())
    root.handlers.clear()
    root.addHandler(handler)
