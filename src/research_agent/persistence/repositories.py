"""Async repositories for foundation persistence and retention."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import aiosqlite


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp, accepting a trailing 'Z' variant.

    Returns None when the value is not a parseable timestamp; callers treat
    unparseable expiries as already expired (fail closed).
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _is_expired(expires_at: str, now: datetime | None = None) -> bool:
    current = now or datetime.now(UTC)
    expires = _parse_iso(expires_at)
    return expires is None or expires <= current


class UserRepository:
    async def upsert(self, conn: aiosqlite.Connection, user_id: int, language: str = "en") -> None:
        now = _now_iso()
        await conn.execute(
            """
            INSERT INTO users (user_id, language, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                language=excluded.language,
                updated_at=excluded.updated_at
            """,
            (user_id, language, now, now),
        )

    async def get(self, conn: aiosqlite.Connection, user_id: int) -> aiosqlite.Row | None:
        cursor = await conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return await cursor.fetchone()


class SessionRepository:
    async def save(
        self,
        conn: aiosqlite.Connection,
        user_id: int,
        language: str,
        interactions: list[str],
        max_interactions: int = 6,
    ) -> None:
        trimmed = interactions[-max_interactions:]
        now = _now_iso()
        await conn.execute(
            """
            INSERT INTO sessions (user_id, language, interactions, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                language=excluded.language,
                interactions=excluded.interactions,
                updated_at=excluded.updated_at
            """,
            (user_id, language, json.dumps(trimmed), now),
        )

    async def get(self, conn: aiosqlite.Connection, user_id: int) -> aiosqlite.Row | None:
        cursor = await conn.execute("SELECT * FROM sessions WHERE user_id = ?", (user_id,))
        return await cursor.fetchone()

    async def delete(self, conn: aiosqlite.Connection, user_id: int) -> None:
        await conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    async def prune(
        self,
        conn: aiosqlite.Connection,
        *,
        now: datetime | None = None,
        ttl_hours: int = 24,
        max_interactions: int = 6,
    ) -> None:
        current = now or datetime.now(UTC)
        cutoff = (current - timedelta(hours=ttl_hours)).isoformat()
        await conn.execute("DELETE FROM sessions WHERE updated_at < ?", (cutoff,))
        cursor = await conn.execute("SELECT user_id, interactions FROM sessions")
        rows = await cursor.fetchall()
        for row in rows:
            interactions: list[str] = json.loads(row["interactions"])
            if len(interactions) > max_interactions:
                trimmed = interactions[-max_interactions:]
                await conn.execute(
                    "UPDATE sessions SET interactions = ? WHERE user_id = ?",
                    (json.dumps(trimmed), row["user_id"]),
                )


class JobRepository:
    async def create(
        self,
        conn: aiosqlite.Connection,
        *,
        job_id: str,
        user_id: int,
        query: str,
        language: str = "mixed",
        domain: str = "general",
        depth: str = "standard",
        risk_level: str = "normal",
        state: str = "queued",
    ) -> None:
        now = _now_iso()
        await conn.execute(
            """
            INSERT INTO jobs (job_id, user_id, query, language, domain, depth,
                              risk_level, state, repair_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (job_id, user_id, query, language, domain, depth, risk_level, state, now, now),
        )

    async def update_state(
        self,
        conn: aiosqlite.Connection,
        job_id: str,
        state: str,
        *,
        error: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        await conn.execute(
            "UPDATE jobs SET state = ?, error = ?, trace_id = ?, updated_at = ? WHERE job_id = ?",
            (state, error, trace_id, _now_iso(), job_id),
        )

    async def get(self, conn: aiosqlite.Connection, job_id: str) -> aiosqlite.Row | None:
        cursor = await conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        return await cursor.fetchone()


class ReportRepository:
    async def save(
        self,
        conn: aiosqlite.Connection,
        *,
        report_id: str,
        job_id: str,
        topic: str,
        summary: str,
        markdown_path: str,
        tools_used: list[str],
    ) -> None:
        now = _now_iso()
        await conn.execute(
            """
            INSERT INTO reports
                (report_id, job_id, topic, summary, markdown_path, tools_used, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (report_id, job_id, topic, summary, markdown_path, json.dumps(tools_used), now),
        )
        await conn.execute(
            "INSERT INTO report_fts (report_id, topic, summary) VALUES (?, ?, ?)",
            (report_id, topic, summary),
        )

    async def get(self, conn: aiosqlite.Connection, report_id: str) -> aiosqlite.Row | None:
        cursor = await conn.execute("SELECT * FROM reports WHERE report_id = ?", (report_id,))
        return await cursor.fetchone()

    async def prune(
        self,
        conn: aiosqlite.Connection,
        *,
        retention_days: int = 90,
        now: datetime | None = None,
    ) -> int:
        current = now or datetime.now(UTC)
        cutoff = (current - timedelta(days=retention_days)).isoformat()
        cursor = await conn.execute("SELECT report_id FROM reports WHERE created_at < ?", (cutoff,))
        expired = [row["report_id"] for row in await cursor.fetchall()]
        for report_id in expired:
            await conn.execute("DELETE FROM sources WHERE report_id = ?", (report_id,))
            await conn.execute("DELETE FROM report_fts WHERE report_id = ?", (report_id,))
            await conn.execute("DELETE FROM reports WHERE report_id = ?", (report_id,))
        return len(expired)


class SourceRepository:
    async def save_many(
        self, conn: aiosqlite.Connection, report_id: str, sources: list[dict[str, str | int | None]]
    ) -> None:
        for source in sources:
            await conn.execute(
                """
                INSERT INTO sources (report_id, source_ref, title, url, publisher,
                                     published_at, accessed_at, source_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    source.get("source_ref"),
                    source.get("title", ""),
                    source.get("url", ""),
                    source.get("publisher"),
                    source.get("published_at"),
                    source.get("accessed_at", _now_iso()),
                    source.get("source_type", "web"),
                ),
            )

    async def list_by_report(
        self, conn: aiosqlite.Connection, report_id: str
    ) -> list[aiosqlite.Row]:
        cursor = await conn.execute(
            "SELECT * FROM sources WHERE report_id = ? ORDER BY source_ref", (report_id,)
        )
        return list(await cursor.fetchall())


class ToolRunRepository:
    async def record(
        self,
        conn: aiosqlite.Connection,
        *,
        job_id: str,
        tool_name: str,
        success: bool,
        duration_ms: int = 0,
        result_count: int = 0,
        error_category: str | None = None,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO tool_runs (job_id, tool_name, success, duration_ms,
                                   result_count, error_category, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                tool_name,
                int(success),
                duration_ms,
                result_count,
                error_category,
                _now_iso(),
            ),
        )

    async def list_by_job(self, conn: aiosqlite.Connection, job_id: str) -> list[aiosqlite.Row]:
        cursor = await conn.execute("SELECT * FROM tool_runs WHERE job_id = ?", (job_id,))
        return list(await cursor.fetchall())


class ProviderUsageRepository:
    async def record(
        self,
        conn: aiosqlite.Connection,
        *,
        job_id: str,
        provider: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: int = 0,
        cache_hit: bool = False,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO provider_usage (job_id, provider, model, input_tokens,
                                        output_tokens, latency_ms, cache_hit, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                provider,
                model,
                input_tokens,
                output_tokens,
                latency_ms,
                int(cache_hit),
                _now_iso(),
            ),
        )

    async def list_by_job(self, conn: aiosqlite.Connection, job_id: str) -> list[aiosqlite.Row]:
        cursor = await conn.execute("SELECT * FROM provider_usage WHERE job_id = ?", (job_id,))
        return list(await cursor.fetchall())


class CacheRepository:
    async def set(
        self, conn: aiosqlite.Connection, cache_key: str, payload: str, expires_at: str
    ) -> None:
        await conn.execute(
            """
            INSERT INTO cache (cache_key, payload, expires_at, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                payload=excluded.payload,
                expires_at=excluded.expires_at,
                created_at=excluded.created_at
            """,
            (cache_key, payload, expires_at, _now_iso()),
        )

    async def get(self, conn: aiosqlite.Connection, cache_key: str) -> str | None:
        cursor = await conn.execute(
            "SELECT payload, expires_at FROM cache WHERE cache_key = ?", (cache_key,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        if _is_expired(str(row["expires_at"])):
            return None
        return str(row["payload"])

    async def delete_expired(self, conn: aiosqlite.Connection) -> int:
        cursor = await conn.execute("SELECT cache_key, expires_at FROM cache")
        expired = [
            row["cache_key"] for row in await cursor.fetchall() if _is_expired(str(row["expires_at"]))
        ]
        for cache_key in expired:
            await conn.execute("DELETE FROM cache WHERE cache_key = ?", (cache_key,))
        return len(expired)
