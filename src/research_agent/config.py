"""Runtime configuration (placeholder expanded in M1 config step)."""

from __future__ import annotations


class Settings:  # minimal placeholder so main.py imports on bootstrap commit
    def __init__(self) -> None:
        self.runtime_environment = "development"


def validate_at_startup(settings_cls: type[Settings] = Settings) -> Settings:
    return settings_cls()
