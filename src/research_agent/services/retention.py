"""Best-effort retention sweeps for sessions, reports, and cache entries."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from research_agent.config import Settings
from research_agent.persistence.repositories import (
    CacheRepository,
    ReportRepository,
    SessionRepository,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetentionPolicy:
    """Retention limits wired from :class:`Settings` (PLAN §16)."""

    session_ttl_hours: int = 24
    session_max_interactions: int = 6
    report_retention_days: int = 90

    @classmethod
    def from_settings(cls, settings: Settings) -> RetentionPolicy:
        """Build a policy from typed settings."""
        return cls(
            session_ttl_hours=int(settings.session_ttl_hours),
            session_max_interactions=int(settings.session_max_interactions),
            report_retention_days=int(settings.report_retention_days),
        )


async def run_retention(
    conn: aiosqlite.Connection,
    policy: RetentionPolicy | None = None,
    reports_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Prune expired sessions, reports, and cache entries; never raises.

    Returns per-store counts (or error strings) for observability. Callers
    commit (``open_db`` commits on exit; injected connections commit
    explicitly) so the sweep stays best-effort and atomicity stays with them.
    """
    active = policy or RetentionPolicy()
    outcome: dict[str, Any] = {"sessions": 0, "reports": 0, "cache": 0}
    try:
        await SessionRepository().prune(
            conn,
            ttl_hours=active.session_ttl_hours,
            max_interactions=active.session_max_interactions,
        )
        outcome["sessions"] = "ok"
    except Exception as exc:  # noqa: BLE001 - retention must not fail requests
        logger.debug("session retention sweep failed: %s", type(exc).__name__)
        outcome["sessions"] = f"error: {type(exc).__name__}"
    try:
        outcome["reports"] = await ReportRepository().prune(
            conn,
            retention_days=active.report_retention_days,
            reports_dir=reports_dir,
        )
    except Exception as exc:  # noqa: BLE001 - retention must not fail requests
        logger.debug("report retention sweep failed: %s", type(exc).__name__)
        outcome["reports"] = f"error: {type(exc).__name__}"
    try:
        outcome["cache"] = await CacheRepository().delete_expired(conn)
    except Exception as exc:  # noqa: BLE001 - retention must not fail requests
        logger.debug("cache retention sweep failed: %s", type(exc).__name__)
        outcome["cache"] = f"error: {type(exc).__name__}"
    return outcome
