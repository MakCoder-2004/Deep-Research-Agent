"""Integration tests for queue limits, lifecycle, cancel, and status (M2.28)."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from research_agent.config import Settings
from research_agent.models import JobState
from research_agent.persistence.database import connect, init_schema, open_db
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


async def test_one_active_index_survives_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "reopen-busy.db"
    async with open_db(db_path) as conn:
        first = await enqueue_request(conn, 44, "first query")
        cursor = await conn.execute("PRAGMA index_list('jobs')")
        indexes = {str(row["name"]) for row in await cursor.fetchall()}
        assert "ux_jobs_one_active_per_user" in indexes
    async with open_db(db_path) as reopened:
        with pytest.raises(UserBusyError) as exc_info:
            await enqueue_request(reopened, 44, "second query")
        assert exc_info.value.job_id == first.job_id


async def test_legacy_duplicate_active_jobs_are_reconciled_before_index(tmp_path: Path) -> None:
    """An upgrade keeps the oldest live row and is safe to run repeatedly."""
    db_path = tmp_path / "legacy-duplicates.db"
    conn = await connect(db_path)
    try:
        await conn.execute(
            """
            CREATE TABLE jobs (
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
            )
            """
        )
        oldest = (datetime.now(UTC) - timedelta(minutes=3)).isoformat()
        middle = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
        newest = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        for job_id, state, created_at in (
            ("keep-oldest", "active", oldest),
            ("cancel-queued", "queued", middle),
            ("fail-active", "active", newest),
        ):
            await conn.execute(
                "INSERT INTO jobs (job_id, user_id, query, state, created_at, updated_at) "
                "VALUES (?, 900, ?, ?, ?, ?)",
                (job_id, job_id, state, created_at, created_at),
            )
        await conn.commit()

        await init_schema(conn)
        cursor = await conn.execute(
            "SELECT job_id, state, error FROM jobs WHERE user_id = 900 ORDER BY job_id"
        )
        rows = {str(row["job_id"]): row for row in await cursor.fetchall()}
        assert str(rows["keep-oldest"]["state"]) == "active"
        assert str(rows["cancel-queued"]["state"]) == "cancelled"
        assert str(rows["fail-active"]["state"]) == "failed"
        assert "migration" in str(rows["cancel-queued"]["error"])
        assert "migration" in str(rows["fail-active"]["error"])

        cursor = await conn.execute("PRAGMA table_info('jobs')")
        assert "deadline_at" in {str(row["name"]) for row in await cursor.fetchall()}
        cursor = await conn.execute("PRAGMA index_list('jobs')")
        indexes = {str(row["name"]): row for row in await cursor.fetchall()}
        assert int(indexes["ux_jobs_one_active_per_user"]["unique"]) == 1
        assert int(indexes["ux_jobs_one_active_per_user"]["partial"]) == 1

        await init_schema(conn)
        cursor = await conn.execute("SELECT job_id, state FROM jobs WHERE user_id = 900")
        assert {str(row["job_id"]): str(row["state"]) for row in await cursor.fetchall()} == {
            "keep-oldest": "active",
            "cancel-queued": "cancelled",
            "fail-active": "failed",
        }
    finally:
        await conn.close()


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


async def test_terminal_cancellation_is_a_noop(tmp_path: Path) -> None:
    db_path = tmp_path / "terminal-cancel.db"
    async with open_db(db_path) as conn:
        job = await enqueue_request(conn, 12, "terminal query")
        await set_state(conn, job.job_id, JobState.ACTIVE)
        await set_state(conn, job.job_id, JobState.COMPLETED)
        assert await set_state(conn, job.job_id, JobState.CANCELLED) is False
        row = await get_user_active_job(conn, 12)
        assert row is None


async def test_concurrent_cancel_and_completion_keep_one_terminal_winner(tmp_path: Path) -> None:
    """Concurrent writers cannot overwrite the state transition that won."""
    db_path = tmp_path / "state-race.db"
    async with open_db(db_path) as conn:
        job = await enqueue_request(conn, 13, "race query")
        await set_state(conn, job.job_id, JobState.ACTIVE)

    first = await connect(db_path)
    second = await connect(db_path)
    try:
        results = await asyncio.gather(
            set_state(first, job.job_id, JobState.CANCELLED),
            set_state(second, job.job_id, JobState.COMPLETED),
            return_exceptions=True,
        )
        assert sum(result is True for result in results) == 1
        assert all(
            result is True or result is False or isinstance(result, ValueError)
            for result in results
        )
        async with open_db(db_path) as conn:
            cursor = await conn.execute("SELECT state FROM jobs WHERE job_id = ?", (job.job_id,))
            row = await cursor.fetchone()
            assert row is not None
            assert str(row["state"]) in {"cancelled", "completed"}
    finally:
        await first.close()
        await second.close()


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
        cursor = await conn.execute("SELECT state FROM jobs WHERE job_id = ?", (job.job_id,))
        row = await cursor.fetchone()
        assert row is not None and str(row["state"]) == "failed"
    async with open_db(db_path) as reopened:
        cursor = await reopened.execute("SELECT state FROM jobs WHERE job_id = ?", (job.job_id,))
        persisted = await cursor.fetchone()
        assert persisted is not None and str(persisted["state"]) == "failed"


async def test_queued_timeout_starts_before_worker_dispatch(tmp_path: Path) -> None:
    """A job that expires before start is failed without executing."""
    db_path = tmp_path / "timeout-before-start.db"
    queue = BoundedJobQueue(db_path, job_timeout_seconds=1)
    ref = await queue.enqueue(551, "queued timeout")
    await asyncio.sleep(1.1)
    executed = asyncio.Event()

    async def _must_not_run(_job: JobRef) -> None:
        executed.set()

    queue.run_placeholder = _must_not_run  # type: ignore[method-assign]
    await queue.start()

    async def _wait_for_failure() -> None:
        while True:
            async with open_db(db_path) as conn:
                cursor = await conn.execute(
                    "SELECT state, error FROM jobs WHERE job_id = ?", (ref.job_id,)
                )
                row = await cursor.fetchone()
            if row is not None and str(row["state"]) == "failed":
                assert str(row["error"]) == "job timeout"
                return
            await asyncio.sleep(0.02)

    try:
        await asyncio.wait_for(_wait_for_failure(), timeout=2)
        assert not executed.is_set()
        async with open_db(db_path) as conn:
            cursor = await conn.execute(
                "SELECT deadline_at, created_at FROM jobs WHERE job_id = ?", (ref.job_id,)
            )
            row = await cursor.fetchone()
            assert row is not None and row["deadline_at"] is not None
    finally:
        await queue.stop()


async def test_queue_stop_persists_queued_and_active_recovery(tmp_path: Path) -> None:
    """Shutdown does not leave queued or interrupted active jobs stranded."""
    db_path = tmp_path / "stop-recovery.db"
    queue = BoundedJobQueue(db_path, max_concurrent_jobs=1, job_timeout_seconds=5)
    active = await queue.enqueue(561, "active recovery")
    started = asyncio.Event()
    release = asyncio.Event()

    async def _active_wait(job: JobRef) -> None:
        async with open_db(db_path) as conn:
            await set_state(conn, job.job_id, JobState.ACTIVE)
        if job.job_id == active.job_id:
            started.set()
            await release.wait()

    queue.run_placeholder = _active_wait  # type: ignore[method-assign]
    await queue.start()
    await asyncio.wait_for(started.wait(), timeout=1)
    queued = await queue.enqueue(562, "queued recovery")
    await queue.stop()

    async with open_db(db_path) as conn:
        cursor = await conn.execute(
            "SELECT job_id, state, error FROM jobs WHERE job_id IN (?, ?) ORDER BY job_id",
            (queued.job_id, active.job_id),
        )
        rows = {str(row["job_id"]): row for row in await cursor.fetchall()}
    assert str(rows[active.job_id]["state"]) == "failed"
    assert str(rows[queued.job_id]["state"]) == "cancelled"
    assert "recovery" in str(rows[active.job_id]["error"])
    assert "recovery" in str(rows[queued.job_id]["error"])


async def test_recover_pending_jobs_handles_restart_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "restart-recovery.db"
    async with open_db(db_path) as conn:
        queued = await enqueue_request(conn, 571, "queued restart")
        active = await enqueue_request(conn, 572, "active restart")
        await set_state(conn, active.job_id, JobState.ACTIVE)

    queue = BoundedJobQueue(db_path)
    try:
        assert await queue.recover_pending_jobs() == 2
        async with open_db(db_path) as conn:
            cursor = await conn.execute(
                "SELECT job_id, state FROM jobs WHERE job_id IN (?, ?)",
                (queued.job_id, active.job_id),
            )
            rows = {str(row["job_id"]): str(row["state"]) for row in await cursor.fetchall()}
        assert rows == {queued.job_id: "cancelled", active.job_id: "failed"}
        assert await queue.recover_pending_jobs() == 0
    finally:
        await queue.stop()


async def test_run_with_semaphore_timeout_while_queued(tmp_path: Path) -> None:
    """Timeout includes semaphore wait and durably fails the queued job."""
    db_path = tmp_path / "timeout-worker.db"
    async with open_db(db_path):
        pass
    queue = BoundedJobQueue(db_path, max_concurrent_jobs=1, job_timeout_seconds=30)
    first = await queue.enqueue(56, "first slow query")
    second = await queue.enqueue(57, "second slow query")
    async with open_db(db_path) as conn:
        await conn.execute(
            "UPDATE jobs SET deadline_at = ? WHERE job_id = ?",
            ((datetime.now(UTC) + timedelta(seconds=1)).isoformat(), second.job_id),
        )
        await conn.commit()
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow(job: JobRef) -> None:
        async with open_db(db_path) as conn:
            await set_state(conn, job.job_id, JobState.ACTIVE)
        if job.job_id == first.job_id:
            started.set()
            await release.wait()
            async with open_db(db_path) as conn:
                await set_state(conn, job.job_id, JobState.COMPLETED)
        else:
            await release.wait()

    queue.run_placeholder = _slow  # type: ignore[method-assign]
    await queue.start()
    await asyncio.wait_for(started.wait(), timeout=5)

    async def _wait_for_failure() -> None:
        while True:
            async with open_db(db_path) as conn:
                cursor = await conn.execute(
                    "SELECT state FROM jobs WHERE job_id = ?", (second.job_id,)
                )
                row = await cursor.fetchone()
            if row is not None and str(row["state"]) == "failed":
                return
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_wait_for_failure(), timeout=3)
    async with open_db(db_path) as conn:
        cursor = await conn.execute(
            "SELECT state, error FROM jobs WHERE job_id = ?", (second.job_id,)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert str(row["state"]) == "failed"
        assert str(row["error"]) == "job timeout"
    release.set()
    await asyncio.wait_for(queue._queue.join(), timeout=2)
    await queue.stop()


async def test_cancel_active_worker_preserves_event_identity(tmp_path: Path) -> None:
    """Cancellation wakes the event already held by an active worker."""
    from research_agent.services.queue import _CANCEL_EVENTS

    db_path = tmp_path / "cancel-active-worker.db"
    queue = BoundedJobQueue(db_path, job_timeout_seconds=5)
    ref = await queue.enqueue(58, "active query")
    active = asyncio.Event()
    cancel_seen = asyncio.Event()
    release = asyncio.Event()
    cancel_event = queue.get_cancel_event(ref.job_id)

    async def _wait_for_cancel(job: JobRef) -> None:
        async with open_db(db_path) as conn:
            await set_state(conn, job.job_id, JobState.ACTIVE)
        active.set()
        await cancel_event.wait()
        cancel_seen.set()
        await release.wait()

    queue.run_placeholder = _wait_for_cancel  # type: ignore[method-assign]
    await queue.start()
    await asyncio.wait_for(active.wait(), timeout=1)
    async with open_db(db_path) as conn:
        assert await cancel_user_job(conn, 58) is True
    assert queue.get_cancel_event(ref.job_id) is cancel_event
    await asyncio.wait_for(cancel_seen.wait(), timeout=1)
    release.set()
    await asyncio.wait_for(queue._queue.join(), timeout=2)
    assert ref.job_id not in _CANCEL_EVENTS
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
            "SELECT COUNT(*) AS n FROM jobs WHERE user_id = ? AND state IN ('queued', 'active')",
            (77,),
        )
        row = await cursor.fetchone()
        assert row is not None and int(row["n"]) == 0


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


async def test_terminal_persistence_failure_is_logged_and_recovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed state write is observable and leaves a row recoverable on restart."""
    import research_agent.services.queue as queue_module

    db_path = tmp_path / "persistence-failure.db"
    queue = BoundedJobQueue(db_path)
    ref = await queue.enqueue(601, "persistence failure")

    async def _fail_persist(*_args: object, **_kwargs: object) -> bool:
        raise sqlite3.OperationalError("database is locked")

    async def _fail_job(_job: JobRef) -> None:
        raise RuntimeError("placeholder failure")

    monkeypatch.setattr(queue_module, "set_state", _fail_persist)
    queue.run_placeholder = _fail_job  # type: ignore[method-assign]
    caplog.set_level(logging.ERROR, logger="research_agent.services.queue")
    await queue.start()
    await asyncio.wait_for(queue._queue.join(), timeout=2)
    assert "terminal state persistence failed" in caplog.text

    monkeypatch.undo()
    try:
        await queue.stop()
        async with open_db(db_path) as conn:
            cursor = await conn.execute("SELECT state FROM jobs WHERE job_id = ?", (ref.job_id,))
            row = await cursor.fetchone()
            assert row is not None and str(row["state"]) == "cancelled"
    finally:
        clear_all_cancel_events()


