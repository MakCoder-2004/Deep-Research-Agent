"""Bounded research-job queue stub for the Telegram gateway (M2.7+)."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

import aiosqlite

from research_agent.persistence.repositories import JobRepository


@dataclass
class JobRef:
    """Lightweight reference to an enqueued research job."""

    job_id: str
    user_id: int
    query: str
    position: int = 1


async def enqueue_request(
    conn: aiosqlite.Connection,
    user_id: int,
    query: str,
    language: str = "mixed",
) -> JobRef:
    """Persist a queued job and return its reference (stub position 1)."""
    job_id = str(uuid4())
    await JobRepository().create(
        conn,
        job_id=job_id,
        user_id=user_id,
        query=query,
        language=language,  # type: ignore[arg-type]
    )
    await conn.commit()
    return JobRef(job_id=job_id, user_id=user_id, query=query, position=1)


async def get_user_active_job(conn: aiosqlite.Connection, user_id: int) -> aiosqlite.Row | None:
    """Return the oldest queued/active job for a user, if any."""
    cursor = await conn.execute(
        """
        SELECT * FROM jobs
        WHERE user_id = ? AND state IN ('queued', 'active')
        ORDER BY created_at ASC
        LIMIT 1
        """,
        (user_id,),
    )
    return await cursor.fetchone()


async def queue_position(conn: aiosqlite.Connection, job_id: str) -> int:
    """Return 1-indexed queue position for a queued job (0 when not queued)."""
    cursor = await conn.execute(
        "SELECT job_id, state, created_at FROM jobs WHERE job_id = ?", (job_id,)
    )
    row = await cursor.fetchone()
    if row is None or row["state"] != "queued":
        return 0
    created_at = row["created_at"]
    cursor = await conn.execute(
        """
        SELECT COUNT(*) AS n FROM jobs
        WHERE state = 'queued' AND created_at <= ?
        """,
        (created_at,),
    )
    count_row = await cursor.fetchone()
    if count_row is None:
        return 1
    try:
        return max(1, int(count_row["n"]))
    except (TypeError, ValueError):
        return 1
