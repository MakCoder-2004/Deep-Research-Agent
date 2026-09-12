"""Async repositories for foundation persistence and retention."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from research_agent.models import Depth, Domain, JobState, Language, RiskLevel
from research_agent.models.reports import Source

logger = logging.getLogger(__name__)


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


def _is_older_than(created_at: str, cutoff: datetime) -> bool:
    """Return True when a row predates the cutoff (fail closed on garbage)."""
    created = _parse_iso(created_at)
    return created is None or created <= cutoff


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
        limit = max(int(max_interactions), 1)
        trimmed = interactions[-limit:] if interactions else []
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

    async def get_valid(
        self,
        conn: aiosqlite.Connection,
        user_id: int,
        *,
        now: datetime | None = None,
        ttl_hours: int = 24,
    ) -> aiosqlite.Row | None:
        """Return the session only when it has not exceeded TTL (lazy expiry)."""
        row = await self.get(conn, user_id)
        if row is None:
            return None
        try:
            updated = _parse_iso(str(row["updated_at"]))
        except Exception:
            updated = None
        if updated is None:
            return None
        current = now or datetime.now(UTC)
        if current - updated > timedelta(hours=ttl_hours):
            return None
        return row

    async def append(
        self,
        conn: aiosqlite.Connection,
        user_id: int,
        language: str,
        interaction: str,
        max_interactions: int = 6,
    ) -> None:
        """Append one interaction, trimming to the last N (default 6)."""
        row = await self.get(conn, user_id)
        existing: list[str] = []
        if row is not None:
            try:
                loaded = json.loads(row["interactions"])
                if isinstance(loaded, list):
                    existing = [str(x) for x in loaded]
            except Exception:  # noqa: S110, BLE001 - start fresh on corrupt data
                logger.debug("session append: corrupt interactions reset")
                existing = []
        existing.append(interaction)
        await self.save(conn, user_id, language, existing, max_interactions)

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
        # Parsed comparison (not lexicographic) so Z/offset/naive values behave.
        cursor = await conn.execute("SELECT user_id, interactions, updated_at FROM sessions")
        rows = await cursor.fetchall()
        for row in rows:
            try:
                updated = _parse_iso(str(row["updated_at"]))
            except Exception:  # noqa: S110, BLE001 - treat unreadable as expired below
                logger.debug("session prune: unreadable updated_at")
                updated = None
            if updated is None or current - updated > timedelta(hours=ttl_hours):
                try:
                    await conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["user_id"],))
                except Exception:  # noqa: S112, BLE001 - prune must continue
                    logger.debug("session prune: delete failed")
                    continue
                continue
            try:
                loaded = json.loads(row["interactions"])
                interactions: list[str] = (
                    [str(x) for x in loaded] if isinstance(loaded, list) else []
                )
            except Exception:  # noqa: S112, BLE001 - skip corrupt row, keep others
                logger.debug("session prune: corrupt interactions skipped")
                continue
            limit = max(int(max_interactions), 1)
            if len(interactions) > limit:
                trimmed = interactions[-limit:]
                try:
                    await conn.execute(
                        "UPDATE sessions SET interactions = ? WHERE user_id = ?",
                        (json.dumps(trimmed), row["user_id"]),
                    )
                except Exception:  # noqa: S112, BLE001 - prune must continue
                    logger.debug("session prune: trim failed")
                    continue


class JobRepository:
    async def create(
        self,
        conn: aiosqlite.Connection,
        *,
        job_id: str,
        user_id: int,
        query: str,
        language: Language | str = Language.MIXED,
        domain: Domain | str = Domain.GENERAL,
        depth: Depth | str = Depth.STANDARD,
        risk_level: RiskLevel | str = RiskLevel.NORMAL,
        state: JobState | str = JobState.QUEUED,
        source_url: str | None = None,
    ) -> None:
        now = _now_iso()
        # A job implies a known user: auto-create the parent so the
        # users FK holds even when callers enqueue before /start.
        # OR IGNORE preserves an existing language preference.
        await conn.execute(
            """
            INSERT OR IGNORE INTO users (user_id, language, created_at, updated_at)
            VALUES (?, 'en', ?, ?)
            """,
            (user_id, now, now),
        )
        await conn.execute(
            """
            INSERT INTO jobs (job_id, user_id, query, language, domain, depth,
                              risk_level, state, repair_count, source_url,
                              created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                job_id,
                user_id,
                query,
                Language(language).value,
                Domain(domain).value,
                Depth(depth).value,
                RiskLevel(risk_level).value,
                JobState(state).value,
                source_url,
                now,
                now,
            ),
        )

    async def update_state(
        self,
        conn: aiosqlite.Connection,
        job_id: str,
        state: JobState | str,
        *,
        error: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        from research_agent.observability.redaction import redact_text

        safe_error = redact_text(error) if error else None
        if trace_id is not None:
            await conn.execute(
                "UPDATE jobs SET state=?, error=?, trace_id=?, updated_at=? WHERE job_id=?",
                (JobState(state).value, safe_error, trace_id, _now_iso(), job_id),
            )
        else:
            # Preserve existing trace_id (PLAN 25:1042) when caller has none.
            await conn.execute(
                "UPDATE jobs SET state = ?, error = ?, updated_at = ? WHERE job_id = ?",
                (JobState(state).value, safe_error, _now_iso(), job_id),
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
        # FTS sync is owned by the trg_reports_fts_insert/update triggers so
        # raw SQL and repository writes cannot diverge (no manual insert here).

    async def get(self, conn: aiosqlite.Connection, report_id: str) -> aiosqlite.Row | None:
        cursor = await conn.execute("SELECT * FROM reports WHERE report_id = ?", (report_id,))
        return await cursor.fetchone()

    async def search(
        self, conn: aiosqlite.Connection, query: str, limit: int = 10
    ) -> list[aiosqlite.Row]:
        """Full-text search over report topics and summaries, best match first."""
        cursor = await conn.execute(
            """
            SELECT r.* FROM report_fts
            JOIN reports r ON r.report_id = report_fts.report_id
            WHERE report_fts MATCH ?
            ORDER BY bm25(report_fts)
            LIMIT ?
            """,
            (query, limit),
        )
        return list(await cursor.fetchall())

    async def prune(
        self,
        conn: aiosqlite.Connection,
        *,
        retention_days: int = 90,
        now: datetime | None = None,
        reports_dir: Path | str | None = None,
    ) -> int:
        current = now or datetime.now(UTC)
        cutoff = current - timedelta(days=retention_days)
        # Parsed comparison (not lexicographic) so Z/offset/naive values
        # behave; unparseable timestamps fail closed (treated as expired).
        cursor = await conn.execute("SELECT report_id, markdown_path, created_at FROM reports")
        rows = await cursor.fetchall()
        expired = [row for row in rows if _is_older_than(str(row["created_at"]), cutoff)]
        for row in expired:
            report_id = row["report_id"]
            try:
                md = str(row["markdown_path"]) if row["markdown_path"] else ""
            except Exception:  # noqa: S110, BLE001 - prune must continue
                logger.debug("prune: unreadable markdown_path for %s", report_id)
                md = ""
            await conn.execute("DELETE FROM sources WHERE report_id = ?", (report_id,))
            await conn.execute("DELETE FROM report_fts WHERE report_id = ?", (report_id,))
            await conn.execute("DELETE FROM reports WHERE report_id = ?", (report_id,))
            if md:
                try:
                    p = Path(md)
                    base = Path(reports_dir) if reports_dir else None
                    # Only delete confined research-*.md files to avoid traversal.
                    if p.name.startswith("research-") and p.suffix == ".md":
                        target = (base / p.name) if base else p
                        if target.is_file():  # noqa: ASYNC240 - rare prune path
                            target.unlink()
                except Exception:  # noqa: S110, BLE001 - file cleanup best-effort
                    logger.debug("prune: markdown cleanup failed for %s", report_id)
        return len(expired)


def _normalize_source(source: Source | Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a Source model or plain mapping into a storable record."""
    if isinstance(source, Source):
        return {
            "source_ref": source.id,
            "title": source.title,
            "url": str(source.url),
            "publisher": source.publisher,
            "published_at": source.published_at.isoformat() if source.published_at else None,
            "accessed_at": source.accessed_at.isoformat(),
            "source_type": source.source_type.value,
        }
    return dict(source)


class SourceRepository:
    async def save_many(
        self,
        conn: aiosqlite.Connection,
        report_id: str,
        sources: list[Source | Mapping[str, Any]],
    ) -> None:
        for source in sources:
            record = _normalize_source(source)
            await conn.execute(
                """
                INSERT INTO sources (report_id, source_ref, title, url, publisher,
                                     published_at, accessed_at, source_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    record.get("source_ref"),
                    record.get("title", ""),
                    record.get("url", ""),
                    record.get("publisher"),
                    record.get("published_at"),
                    record.get("accessed_at", _now_iso()),
                    record.get("source_type", "web"),
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
        rows = await cursor.fetchall()
        expired = [row["cache_key"] for row in rows if _is_expired(str(row["expires_at"]))]
        for cache_key in expired:
            await conn.execute("DELETE FROM cache WHERE cache_key = ?", (cache_key,))
        return len(expired)
