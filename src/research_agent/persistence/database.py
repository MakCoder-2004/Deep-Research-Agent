"""Async SQLite connection management with WAL mode and schema init."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

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
    updated_at TEXT NOT NULL,
    deadline_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_user_state ON jobs(user_id, state);

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
    if str(path) != ":memory:" and not str(path).startswith("file:") and path.parent != Path("."):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OSError(f"Cannot create database directory {path.parent}: {exc}") from exc
        if path.is_dir():  # noqa: ASYNC240 - startup path check, tiny and rare
            raise OSError(f"Database path {path} is a directory, not a file.")
    conn = await aiosqlite.connect(str(path))
    try:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA foreign_keys=ON;")
        await conn.execute("PRAGMA busy_timeout=5000;")
    except Exception:
        try:
            await conn.close()
        except Exception:  # noqa: S110, BLE001 - original error takes precedence
            logger.debug("db connection close failed after connect error")
        raise
    return conn


async def init_schema(conn: aiosqlite.Connection) -> None:
    """Create the schema and apply idempotent job-table migrations.

    The active-job index is deliberately created after duplicate reconciliation.
    Older databases may have been written before that invariant existed.
    """
    await conn.executescript(SCHEMA_SQL)
    if await _job_schema_is_current(conn):
        return
    await conn.execute("BEGIN IMMEDIATE")
    try:
        await _ensure_deadline_column(conn)
        await _reconcile_duplicate_active_jobs(conn)
        await _ensure_active_job_index(conn)
    except BaseException:
        await conn.rollback()
        raise
    await conn.commit()


async def _job_schema_is_current(conn: aiosqlite.Connection) -> bool:
    """Avoid taking a write lock when the job migrations are already applied."""
    cursor = await conn.execute("PRAGMA table_info('jobs')")
    columns = {str(row["name"]) for row in await cursor.fetchall()}
    if "deadline_at" not in columns:
        return False
    cursor = await conn.execute("PRAGMA index_list('jobs')")
    for row in await cursor.fetchall():
        if str(row["name"]) != "ux_jobs_one_active_per_user":
            continue
        if int(row["unique"]) != 1 or int(row["partial"]) != 1:
            return False
        sql_cursor = await conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (row["name"],),
        )
        sql_row = await sql_cursor.fetchone()
        if sql_row is None or sql_row["sql"] is None:
            return False
        normalized_sql = " ".join(str(sql_row["sql"]).lower().split())
        if (
            "on jobs(user_id)" in normalized_sql
            and "where state in ('queued', 'active')" in normalized_sql
        ):
            return True
    return False


async def _ensure_deadline_column(conn: aiosqlite.Connection) -> None:
    """Add the enqueue deadline column to databases created by older versions."""
    cursor = await conn.execute("PRAGMA table_info('jobs')")
    columns = {str(row["name"]) for row in await cursor.fetchall()}
    if "deadline_at" not in columns:
        await conn.execute("ALTER TABLE jobs ADD COLUMN deadline_at TEXT")


async def _reconcile_duplicate_active_jobs(conn: aiosqlite.Connection) -> None:
    """Keep the oldest live job per user before installing the unique index."""
    cursor = await conn.execute(
        """
        SELECT rowid, job_id, user_id, state
        FROM jobs
        WHERE state IN ('queued', 'active')
        ORDER BY user_id ASC, created_at ASC, rowid ASC
        """
    )
    rows = await cursor.fetchall()
    seen_users: set[int] = set()
    now = datetime.now(UTC).isoformat()
    for row in rows:
        user_id = int(row["user_id"])
        if user_id not in seen_users:
            seen_users.add(user_id)
            continue
        current_state = str(row["state"])
        replacement_state = "cancelled" if current_state == "queued" else "failed"
        reason = (
            "job cancelled during active-job index migration"
            if replacement_state == "cancelled"
            else "job failed during active-job index migration"
        )
        await conn.execute(
            """
            UPDATE jobs
            SET state = ?, error = ?, updated_at = ?
            WHERE rowid = ? AND state = ?
            """,
            (replacement_state, reason, now, row["rowid"], current_state),
        )


async def _ensure_active_job_index(conn: aiosqlite.Connection) -> None:
    """Create the one-live-job index, replacing a malformed same-name index."""
    cursor = await conn.execute("PRAGMA index_list('jobs')")
    indexes = await cursor.fetchall()
    current = next(
        (row for row in indexes if str(row["name"]) == "ux_jobs_one_active_per_user"),
        None,
    )
    if current is not None and int(current["unique"]) == 1 and int(current["partial"]) == 1:
        sql_cursor = await conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (current["name"],),
        )
        sql_row = await sql_cursor.fetchone()
        if sql_row is not None and sql_row["sql"] is not None:
            normalized_sql = " ".join(str(sql_row["sql"]).lower().split())
            if (
                "on jobs(user_id)" in normalized_sql
                and "where state in ('queued', 'active')" in normalized_sql
            ):
                return
    if current is not None:
        await conn.execute("DROP INDEX ux_jobs_one_active_per_user")
    await conn.execute(
        """
        CREATE UNIQUE INDEX ux_jobs_one_active_per_user
        ON jobs(user_id) WHERE state IN ('queued', 'active')
        """
    )


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