async def test_cancel_registry_bounded() -> None:
    """Live events beyond the old bound retain cancellation identity."""
    from research_agent.services.queue import _CANCEL_EVENTS, _MAX_CANCEL_EVENTS

    clear_all_cancel_events()
    try:
        live_event = get_cancel_event("live-job")
        for i in range(_MAX_CANCEL_EVENTS + 50):
            get_cancel_event(f"job-{i}")
        assert len(_CANCEL_EVENTS) > _MAX_CANCEL_EVENTS
        assert get_cancel_event("live-job") is live_event
    finally:
        clear_all_cancel_events()


async def test_default_queue_helper(tmp_path: Path) -> None:
    """Handlers can retrieve the live queue via the default registry."""
    db_path = tmp_path / "default-q.db"
    async with open_db(db_path):
        pass
    queue = BoundedJobQueue(db_path)
    try:
        assert get_default_queue() is None or isinstance(get_default_queue(), BoundedJobQueue)
        set_default_queue(queue)
        assert get_default_queue() is queue
    finally:
        set_default_queue(None)
        clear_all_cancel_events()
        await queue.stop()


async def test_enqueue_request_id_validation_and_idempotency(tmp_path: Path) -> None:
    """Explicit request IDs are validated, owned, and deduplicated."""
    db_path = tmp_path / "reqid.db"
    async with open_db(db_path):
        pass
    queue = BoundedJobQueue(db_path)
    try:
        first = await queue.enqueue(70, "req query", request_id="req-123")
        assert first.job_id == "req-123"
        assert queue.pending == 1
        second = await queue.enqueue(70, "req query", request_id="req-123")
        assert second.job_id == "req-123"
        assert queue.pending == 1
        with pytest.raises(ValueError, match="unsupported language"):
            await queue.enqueue(71, "bad language", "fr")
        with pytest.raises(ValueError, match="safe identifier"):
            await queue.enqueue(71, "bad ID", request_id="bad ID")
        with pytest.raises(ValueError, match="another user"):
            await queue.enqueue(71, "wrong owner", request_id="req-123")
        async with open_db(db_path) as conn:
            again = await enqueue_request(conn, 70, "req query", request_id="req-123")
            assert again.job_id == "req-123"
            await set_state(conn, first.job_id, JobState.ACTIVE)
            await set_state(conn, first.job_id, JobState.COMPLETED)
        with pytest.raises(ValueError, match="already terminal"):
            await queue.enqueue(70, "requeue", request_id="req-123")
    finally:
        await queue.stop()
        clear_all_cancel_events()


async def test_put_compat_and_instance_helpers(tmp_path: Path) -> None:
    """put() schedules DB-only jobs live; instance helpers delegate correctly."""
    db_path = tmp_path / "put.db"
    queue = BoundedJobQueue(db_path)
    try:
        async with open_db(db_path) as conn:
            job = await enqueue_request(conn, 71, "put query")
        assert queue.pending == 0
        await queue.put(job)
        assert queue.pending == 1
        assert queue.get_cancel_event(job.job_id) is get_cancel_event(job.job_id)
        assert await queue.queue_position(job.job_id) == 1
    finally:
        await queue.stop()
        clear_all_cancel_events()
