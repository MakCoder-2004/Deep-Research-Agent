"""Logging configuration (placeholder expanded in persistence/logging step)."""

from __future__ import annotations

import logging


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(level=level, format="%(message)s")
