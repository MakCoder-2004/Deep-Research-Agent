"""Application entrypoint for the Deep Research Agent (Milestone 1 foundation)."""

from __future__ import annotations

import sys

from research_agent.config import Settings, validate_at_startup
from research_agent.observability.logging import configure_logging


def _collect_secrets(settings: Settings) -> list[str]:
    candidates = [
        settings.telegram_bot_token.get_secret_value(),
        settings.groq_api_key.get_secret_value(),
        settings.openrouter_api_key.get_secret_value(),
        settings.cloudflare_api_key.get_secret_value(),
        settings.tavily_api_key.get_secret_value(),
        settings.brave_api_key.get_secret_value(),
        settings.langsmith_api_key.get_secret_value(),
    ]
    return [secret for secret in candidates if secret]


def main() -> None:
    """Validate configuration and start the service stub."""
    try:
        settings = validate_at_startup(Settings)
    except Exception as exc:  # noqa: BLE001 - surface startup errors clearly
        print("Configuration error: startup validation failed.", file=sys.stderr)
        raise SystemExit(1) from exc
    configure_logging(secrets=_collect_secrets(settings))
    print(f"Deep Research Agent ready (environment={settings.runtime_environment}).")


if __name__ == "__main__":
    main()
