"""Async SQLite connection management with WAL mode and schema init."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    language TEXT NOT NULL DEFAULT 'en',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    user_id INTEGER PRIMARY KEY,
    language TEXT NOT NULL DEFAULT 'en',
    interactions TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    query TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'mixed',
    domain TEXT NOT NULL DEFAULT 'general',
    depth TEXT NOT NULL DEFAULT 'standard',
    risk_level TEXT NOT NULL DEFAULT 'normal',
    state TEXT NOT NULL DEFAULT 'queued',
    repair_count INTEGER NOT NULL DEFAULT 0,
    trace_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_user_state ON jobs(user_id, state);
CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_one_active_per_user
    ON jobs(user_id) WHERE state IN ('queued', 'active');

CREATE TABLE IF NOT EXISTS reports (
    report_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    summary TEXT NOT NULL,
    markdown_path TEXT NOT NULL,
    tools_used TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id TEXT NOT NULL,
    source_ref INTEGER NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    publisher TEXT,
    published_at TEXT,
    accessed_at TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'web',
    FOREIGN KEY (report_id) REFERENCES reports(report_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sources_report ON sources(report_id);

CREATE TABLE IF NOT EXISTS tool_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    success INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    result_count INTEGER NOT NULL DEFAULT 0,
    error_category TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_runs_job ON tool_runs(job_id);

CREATE TABLE IF NOT EXISTS provider_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    cache_hit INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_provider_usage_job ON provider_usage(job_id);

CREATE TABLE IF NOT EXISTS cache (
    cache_key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS report_fts USING fts5(report_id, topic, summary);

-- Keep the FTS index in sync when reports disappear through paths that bypass
-- ReportRepository.prune (e.g. ON DELETE CASCADE from jobs).
CREATE TRIGGER IF NOT EXISTS trg_reports_fts_delete AFTER DELETE ON reports
BEGIN
    DELETE FROM report_fts WHERE report_id = OLD.report_id;
END;
"""


async def connect(db_path: Path | str) -> aiosqlite.Connection:
    """Open a SQLite connection with WAL mode and foreign keys enabled."""
    path = Path(db_path)
    if str(path) != ":memory:" and path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(path))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL;")
    await conn.execute("PRAGMA foreign_keys=ON;")
    await conn.execute("PRAGMA busy_timeout=5000;")
    return conn


async def init_schema(conn: aiosqlite.Connection) -> None:
    """Create all foundation tables and the FTS5 index."""
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()


@asynccontextmanager
async def open_db(db_path: Path | str) -> AsyncIterator[aiosqlite.Connection]:
    """Context manager yielding an initialized WAL connection."""
    conn = await connect(db_path)
    try:
        await init_schema(conn)
        yield conn
        await conn.commit()
    finally:
        await conn.close()
