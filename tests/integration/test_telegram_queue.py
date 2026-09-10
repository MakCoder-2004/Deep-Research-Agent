"""Integration tests for queue limits, lifecycle, cancel, and status (M2.28)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from research_agent.config import Settings
from research_agent.models import JobState
from research_agent.persistence.database import open_db
from research_agent.services.queue import (
    BoundedJobQueue,
    JobRef,
    UserBusyError,
    cancel_user_job,
    clear_all_cancel_events,
    enqueue_request,
    get_cancel_event,
    get_default_queue,
    get_user_active_job,
    queue_position,
    set_default_queue,
    set_state,
)


def _msg(user_id: int, text: str, lang: str = "en") -> MagicMock:
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, language_code=lang)
    message.chat = SimpleNamespace(id=user_id, type="private")
    message.text = text
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=5))
    message.answer_document = AsyncMock()
    return message


async def test_fifo_positions(tmp_path: Path) -> None:
    db_path = tmp_path / "fifo.db"
    async with open_db(db_path) as conn:
        first = await enqueue_request(conn, 1, "query one")
        second = await enqueue_request(conn, 2, "query two")
        third = await enqueue_request(conn, 3, "query three")
        assert first.position == 1
        assert second.position == 2
        assert third.position == 3
        assert await queue_position(conn, first.job_id) == 1
        assert await queue_position(conn, second.job_id) == 2
        assert await queue_position(conn, third.job_id) == 3


async def test_max_three_concurrent_config(tmp_path: Path) -> None:
    db_path = tmp_path / "conc.db"
    queue = BoundedJobQueue(db_path)
    assert queue.max_concurrent_jobs == 3
    custom = BoundedJobQueue(db_path, max_concurrent_jobs=2)
    assert custom.max_concurrent_jobs == 2
    settings = Settings(
        _env_file=None,  # type: ignore[arg-type]
        ENVIRONMENT="development",
        TELEGRAM_BOT_TOKEN="test-token",  # noqa: S105, S106
        TELEGRAM_ALLOWED_USER_IDS="1",
        DATABASE_PATH=db_path,  # type: ignore[arg-type]
        MAX_CONCURRENT_JOBS=3,
    )
    from_settings = BoundedJobQueue.from_settings(settings)
    assert from_settings.max_concurrent_jobs == 3
    with pytest.raises(ValueError):
        BoundedJobQueue(db_path, max_concurrent_jobs=0)


async def test_one_active_per_user_reject(tmp_path: Path) -> None:
    db_path = tmp_path / "busy.db"
    async with open_db(db_path) as conn:
        first = await enqueue_request(conn, 42, "first query")
        assert first.job_id
        with pytest.raises(UserBusyError) as exc_info:
            await enqueue_request(conn, 42, "second query")
        assert exc_info.value.user_id == 42
        assert exc_info.value.job_id == first.job_id
        other = await enqueue_request(conn, 43, "other user query")
        assert other.job_id != first.job_id


async def test_lifecycle_transitions_durable(tmp_path: Path) -> None:
    db_path = tmp_path / "life.db"
    async with open_db(db_path) as conn:
        job = await enqueue_request(conn, 7, "lifecycle query")
        await set_state(conn, job.job_id, JobState.ACTIVE)
        row = await get_user_active_job(conn, 7)
        assert row is not None and str(row["state"]) == "active"
        await set_state(conn, job.job_id, JobState.COMPLETED)
        cursor = await conn.execute("SELECT state FROM jobs WHERE job_id = ?", (job.job_id,))
        done = await cursor.fetchone()
        assert done is not None and str(done["state"]) == "completed"
    async with open_db(db_path) as reopened:
        cursor = await reopened.execute("SELECT state FROM jobs WHERE job_id = ?", (job.job_id,))
        persisted = await cursor.fetchone()
        assert persisted is not None and str(persisted["state"]) == "completed"


async def test_lifecycle_failed_and_cancelled_branches(tmp_path: Path) -> None:
    db_path = tmp_path / "branches.db"
    async with open_db(db_path) as conn:
        failed = await enqueue_request(conn, 8, "fail query")
        await set_state(conn, failed.job_id, JobState.ACTIVE)
        await set_state(conn, failed.job_id, JobState.FAILED, error="boom")
        cursor = await conn.execute("SELECT state FROM jobs WHERE job_id = ?", (failed.job_id,))
        row = await cursor.fetchone()
        assert row is not None and str(row["state"]) == "failed"
        cancelled = await enqueue_request(conn, 9, "cancel query")
        await set_state(conn, cancelled.job_id, JobState.ACTIVE)
        await set_state(conn, cancelled.job_id, JobState.CANCELLED)
        cursor = await conn.execute("SELECT state FROM jobs WHERE job_id = ?", (cancelled.job_id,))
        row = await cursor.fetchone()
        assert row is not None and str(row["state"]) == "cancelled"
        direct = await enqueue_request(conn, 10, "direct cancel")
        await set_state(conn, direct.job_id, JobState.CANCELLED)
        cursor = await conn.execute("SELECT state FROM jobs WHERE job_id = ?", (direct.job_id,))
        row = await cursor.fetchone()
        assert row is not None and str(row["state"]) == "cancelled"


async def test_illegal_transition_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "illegal.db"
    async with open_db(db_path) as conn:
        job = await enqueue_request(conn, 11, "illegal query")
        with pytest.raises(ValueError):
            await set_state(conn, job.job_id, JobState.COMPLETED)
        with pytest.raises(ValueError):
            await set_state(conn, "no-such-job", JobState.ACTIVE)


async def test_cancel_queued_and_active(tmp_path: Path) -> None:
    db_path = tmp_path / "cancel.db"
    async with open_db(db_path) as conn:
        queued = await enqueue_request(conn, 20, "queued query")
        assert await cancel_user_job(conn, 20) is True
        cursor = await conn.execute("SELECT state FROM jobs WHERE job_id = ?", (queued.job_id,))
        row = await cursor.fetchone()
        assert row is not None and str(row["state"]) == "cancelled"
        assert await cancel_user_job(conn, 20) is False
        active = await enqueue_request(conn, 21, "active query")
        await set_state(conn, active.job_id, JobState.ACTIVE)
        assert await cancel_user_job(conn, 21) is True
        cursor = await conn.execute("SELECT state FROM jobs WHERE job_id = ?", (active.job_id,))
        row = await cursor.fetchone()
        assert row is not None and str(row["state"]) == "cancelled"


async def test_status_positions_queued_and_active(tmp_path: Path) -> None:
    from research_agent.telegram.handlers import status_handler

    db_path = tmp_path / "status.db"
    async with open_db(db_path) as conn:
        first = await enqueue_request(conn, 30, "first")
        await enqueue_request(conn, 31, "second")
        assert await queue_position(conn, first.job_id) == 1
        queued_msg = _msg(30, "/status", "en")
        await status_handler(queued_msg, conn=conn)
        queued_reply = queued_msg.answer.call_args[0][0]
        assert "#1" in queued_reply
        await set_state(conn, first.job_id, JobState.ACTIVE)
        active_msg = _msg(30, "/status", "en")
        await status_handler(active_msg, conn=conn)
        active_reply = active_msg.answer.call_args[0][0]
        assert "active" in active_reply.lower()
        none_msg = _msg(99, "/status", "en")
        await status_handler(none_msg, conn=conn)
        assert "no active" in none_msg.answer.call_args[0][0].lower()


async def test_bounded_queue_enqueue_fifo_pending(tmp_path: Path) -> None:
    db_path = tmp_path / "bq.db"
    async with open_db(db_path):
        pass
    queue = BoundedJobQueue(db_path)
    first = await queue.enqueue(101, "q1")
    second = await queue.enqueue(102, "q2")
    assert queue.pending == 2
    assert first.position == 1
    assert second.position == 2
    with pytest.raises(UserBusyError):
        await queue.enqueue(101, "q1 again")
    await queue.stop()


async def test_timeout_while_queued_marks_failed(tmp_path: Path) -> None:
    """Queued -> failed must be allowed so timeouts while queued stay durable."""
    db_path = tmp_path / "timeout-queued.db"
    async with open_db(db_path) as conn:
        job = await enqueue_request(conn, 55, "timeout query")
        # Direct transition used by the worker timeout path while still queued.
        await set_state(conn, job.job_id, JobState.FAILED, error="job timeout")
        cursor = await conn.execute(
            "SELECT state FROM jobs WHERE job_id = ?", (job.job_id,)
        )
        row = await cursor.fetchone()
        assert row is not None and str(row["state"]) == "failed"
    async with open_db(db_path) as reopened:
        cursor = await reopened.execute(
            "SELECT state FROM jobs WHERE job_id = ?", (job.job_id,)
        )
        persisted = await cursor.fetchone()
        assert persisted is not None and str(persisted["state"]) == "failed"


async def test_run_with_semaphore_timeout_while_queued(tmp_path: Path) -> None:
    """Worker timeout before active must mark queued jobs failed, not stuck."""
    import asyncio

    db_path = tmp_path / "timeout-worker.db"
    async with open_db(db_path):
        pass
    queue = BoundedJobQueue(db_path, job_timeout_seconds=1)
    ref = await queue.enqueue(56, "slow query")

    async def _slow(_job: object) -> None:
        await asyncio.sleep(5)

    queue.run_placeholder = _slow  # type: ignore[method-assign]
    # Must not raise ValueError for queued -> failed.
    await queue._run_with_semaphore(ref)
    async with open_db(db_path) as conn:
        cursor = await conn.execute(
            "SELECT state FROM jobs WHERE job_id = ?", (ref.job_id,)
        )
        row = await cursor.fetchone()
        assert row is not None and str(row["state"]) == "failed"
    await queue.stop()


async def test_concurrent_enqueue_one_active_per_user(tmp_path: Path) -> None:
    """Concurrent enqueues for one user must leave exactly one winner."""
    import asyncio

    db_path = tmp_path / "race.db"
    async with open_db(db_path):
        pass
    queue = BoundedJobQueue(db_path)
    results = await asyncio.gather(
        queue.enqueue(77, "first"),
        queue.enqueue(77, "second"),
        return_exceptions=True,
    )
    successes = [r for r in results if isinstance(r, JobRef)]
    busy = [r for r in results if isinstance(r, UserBusyError)]
    assert len(successes) == 1
    assert len(busy) == 1
    assert busy[0].user_id == 77
    await queue.stop()
    async with open_db(db_path) as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM jobs "
            "WHERE user_id = ? AND state IN ('queued', 'active')",
            (77,),
        )
        row = await cursor.fetchone()
        assert row is not None and int(row["n"]) == 1


async def test_cancel_events_cleanup_on_terminal_and_stop(tmp_path: Path) -> None:
    """Terminal transitions pop events; stop() clears the registry."""
    from research_agent.services.queue import _CANCEL_EVENTS

    clear_all_cancel_events()
    db_path = tmp_path / "cancel-clean.db"
    async with open_db(db_path) as conn:
        job = await enqueue_request(conn, 60, "cleanup query")
        # enqueue creates no event for DB-only path; create one explicitly.
        get_cancel_event(job.job_id)
        assert job.job_id in _CANCEL_EVENTS
        await set_state(conn, job.job_id, JobState.ACTIVE)
        # Active is non-terminal: event must survive.
        assert job.job_id in _CANCEL_EVENTS
        await set_state(conn, job.job_id, JobState.COMPLETED)
        assert job.job_id not in _CANCEL_EVENTS
    queue = BoundedJobQueue(db_path)
    ref = await queue.enqueue(61, "live query")
    assert ref.job_id in _CANCEL_EVENTS
    await queue.stop()
    assert ref.job_id not in _CANCEL_EVENTS
    assert len(_CANCEL_EVENTS) == 0


async def test_cancel_registry_bounded() -> None:
    """Unbounded job churn must not leak unlimited cancel events."""
    from research_agent.services.queue import _CANCEL_EVENTS, _MAX_CANCEL_EVENTS

    clear_all_cancel_events()
    try:
        for i in range(_MAX_CANCEL_EVENTS + 50):
            get_cancel_event(f"job-{i}")
        assert len(_CANCEL_EVENTS) <= _MAX_CANCEL_EVENTS
    finally:
        clear_all_cancel_events()


async def test_default_queue_helper(tmp_path: Path) -> None:
    """Handlers can retrieve the live queue via the default registry."""
    db_path = tmp_path / "default-q.db"
    async with open_db(db_path):
        pass
    queue = BoundedJobQueue(db_path)
    try:
        assert get_default_queue() is None or isinstance(
            get_default_queue(), BoundedJobQueue
        )
        set_default_queue(queue)
        assert get_default_queue() is queue
    finally:
        set_default_queue(None)
        clear_all_cancel_events()
        await queue.stop()
