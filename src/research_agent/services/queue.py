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
