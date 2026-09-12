"""Persistence tests for schema, WAL, repositories, retention (M1.38)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import pytest
from pydantic import ValidationError

from research_agent.errors import ErrorCategory
from research_agent.models import AttemptOutcome, Depth, Domain, JobState, Language, RiskLevel
from research_agent.models.reports import Source
from research_agent.persistence.database import connect, init_schema
from research_agent.persistence.repositories import (
    CacheRepository,
    JobRepository,
    ProviderUsageRepository,
    ReportRepository,
    SessionRepository,
    SourceRepository,
    ToolRunRepository,
    UserRepository,
)
from research_agent.tools.router import ToolAttempt


async def _memory_db() -> aiosqlite.Connection:
    conn = await connect(":memory:")
    await init_schema(conn)
    return conn


async def test_schema_creates_expected_tables() -> None:
    conn = await _memory_db()
    try:
        cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row["name"] for row in await cursor.fetchall()}
        for expected in (
            "users",
            "sessions",
            "jobs",
            "reports",
            "sources",
            "tool_runs",
            "provider_usage",
            "cache",
            "report_fts",
        ):
            assert expected in tables
    finally:
        await conn.close()


async def test_wal_mode_enabled_on_file_db(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = await connect(db_path)
    try:
        cursor = await conn.execute("PRAGMA journal_mode;")
        row = await cursor.fetchone()
        assert row is not None and str(row[0]).lower() == "wal"
    finally:
        await conn.close()


async def test_user_and_session_repositories() -> None:
    conn = await _memory_db()
    try:
        users = UserRepository()
        sessions = SessionRepository()
        await users.upsert(conn, 123, "ar")
        assert (await users.get(conn, 123)) is not None
        await conn.execute(
            "INSERT INTO users (user_id, language, created_at, updated_at) VALUES (1,'en','x','x')"
        )
        await sessions.save(conn, 1, "en", ["a", "b"])
        row = await sessions.get(conn, 1)
        assert row is not None
        assert json.loads(row["interactions"]) == ["a", "b"]
        await sessions.save(conn, 1, "en", [f"m{i}" for i in range(10)])
        row = await sessions.get(conn, 1)
        assert row is not None
        assert len(json.loads(row["interactions"])) == 6
        await sessions.delete(conn, 1)
        assert await sessions.get(conn, 1) is None
    finally:
        await conn.close()


async def test_session_retention_ttl() -> None:
    conn = await _memory_db()
    try:
        await conn.execute(
            "INSERT INTO users (user_id, language, created_at, updated_at) VALUES (7,'en','x','x')"
        )
        old = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        await conn.execute(
            "INSERT INTO sessions (user_id, language, interactions, updated_at)"
            " VALUES (7,'en','[]',?)",
            (old,),
        )
        await conn.commit()
        await SessionRepository().prune(conn, ttl_hours=24, max_interactions=6)
        await conn.commit()
        cursor = await conn.execute("SELECT * FROM sessions WHERE user_id = 7")
        assert await cursor.fetchone() is None
    finally:
        await conn.close()


async def test_job_report_source_repositories() -> None:
    conn = await _memory_db()
    try:
        jobs = JobRepository()
        reports = ReportRepository()
        sources = SourceRepository()
        await jobs.create(conn, job_id="j1", user_id=1, query="q")
        assert (await jobs.get(conn, "j1")) is not None
        await jobs.update_state(conn, "j1", "completed", trace_id="t1")
        row = await jobs.get(conn, "j1")
        assert row is not None and row["state"] == "completed"
        await reports.save(
            conn,
            report_id="r1",
            job_id="j1",
            topic="T",
            summary="S",
            markdown_path="data/reports/r1.md",
            tools_used=["tavily"],
        )
        assert (await reports.get(conn, "r1")) is not None
        await sources.save_many(
            conn,
            "r1",
            [
                {
                    "source_ref": 1,
                    "title": "A",
                    "url": "https://example.com/a",
                    "publisher": None,
                    "published_at": None,
                    "accessed_at": datetime.now(UTC).isoformat(),
                    "source_type": "web",
                }
            ],
        )
        assert len(await sources.list_by_report(conn, "r1")) == 1
    finally:
        await conn.close()


async def test_report_retention_configurable() -> None:
    conn = await _memory_db()
    try:
        # Parent job via the repository so the jobs FK holds; the report
        # itself is inserted raw with an aged timestamp like legacy rows.
        await JobRepository().create(conn, job_id="j-old", user_id=1, query="q")
        old = (datetime.now(UTC) - timedelta(days=100)).isoformat()
        await conn.execute(
            "INSERT INTO reports"
            " (report_id, job_id, topic, summary, markdown_path, tools_used, created_at)"
            " VALUES ('r-old', 'j-old', 'T', 'S', 'p', '[]', ?)",
            (old,),
        )
        await conn.commit()
        removed = await ReportRepository().prune(conn, retention_days=90)
        await conn.commit()
        assert removed == 1
        assert await ReportRepository().get(conn, "r-old") is None
    finally:
        await conn.close()


async def test_tool_provider_cache_repositories() -> None:
    conn = await _memory_db()
    try:
        tools = ToolRunRepository()
        usage = ProviderUsageRepository()
        cache = CacheRepository()
        # tool_runs/provider_usage reference jobs via FK: create the parent.
        await JobRepository().create(conn, job_id="j1", user_id=1, query="q")
        await tools.record(conn, job_id="j1", tool_name="tavily", success=True, result_count=3)
        assert len(await tools.list_by_job(conn, "j1")) == 1
        await usage.record(conn, job_id="j1", provider="groq", model="m1", input_tokens=10)
        assert len(await usage.list_by_job(conn, "j1")) == 1
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        await cache.set(conn, "k1", "v1", future)
        assert await cache.get(conn, "k1") == "v1"
        await cache.set(conn, "k2", "v2", past)
        assert await cache.get(conn, "k2") is None
        assert await cache.delete_expired(conn) >= 1
    finally:
        await conn.close()


async def test_tool_attempt_persistence_records_outcomes_metadata_and_order() -> None:
    conn = await _memory_db()
    try:
        await JobRepository().create(conn, job_id="attempt-job", user_id=1, query="q")
        repository = ToolRunRepository()
        await repository.record(
            conn,
            job_id="attempt-job",
            task_id="second",
            task_index=1,
            tool_name="tool-two",
            success=False,
            outcome=AttemptOutcome.TIMEOUT,
            error_category=ErrorCategory.TIMEOUT,
            duration_ms=12,
            result_count=0,
            http_status=429,
            retry_after=3.5,
        )
        await repository.record(
            conn,
            job_id="attempt-job",
            task_id="first",
            task_index=0,
            tool_name="tool-one",
            success=True,
            outcome=AttemptOutcome.SUCCESS,
            duration_ms=4,
            result_count=2,
        )
        await conn.commit()

        rows = await repository.list_by_job(conn, "attempt-job")
        assert [row["task_id"] for row in rows] == ["first", "second"]
        assert rows[1]["outcome"] == "timeout"
        assert rows[1]["http_status"] == 429
        assert rows[1]["retry_after"] == 3.5
    finally:
        await conn.close()


async def test_tool_attempt_and_repository_reject_invalid_measurements() -> None:
    with pytest.raises(ValidationError):
        ToolAttempt(tool_name="tool", success=True, result_count=-1)

    conn = await _memory_db()
    try:
        await JobRepository().create(conn, job_id="invalid-attempt", user_id=1, query="q")
        with pytest.raises(ValidationError):
            await ToolRunRepository().record(
                conn,
                job_id="invalid-attempt",
                tool_name="tool",
                success=True,
                duration_ms=-1,
            )
    finally:
        await conn.close()


async def test_legacy_tool_runs_gain_outcome_and_metadata_columns() -> None:
    conn = await connect(":memory:")
    try:
        await conn.execute(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                query TEXT NOT NULL,
                language TEXT NOT NULL,
                domain TEXT NOT NULL,
                depth TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                state TEXT NOT NULL,
                repair_count INTEGER NOT NULL,
                trace_id TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE tool_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                success INTEGER NOT NULL,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                result_count INTEGER NOT NULL DEFAULT 0,
                error_category TEXT,
                quota_metadata TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        await conn.execute(
            """
            INSERT INTO tool_runs
                (job_id, tool_name, success, error_category, created_at)
            VALUES ('legacy-job', 'legacy-tool', 0, 'timeout', '2026-01-01T00:00:00+00:00')
            """
        )
        await init_schema(conn)

        cursor = await conn.execute("SELECT outcome, cancelled FROM tool_runs")
        row = await cursor.fetchone()
        assert row is not None
        assert (row["outcome"], row["cancelled"]) == ("timeout", 0)
    finally:
        await conn.close()


async def test_cache_expiry_parses_offset_formats() -> None:
    conn = await _memory_db()
    try:
        cache = CacheRepository()
        # Future instant written with a -05:00 offset sorts *before* the current
        # UTC time as a raw string; only parsed comparison gets it right.
        future_utc = datetime.now(UTC) + timedelta(hours=1)
        minus_five = timezone(timedelta(hours=-5))
        offset_format = future_utc.astimezone(minus_five).strftime("%Y-%m-%dT%H:%M:%S%z")
        assert offset_format < datetime.now(UTC).isoformat()  # guard: discriminating case
        await cache.set(conn, "offset", "v", offset_format)
        assert await cache.get(conn, "offset") == "v"
        await cache.set(conn, "garbage", "v", "not-a-timestamp")
        assert await cache.get(conn, "garbage") is None
    finally:
        await conn.close()


async def test_report_fts_search_and_delete_trigger() -> None:
    conn = await _memory_db()
    try:
        jobs = JobRepository()
        reports = ReportRepository()
        await jobs.create(conn, job_id="j-fts", user_id=1, query="q")
        await reports.save(
            conn,
            report_id="r-fts",
            job_id="j-fts",
            topic="Quantum batteries",
            summary="Solid-state energy storage review",
            markdown_path="data/reports/r-fts.md",
            tools_used=["tavily"],
        )
        await conn.commit()
        hits = await reports.search(conn, "batteries")
        assert [row["report_id"] for row in hits] == ["r-fts"]
        assert await reports.search(conn, "unrelatedzebra") == []
        # Direct deletes (e.g. ON DELETE CASCADE from jobs) must not orphan FTS rows.
        await conn.execute("DELETE FROM reports WHERE report_id = 'r-fts'")
        await conn.commit()
        cursor = await conn.execute("SELECT * FROM report_fts WHERE report_id = 'r-fts'")
        assert await cursor.fetchall() == []
    finally:
        await conn.close()


async def test_job_create_accepts_enums_and_source_models() -> None:
    conn = await _memory_db()
    try:
        jobs = JobRepository()
        reports = ReportRepository()
        sources = SourceRepository()
        await jobs.create(
            conn,
            job_id="j-enum",
            user_id=1,
            query="q",
            language=Language.ARABIC,
            domain=Domain.ACADEMIC,
            depth=Depth.DEEP,
            risk_level=RiskLevel.HIGH_STAKES,
            state=JobState.ACTIVE,
        )
        row = await jobs.get(conn, "j-enum")
        assert row is not None
        assert (row["language"], row["domain"], row["depth"]) == ("ar", "academic", "deep")
        assert (row["risk_level"], row["state"]) == ("high_stakes", "active")
        await reports.save(
            conn,
            report_id="r-enum",
            job_id="j-enum",
            topic="T",
            summary="S",
            markdown_path="p",
            tools_used=[],
        )
        await sources.save_many(
            conn,
            "r-enum",
            [
                Source(
                    id=1,
                    title="A",
                    url="https://example.com/a",  # type: ignore[arg-type]
                    accessed_at=datetime.now(UTC),
                )
            ],
        )
        rows = await sources.list_by_report(conn, "r-enum")
        assert len(rows) == 1 and rows[0]["url"] == "https://example.com/a"
    finally:
        await conn.close()
