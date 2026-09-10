"""Report retrieval helpers for the Telegram gateway (owner-scoped)."""

from __future__ import annotations

import aiosqlite


async def list_recent_reports(
    conn: aiosqlite.Connection, user_id: int, limit: int = 5
) -> list[aiosqlite.Row]:
    """List the user's most recent reports (owner-scoped JOIN, newest first)."""
    cursor = await conn.execute(
        """
        SELECT r.* FROM reports r
        JOIN jobs j ON j.job_id = r.job_id
        WHERE j.user_id = ?
        ORDER BY r.created_at DESC
        LIMIT ?
        """,
        (user_id, limit),
    )
    return list(await cursor.fetchall())


async def get_report_bundle(
    conn: aiosqlite.Connection, user_id: int, report_id: str
) -> tuple[aiosqlite.Row | None, list[aiosqlite.Row]]:
    """Return (report, sources) when owned by user, else (None, [])."""
    cursor = await conn.execute(
        """
        SELECT r.* FROM reports r
        JOIN jobs j ON j.job_id = r.job_id
        WHERE r.report_id = ? AND j.user_id = ?
        """,
        (report_id, user_id),
    )
    report = await cursor.fetchone()
    if report is None:
        return None, []
    cursor = await conn.execute(
        "SELECT * FROM sources WHERE report_id = ? ORDER BY source_ref",
        (report_id,),
    )
    sources = list(await cursor.fetchall())
    return report, sources
