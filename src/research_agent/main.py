"""Application entrypoint for the Deep Research Agent (Milestone 1 foundation)."""

from __future__ import annotations

import sys

from research_agent.config import Settings, validate_at_startup
from research_agent.observability.logging import configure_logging


def main() -> None:
    """Validate configuration and start the service stub."""
    try:
        settings = validate_at_startup(Settings)
    except Exception as exc:  # noqa: BLE001 - surface startup errors clearly
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    configure_logging()
    print(f"Deep Research Agent ready (environment={settings.runtime_environment}).")


if __name__ == "__main__":
    main()
